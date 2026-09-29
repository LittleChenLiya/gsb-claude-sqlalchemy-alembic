============================
Trial-Running Migrations on SQLite
============================

.. versionadded:: 1.21

Alembic ships a small helper, :mod:`alembic.trial`, that **trial-runs**
a migration environment against a *disposable copy* of an existing
SQLite database.  This allows a migration to be rehearsed against real
data with the guarantee that the original database is never modified,
whether the migration succeeds, fails, or is cancelled.

How it works
============

Given the path to an existing SQLite database, an Alembic
configuration, and a target revision, :func:`~alembic.trial.run_sqlite_trial`:

* opens the source database **read-only** (SQLite URI ``mode=ro``);
  the trial never writes to the original file, its WAL, or its
  shared-memory files;
* produces a consistent copy using the SQLite **online backup API**
  (:meth:`sqlite3.Connection.backup`).  The main database file is never
  copied directly, so committed data that has not been checkpointed out
  of the WAL is not lost;
* hands the copy to the migration environment as an **injected
  connection**.  On that connection, ``ATTACH DATABASE`` and extension
  loading are refused through a SQLite authorizer, so migrations cannot
  reach outside the copied database via SQL;
* reports the start and end revisions, the revisions applied on the
  copy, whether the run succeeded, failed, or was cancelled, and any
  error that occurred.  A failed or cancelled trial is **never**
  reported as a completed migration;
* closes all connections and deletes the temporary copy when finished
  -- including on migration errors and cancellation -- leaving no file
  locks behind.

.. note::

    The isolation guarantee covers only database operations performed
    on the injected connection.  A migration file is ordinary Python
    code; the trial runner does **not** sandbox arbitrary Python.  A
    migration that opens its own connections, writes files, or touches
    the network can still affect the outside world.

The migration environment
=========================

The migration environment must run its migrations on the injected
connection and must not create its own engine.  A ready-made
``env.py`` is provided; point the environment's ``env.py`` at it::

    from alembic.trial.env import run_migrations

    run_migrations(target_metadata=target_metadata)

:func:`alembic.trial.env.run_migrations` raises :class:`RuntimeError`
if no connection was injected, so the environment fails loudly instead
of silently connecting to the real database.

Running a trial
===============

From Python::

    from alembic.trial import run_sqlite_trial

    result = run_sqlite_trial(
        "app.db", "alembic.ini", "head"
    )
    print(result.format())
    assert result.ok, result.error

Or from the command line::

    python -m alembic.trial app.db --config alembic.ini --revision head

The command exits with status ``0`` on success, ``1`` on failure, and
``2`` when the trial was cancelled through the injected stop point.

Cancellation can be rehearsed by injecting a stop point: pass
``stop_check``, a callable receiving the next revision id before each
migration step; returning ``True`` cancels the trial::

    result = run_sqlite_trial(
        "app.db",
        "alembic.ini",
        "head",
        stop_check=lambda next_revision: next_revision == "0002",
    )
    assert result.status == "cancelled"

The command line equivalent is ``--stop-after N``, which cancels the
trial before the (N+1)-th migration step.

In all outcomes -- success, failure, or cancellation -- the original
database remains readable and writable, and the temporary working
directory is removed.

API
===

.. autofunction:: alembic.trial.run_sqlite_trial

.. autoclass:: alembic.trial.TrialResult
    :members:

.. autofunction:: alembic.trial.env.run_migrations

