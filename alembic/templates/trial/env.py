"""Alembic environment for use with :func:`alembic.trial.run_sqlite_trial`.

The trial runner hands this script a connection to a *temporary copy*
of the source database, via ``config.attributes["connection"]``.
This script deliberately refuses to create its own engine from the
configured ``sqlalchemy.url``:  doing so would run the migrations
against the real database, which is exactly what a trial run must
never do.

"""

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = None

# the connection to the temporary database copy, supplied by
# alembic.trial.run_sqlite_trial.  This environment never connects to
# the configured sqlalchemy.url on its own.
connection = config.attributes.get("connection")
if connection is None:
    raise RuntimeError(
        "the trial env.py requires a connection to be supplied via "
        "config.attributes['connection']; refusing to connect to the "
        "configured sqlalchemy.url directly"
    )

context.configure(
    connection=connection,
    target_metadata=target_metadata,
    # the trial connection is set up for transactional DDL (pysqlite's
    # implicit commit-before-DDL behavior is disabled on it), so the
    # whole migration stream runs in a single transaction that is
    # rolled back if any step fails.
    transactional_ddl=True,
)

with context.begin_transaction():
    context.run_migrations()
