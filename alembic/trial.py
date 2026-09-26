"""Trial-run Alembic migrations against a disposable copy of a SQLite
database.

This module provides :func:`run_sqlite_trial`, a reusable entry point
that runs a migration stream against a consistent, throwaway copy of an
existing SQLite database, leaving the original database untouched.

A trial run works as follows:

1.  The source database is opened **read-only** and copied using the
    `SQLite backup API <https://www.sqlite.org/backup.html>`_ through
    :meth:`sqlite3.Connection.backup`.  Unlike copying the main
    database file directly, the backup API produces a consistent
    snapshot that includes committed data still living in the WAL file,
    and it tolerates other connections reading the source database
    concurrently.

2.  The copy is placed inside a freshly created temporary directory and
    the Alembic migration environment is run against a connection to
    the *copy*.  The companion ``env.py`` (see the ``trial``
    template in ``alembic/templates/trial/env.py``) requires this
    connection to be handed to it via
    ``config.attributes["connection"]`` and refuses to connect to any
    configured database URL on its own.

3.  On the connection used for the trial, ``ATTACH DATABASE`` and
    extension loading (the ``load_extension()`` SQL function) are
    refused through the SQLite authorizer hook, so database-level side
    effects of the migration are confined to the copied database.

4.  When the run finishes - whether it succeeded, failed, or was
    cancelled - all connections are closed and the temporary copy is
    deleted, so no file locks or stray files remain.

.. note::

    The isolation described above applies to **database operations**
    performed on the connection handed to the migration environment.  A
    migration script is still arbitrary Python code; this module does
    **not** place migration code in a security sandbox.  Only trial-run
    migrations from sources you trust.

"""

from __future__ import annotations

import contextlib
import dataclasses
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import traceback as traceback_module
from typing import TYPE_CHECKING
from typing import Any
from typing import Callable
from typing import Dict
from typing import Iterator
from typing import Optional
from typing import Tuple
from typing import Union
import urllib.parse

from sqlalchemy import create_engine
from sqlalchemy import event

from .runtime.environment import EnvironmentContext
from .runtime.migration import MigrationContext
from .script import ScriptDirectory

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

    from .config import Config


#: Hook event emitted after the consistent backup of the source
#: database has been created, before migrations run.
TRIAL_AFTER_BACKUP = "after_backup"

#: Hook event emitted after migrations completed successfully.
TRIAL_AFTER_MIGRATE = "after_migrate"

#: Hook event emitted after migrations failed, before the temporary
#: copy is cleaned up.
TRIAL_AFTER_FAILURE = "after_failure"

#: Signature of trial run hooks.  Receives the event name (one of
#: TRIAL_AFTER_BACKUP, TRIAL_AFTER_MIGRATE, TRIAL_AFTER_FAILURE) and a
#: dictionary of run information.
TrialHook = Callable[[str, Dict[str, Any]], None]


class TrialCancelled(Exception):
    """Raised by a trial hook (or the caller) to cancel a trial run.

    The exception propagates out of :func:`run_sqlite_trial` only
    after all connections have been closed and the temporary copy has
    been removed.
    """


@dataclasses.dataclass
class TrialResult:
    """Reports the outcome of :func:`run_sqlite_trial`."""

    source_database: str
    """Path of the source database that was copied."""

    revision: str
    """The target revision the trial run attempted to reach."""

    success: bool
    """True if the migration stream ran to completion on the copy."""

    starting_revisions: Tuple[str, ...]
    """Revision(s) present on the copy before migrations ran."""

    ending_revisions: Optional[Tuple[str, ...]]
    """Revision(s) present on the copy after a successful run.

    None if the run did not complete; a failed run is never reported
    as if the migration completed.
    """

    error: Optional[str] = None
    """Short description of the failure, if any."""

    error_traceback: Optional[str] = None
    """Full traceback of the failure, if any."""

    def __str__(self) -> str:
        start = ", ".join(self.starting_revisions) or "<base>"
        if self.success:
            end = ", ".join(self.ending_revisions or ()) or "<base>"
            return (
                f"trial run of {self.source_database} to revision "
                f"{self.revision!r}: SUCCESS ({start} -> {end})"
            )
        return (
            f"trial run of {self.source_database} to revision "
            f"{self.revision!r}: FAILED (started at {start}): "
            f"{self.error}"
        )


# SQLite authorizer action codes; the sqlite3 module only exposes the
# constants on Python 3.11+, so fall back to the well-known values.
_SQLITE_ATTACH = getattr(sqlite3, "SQLITE_ATTACH", 24)
_SQLITE_FUNCTION = getattr(sqlite3, "SQLITE_FUNCTION", 31)


