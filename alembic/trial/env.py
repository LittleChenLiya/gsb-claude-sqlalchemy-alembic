"""Companion ``env.py`` for use with :mod:`alembic.trial`.

A migration environment that should be trial-run must execute its
migrations on the connection injected by the trial runner, and must
not create its own engine or fall back to a configured database URL --
otherwise the "trial" would run against the real database.

To use it, make the ``env.py`` of the migration environment read::

    from alembic.trial.env import run_migrations

    run_migrations(target_metadata=target_metadata)

:func:`run_migrations` raises :class:`RuntimeError` if no connection
was injected, so the environment fails loudly instead of silently
connecting somewhere else.

"""

from __future__ import annotations

from typing import Any
from typing import Optional

from alembic import context
from alembic.trial import TRIAL_CONNECTION_ATTRIBUTE


def run_migrations(
    target_metadata: Optional[Any] = None, **configure_kw: Any
) -> None:
    """Run migrations on the connection injected by the trial runner."""
    config = context.config
    connection = (
        config.attributes.get(TRIAL_CONNECTION_ATTRIBUTE)
        if config is not None
        else None
    )
    if connection is None:
        raise RuntimeError(
            "alembic.trial requires the trial runner to inject its "
            "connection via "
            f"config.attributes[{TRIAL_CONNECTION_ATTRIBUTE!r}]; "
            "refusing to create an engine or use a configured URL, "
            "which could run the migration against the real database."
        )
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        **configure_kw,
    )
    with context.begin_transaction():
        context.run_migrations()
