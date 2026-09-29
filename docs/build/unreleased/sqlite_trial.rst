.. change::
    :tags: feature, sqlite

    Added :mod:`alembic.trial`, a reusable entry point that trial-runs
    an Alembic migration environment against a disposable copy of an
    existing SQLite database.  The source database is opened read-only
    and copied with the SQLite online backup API (so un-checkpointed
    WAL data is included), migrations run on an injected connection
    that refuses ``ATTACH`` and extension loading, and the temporary
    copy is always cleaned up.  Includes a companion
    :mod:`alembic.trial.env` environment and a
    ``python -m alembic.trial`` command line entry point.

