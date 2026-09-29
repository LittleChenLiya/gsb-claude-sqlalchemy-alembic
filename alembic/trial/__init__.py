"""Trial-run Alembic migrations against a disposable SQLite copy.

This module provides a reusable entry point,
:func:`run_sqlite_trial`, that executes an Alembic migration
environment against a **copy** of an existing SQLite database, so that
a migration can be rehearsed without any risk of modifying the
original database file.

How it works:

* The source database is opened **read-only** (SQLite URI
  ``mode=ro``); the trial never writes to the original file, its WAL,
  or its shared-memory files.
* A consistent copy is produced with the SQLite online backup API
  (:meth:`sqlite3.Connection.backup`), which includes committed data
  that still lives only in the WAL.  The main database file is never
  copied directly, so un-checkpointed WAL content is not lost.
* The copy is handed to the Alembic migration environment as an
  injected connection (see :mod:`alembic.trial.env` for the companion
  ``env.py``).  On that connection, ``ATTACH DATABASE`` and extension
  loading are refused via a SQLite authorizer, so migrations cannot
  reach outside the copied database through SQL.
* The trial reports the start and end revisions, whether it succeeded,
  failed, or was cancelled, and any error that occurred.  A failed or
  cancelled trial is never reported as a completed migration.
* When the trial ends -- whether it succeeded, failed, raised, or was
  cancelled through an injected stop point -- all connections are
  closed and the temporary copy is deleted, leaving no file locks
  behind.

Scope of the isolation guarantee: only database operations performed
on the injected connection are isolated to the disposable copy.  A
migration file is ordinary Python code; this module does **not** claim
to sandbox arbitrary Python.  A migration that opens its own
connections, writes files, or touches the network can still affect the
outside world.

"""

from __future__ import annotations

import dataclasses
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Callable
from typing import Optional
from typing import Tuple
from typing import Union

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from .. import command
from ..config import Config
from ..runtime.migration import MigrationContext
from ..script import ScriptDirectory
from ..util.exc import CommandError

__all__ = [
    "TRIAL_CONNECTION_ATTRIBUTE",
    "STATUS_SUCCESS",
    "STATUS_FAILED",
    "STATUS_CANCELLED",
    "TrialError",
    "TrialCancelled",
    "TrialResult",
    "run_sqlite_trial",
]

#: Key in ``Config.attributes`` under which the trial runner passes the
#: connection for the copied database to the migration environment.
TRIAL_CONNECTION_ATTRIBUTE = "trial_connection"

#: Trial finished and the target revision was reached on the copy.
STATUS_SUCCESS = "success"

#: Trial raised an error; the target revision was not reached.
STATUS_FAILED = "failed"

#: Trial was cancelled through the injected stop point.
STATUS_CANCELLED = "cancelled"

_MISSING = object()


class TrialError(Exception):
    """Raised for trial setup problems, e.g. a missing source database."""


class TrialCancelled(TrialError):
    """Raised internally when the injected stop point cancels a trial."""


@dataclasses.dataclass
class TrialResult:
    """Report of a single trial run.

    .. attribute:: status

        One of ``"success"``, ``"failed"`` or
        ``"cancelled"``.  ``"success"`` is only reported when the
        target revision was fully reached on the copy; a trial that
        raised or was cancelled partway is never reported as a
        completed migration.

    """

    status: str
    source_path: Path
    target_revision: str
    resolved_target: Optional[str] = None
    start_revision: Optional[str] = None
    end_revision: Optional[str] = None
    applied_revisions: Tuple[str, ...] = ()
    error: Optional[str] = None
    error_type: Optional[str] = None
    workdir: Optional[Path] = None

    @property
    def ok(self) -> bool:
        """True if the trial reached the target revision on the copy."""
        return self.status == STATUS_SUCCESS

    def format(self) -> str:
        """Render a human-readable report of the trial run."""
        lines = [
            f"status:           {self.status}",
            f"source database:  {self.source_path}",
            f"target revision:  {self.target_revision}"
            + (
                f" (resolved: {self.resolved_target})"
                if self.resolved_target
                and self.resolved_target != self.target_revision
                else ""
            ),
            f"start revision:   {self.start_revision or '(base)'}",
            f"end revision:     {self.end_revision or '(base)'}"
            + ("" if self.ok else " (on the discarded copy)"),
            "applied on copy:  "
            + (
                ", ".join(self.applied_revisions)
                if self.applied_revisions
                else "(none)"
            ),
        ]
        if self.error is not None:
            lines.append(f"error:            [{self.error_type}] {self.error}")
        if self.workdir is not None:
            lines.append(
                f"temporary copy:   {self.workdir} (removed: "
                f"{not self.workdir.exists()})"
            )
        return "\n".join(lines)


def _install_connection_guard(conn: sqlite3.Connection) -> None:
    """Refuse ATTACH and extension loading on the copied database."""

    def authorizer(
        action: int,
        arg1: Optional[str],
        arg2: Optional[str],
        dbname: Optional[str],
        source: Optional[str],
    ) -> int:
        if action == sqlite3.SQLITE_ATTACH:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            func_name = (arg2 or arg1 or "").lower()
            if func_name == "load_extension":
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    try:
        conn.enable_load_extension(False)
    except (AttributeError, sqlite3.NotSupportedError):
        pass