def run_sqlite_trial(
    source_database: Union[str, Path],
    config: "Config",
    revision: str = "head",
    *,
    temporary_directory: Union[str, Path, None] = None,
    hook: Optional[TrialHook] = None,
) -> TrialResult:
    """Run a migration stream against a throwaway copy of a SQLite
    database and report the outcome.

    The source database is never modified:  it is opened read-only,
    copied via the SQLite backup API, and all migration activity happens
    against the copy.

    :param source_database: path to the source SQLite database file.
    :param config: an :class:`alembic.config.Config` identifying the
     migration environment (at minimum its ``script_location``).
     The environment's ``env.py`` must use the connection supplied
     in ``config.attributes["connection"]``; see the ``trial``
     template in ``alembic/templates/trial/env.py`` for a
     ready-to-use example.
    :param revision: target revision, e.g. ``"head"`` or a specific
     revision id.
    :param temporary_directory: optional parent directory for the
     temporary working directory that receives the database copy.
     Defaults to the system temporary directory.  The working directory
     itself is always removed when the run ends.
    :param hook: optional callable invoked as ``hook(event, info)``
     at the TRIAL_AFTER_* stages of the run.  ``info`` carries keys
     such as ``copy_path``, ``starting_revisions`` and
     ``ending_revisions``.  Raising from the hook (for example
     :class:`TrialCancelled`) cancels the run; the exception
     propagates after cleanup has completed.

    :return: a :class:`TrialResult` describing the starting and
     ending revisions, success or failure, and error details.  A failed
     run is never reported as a completed migration.

    """
    source_path = Path(source_database).expanduser().absolute()
    if not source_path.is_file():
        raise FileNotFoundError(
            f"source database {source_path} does not exist"
        )

    work_dir = Path(
        tempfile.mkdtemp(prefix="alembic_trial_", dir=temporary_directory)
    )
    copy_path = work_dir / source_path.name
    info: Dict[str, Any] = {
        "source_path": source_path,
        "copy_path": copy_path,
        "revision": revision,
    }
    try:
        _backup_database(source_path, copy_path)
        _emit_hook(hook, TRIAL_AFTER_BACKUP, info)

        with _trial_connection(copy_path) as connection:
            starting_revisions = _current_heads(connection)
            info["starting_revisions"] = starting_revisions
            try:
                _run_migrations(config, connection, revision)
            except Exception as err:
                info["error"] = err
                _emit_hook(hook, TRIAL_AFTER_FAILURE, info)
                return TrialResult(
                    source_database=str(source_path),
                    revision=revision,
                    success=False,
                    starting_revisions=starting_revisions,
                    ending_revisions=None,
                    error=f"{type(err).__name__}: {err}",
                    error_traceback="".join(
                        traceback_module.format_exception(err)
                    ),
                )
            ending_revisions = _current_heads(connection)
            info["ending_revisions"] = ending_revisions
            _emit_hook(hook, TRIAL_AFTER_MIGRATE, info)
            return TrialResult(
                source_database=str(source_path),
                revision=revision,
                success=True,
                starting_revisions=starting_revisions,
                ending_revisions=ending_revisions,
            )
    finally:
        try:
            shutil.rmtree(work_dir)
        except OSError:
            # don't mask an exception that is already propagating;
            # otherwise surface the cleanup failure itself.
            if sys.exc_info()[0] is None:
                raise


def _emit_hook(
    hook: Optional[TrialHook], event: str, info: Dict[str, Any]
) -> None:
    if hook is not None:
        hook(event, dict(info))


def _read_only_uri(path: Path) -> str:
    return "file:%s?mode=ro" % urllib.parse.quote(path.as_posix())


def _backup_database(source_path: Path, copy_path: Path) -> None:
    """Copy source_path to copy_path using the SQLite backup API.

    The source is opened read-only; the backup API reads a consistent
    snapshot including committed data that still resides in the WAL
    file, even while other connections hold the source database open.
    """
    source = sqlite3.connect(_read_only_uri(source_path), uri=True)
    try:
        destination = sqlite3.connect(str(copy_path))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def _restrict_connection(dbapi_connection: sqlite3.Connection) -> None:
    """Refuse ATTACH and extension loading on a DBAPI connection."""
    # extension loading is disabled by default; make that explicit.
    dbapi_connection.enable_load_extension(False)

    def authorizer(
        action: int,
        arg1: Optional[str],
        arg2: Optional[str],
        dbname: Optional[str],
        source: Optional[str],
    ) -> int:
        if action == _SQLITE_ATTACH:
            return sqlite3.SQLITE_DENY
        if (
            action == _SQLITE_FUNCTION
            and arg2 is not None
            and arg2.lower() == "load_extension"
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    dbapi_connection.set_authorizer(authorizer)


@contextlib.contextmanager
def _trial_connection(copy_path: Path) -> "Iterator[Connection]":
    """Yield a SQLAlchemy connection to the database copy.

    The underlying DBAPI connection refuses ATTACH and extension
    loading, and everything is closed on exit so no file locks remain.
    """
    dbapi_connection = sqlite3.connect(str(copy_path))
    # take over transaction control from pysqlite:  its default
    # isolation_level implicitly commits before DDL statements, which
    # would make it impossible to roll a failed migration stream back
    # on the copy.
    dbapi_connection.isolation_level = None
    _restrict_connection(dbapi_connection)
    engine = create_engine(
        "sqlite+pysqlite://", creator=lambda: dbapi_connection
    )

    @event.listens_for(engine, "begin")
    def _begin(connection: "Connection") -> None:
        connection.exec_driver_sql("BEGIN")

    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()
        dbapi_connection.close()


def _current_heads(connection: "Connection") -> Tuple[str, ...]:
    heads = MigrationContext.configure(connection).get_current_heads()
    # the read above autobegins a transaction on the connection; roll
    # it back so the migration environment starts from a clean slate.
    if connection.in_transaction():
        connection.rollback()
    return heads


def _run_migrations(
    config: "Config", connection: "Connection", revision: str
) -> None:
    script = ScriptDirectory.from_config(config)

    previous = config.attributes.get("connection")
    config.attributes["connection"] = connection
    try:

        def upgrade(rev: Any, context: Any) -> Any:
            return script._upgrade_revs(revision, rev)

        with EnvironmentContext(
            config, script, fn=upgrade, destination_rev=revision
        ):
            script.run_env()
    finally:
        if previous is None:
            config.attributes.pop("connection", None)
        else:
            config.attributes["connection"] = previous
