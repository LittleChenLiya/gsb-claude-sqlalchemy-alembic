.. change::
    :tags: feature, sqlite

    Added :func:`alembic.trial.run_sqlite_trial`, a reusable entry point
    that trial-runs a migration stream against a consistent, throwaway
    copy of an existing SQLite database.  The source database is opened
    read-only and copied with the SQLite backup API (capturing committed
    data still in the WAL file, even while other connections read the
    source), migrations run against the copy on a connection that
    refuses ``ATTACH`` and extension loading, and the temporary copy is
    always cleaned up afterwards.  A companion ``trial`` environment
    template provides an ``env.py`` that requires the supplied
    connection.  The new :doc:`/trial` document describes the feature.