def _current_revision(connection: Connection) -> Optional[str]:
    context = MigrationContext.configure(connection)
    return context.get_current_revision()


def _upgrade_path(
    script: ScriptDirectory, current: Optional[str], target: str
) -> Tuple[str, ...]:
    """Revisions to apply, in order, from ``current`` to ``target``."""
    down_to_up = [
        rev.revision
        for rev in reversed(
            list(script.walk_revisions(base=current or "base", head=target))
        )
    ]
    if current is not None:
        if not down_to_up or down_to_up[0] != current:
            raise TrialError(
                f"cannot compute an upgrade path from {current!r} "
                f"to {target!r}"
            )
        down_to_up = down_to_up[1:]
    return tuple(down_to_up)


def run_sqlite_trial(
    source_path: Union[str, Path],
    config: Union[str, Path, Config],
    revision: str = "head",
    *,
    stop_check: Optional[Callable[[str], bool]] = None,
    tempdir_parent: Optional[Union[str, Path]] = None,
) -> TrialResult:
    """Run Alembic migrations against a disposable copy of a SQLite database.

    :param source_path: path to the existing SQLite database file.  It is
        opened read-only and is never modified.
    :param config: an Alembic :class:`~alembic.config.Config` or the path
        to an ``alembic.ini`` file.  The referenced migration
        environment must use the injected connection; see
        :mod:`alembic.trial.env`.
    :param revision: target revision, e.g. ``"head"`` or a revision id.
        Only upgrades are supported.
    :param stop_check: optional callable invoked with the next revision
        id before each migration step; returning ``True`` cancels the
        trial.  This is the injectable stop point used to rehearse
        cancellation.
    :param tempdir_parent: optional parent directory for the temporary
        copy; defaults to the system temp directory.
    :return: a :class:`TrialResult`.  The original database, all
        connections, and the temporary copy are always cleaned up,
        including on migration errors and cancellation.

    """
    source = Path(source_path)
    if not source.exists():
        raise TrialError(f"source database does not exist: {source}")

    if isinstance(config, Config):
        cfg = config
    else:
        cfg = Config(str(config))

    if tempdir_parent is not None:
        Path(tempdir_parent).mkdir(parents=True, exist_ok=True)
    workdir = Path(
        tempfile.mkdtemp(
            prefix="alembic-trial-",
            dir=str(tempdir_parent) if tempdir_parent is not None else None,
        )
    )
    copy_path = workdir / "trial.db"
    result = TrialResult(
        status=STATUS_FAILED,
        source_path=source,
        target_revision=revision,
        workdir=workdir,
    )

    source_conn: Optional[sqlite3.Connection] = None
    copy_conn: Optional[sqlite3.Connection] = None
    engine = None
    try:
        # read-only open of the original; the trial never writes to it.
        source_conn = sqlite3.connect(
            source.resolve().as_uri() + "?mode=ro", uri=True
        )
        copy_conn = sqlite3.connect(str(copy_path))
        _install_connection_guard(copy_conn)
        # consistent copy via the SQLite backup API; includes committed
        # data that has not been checkpointed out of the WAL yet.
        source_conn.backup(copy_conn)
        source_conn.close()
        source_conn = None

        engine = create_engine(
            "sqlite://",
            creator=lambda: copy_conn,
            poolclass=StaticPool,
        )
        with engine.connect() as connection:
            previous = cfg.attributes.get(TRIAL_CONNECTION_ATTRIBUTE, _MISSING)
            cfg.attributes[TRIAL_CONNECTION_ATTRIBUTE] = connection
            try:
                script = ScriptDirectory.from_config(cfg)
                result.start_revision = _current_revision(connection)
                # release the transaction auto-begun by the SELECT above;
                # otherwise Alembic treats the connection as externally
                # transactional and never commits the migration steps.
                connection.commit()
                try:
                    target = script.get_revision(revision)
                except CommandError as exc:
                    result.error = str(exc)
                    result.error_type = type(exc).__name__
                    return result
                result.resolved_target = target.revision
                for next_revision in _upgrade_path(
                    script, result.start_revision, target.revision
                ):
                    if stop_check is not None and stop_check(next_revision):
                        raise TrialCancelled(
                            "cancelled by stop point before applying "
                            f"revision {next_revision}"
                        )
                    command.upgrade(cfg, next_revision)
                    result.applied_revisions += (next_revision,)
                result.status = STATUS_SUCCESS
            except TrialCancelled as exc:
                connection.rollback()
                result.status = STATUS_CANCELLED
                result.error = str(exc)
                result.error_type = type(exc).__name__
            except Exception as exc:
                connection.rollback()
                result.status = STATUS_FAILED
                result.error = str(exc)
                result.error_type = type(exc).__name__
            finally:
                if previous is _MISSING:
                    cfg.attributes.pop(TRIAL_CONNECTION_ATTRIBUTE, None)
                else:
                    cfg.attributes[TRIAL_CONNECTION_ATTRIBUTE] = previous
            try:
                result.end_revision = _current_revision(connection)
            except Exception:
                result.end_revision = None
        return result
    finally:
        if engine is not None:
            engine.dispose()
        for conn in (copy_conn, source_conn):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        shutil.rmtree(workdir, ignore_errors=True)
