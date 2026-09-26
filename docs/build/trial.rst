.. _trial:

===========================
Trial-Running Migrations
===========================

.. module:: alembic.trial

Sometimes it is useful to check whether a migration stream applies
cleanly to a *copy* of a real database before running it against the
real thing.  The :func:`run_sqlite_trial` function provides a reusable
entry point for exactly this against SQLite databases::

    from alembic.config import Config
    from alembic.trial import run_sqlite_trial

    config = Config("alembic.ini")
    result = run_sqlite_trial("/path/to/production-copy.db", config, "head")
    print(result)

A trial run:

* opens the source database **read-only** and copies it with the
  `SQLite backup API <https://www.sqlite.org/backup.html>`_, so the
  snapshot is consistent, includes committed data that still resides
  in the WAL file, and tolerates other connections reading the source
  database at the same time.  The main database file is never copied
  directly and never modified.
* runs the migration environment against a connection to the
  temporary copy.  The environment's ``env.py`` must use this
  connection, supplied as ``config.attributes["connection"]``; the
  ``trial`` environment template (usable with ``alembic init -t
  trial``, or copied from ``alembic/templates/trial/env.py``) provides
  an ``env.py`` that requires the supplied connection and refuses to
  connect to the configured ``sqlalchemy.url`` on its own.
* refuses ``ATTACH DATABASE`` and extension loading
  (``load_extension()``) on the trial connection, so database-level
  side effects are confined to the copy.
* reports the starting and ending revisions, success or failure, and
  error details in the returned :class:`TrialResult`.  A failed run is
  never reported as a completed migration.
* closes all connections and deletes the temporary copy when the run
  ends, whether it succeeded, failed, or was cancelled, so no file
  locks or stray files remain.

Cancellation and progress observation are available through the
``hook`` parameter, a callable invoked as ``hook(event, info)`` at the
``TRIAL_AFTER_BACKUP``, ``TRIAL_AFTER_MIGRATE`` and
``TRIAL_AFTER_FAILURE`` stages.  Raising from the hook, for example
:class:`TrialCancelled`, cancels the run; the exception propagates
after cleanup has completed::

    from alembic.trial import TRIAL_AFTER_BACKUP, TrialCancelled

    def stop_before_migrating(event, info):
        if event == TRIAL_AFTER_BACKUP:
            raise TrialCancelled("not today")

    run_sqlite_trial("data.db", config, "head", hook=stop_before_migrating)

.. note::

    The isolation described here applies to **database operations**
    performed on the connection handed to the migration environment.
    A migration script is still arbitrary Python code; a trial run
    does **not** place migration code in a security sandbox.  Only
    trial-run migrations from sources you trust.

API
---

.. autofunction:: run_sqlite_trial

.. autoclass:: TrialResult
    :members:

.. autoclass:: TrialCancelled

.. autodata:: TRIAL_AFTER_BACKUP

.. autodata:: TRIAL_AFTER_MIGRATE

.. autodata:: TRIAL_AFTER_FAILURE
