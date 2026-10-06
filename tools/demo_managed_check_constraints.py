# flake8: noqa: I100
"""End-to-end demonstration of explicitly managed CHECK constraints.

Runs the full autogenerate -> upgrade -> downgrade cycle against a
throwaway SQLite database using the real Alembic command API, and
verifies the constraint behavior at each step.  Completes in a few
seconds (well under the 90 second budget).

Run from the repository root::

    python tools/demo_managed_check_constraints.py

"""

import os
import sys
import tempfile
import time

# ensure the alembic sources in this repository are used, rather than
# any installed copy
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

import alembic  # noqa: E402

if not os.path.abspath(os.path.dirname(alembic.__file__)).startswith(
    _REPO_ROOT
):
    raise SystemExit(
        f"demo must run against the repository's alembic sources; "
        f"got {alembic.__file__}"
    )

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

ENV_PY = """
from sqlalchemy import create_engine
from sqlalchemy import CheckConstraint
from sqlalchemy import Column
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import Table

from alembic import context

config = context.config

# the *target* schema: ck_price is tightened from price >= 0 to price > 0
target_metadata = MetaData()
Table(
    "item",
    target_metadata,
    Column("id", Integer, primary_key=True),
    Column("price", Integer),
    CheckConstraint("price > 0", name="ck_price"),
)

# the constraints autogenerate should track, per table
MANAGED = {"item": ["ck_price"]}


def run_migrations_offline():
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        managed_check_constraints=MANAGED,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = create_engine(config.get_main_option("sqlalchemy.url"))
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            managed_check_constraints=MANAGED,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
"""


def _insert(conn, value):
    conn.execute(text("INSERT INTO item (price) VALUES (:v)"), {"v": value})
    conn.commit()


def _expect_insert_fails(conn, value, step):
    try:
        _insert(conn, value)
    except IntegrityError:
        conn.rollback()
        print(f"  ok: INSERT price={value} rejected ({step})")
    else:
        raise SystemExit(
            f"FAIL: INSERT price={value} should have been rejected ({step})"
        )


def _expect_insert_ok(conn, value, step):
    try:
        _insert(conn, value)
    except IntegrityError:
        conn.rollback()
        raise SystemExit(
            f"FAIL: INSERT price={value} should have been allowed ({step})"
        )
    else:
        print(f"  ok: INSERT price={value} allowed ({step})")


def main():
    started = time.time()
    tmp = tempfile.mkdtemp(prefix="alembic_managed_ck_demo_")
    db_url = "sqlite:///" + os.path.join(tmp, "demo.db").replace("\\", "/")

    print("step 1: create database with ck_price CHECK (price >= 0)")
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE item ("
                "id INTEGER NOT NULL PRIMARY KEY, price INTEGER, "
                "CONSTRAINT ck_price CHECK (price >= 0))"
            )
        )
    with engine.connect() as conn:
        _expect_insert_ok(conn, 0, "initial state allows price >= 0")
        conn.execute(text("DELETE FROM item"))
        conn.commit()

    print("step 2: set up migration environment")
    script_dir = os.path.join(tmp, "migrations")
    cfg = Config(os.path.join(tmp, "alembic.ini"))
    cfg.set_main_option("script_location", script_dir)
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.init(cfg, script_dir)
    with open(os.path.join(script_dir, "env.py"), "w") as f:
        f.write(ENV_PY)

    print("step 3: generate migration (alembic revision --autogenerate)")
    script = command.revision(
        cfg, message="tighten ck_price", autogenerate=True
    )
    with open(script.path) as f:
        generated = f.read()
    print("-" * 70)
    print(generated)
    print("-" * 70)
    assert "batch_alter_table" in generated, "expected SQLite batch rendering"
    assert "price > 0" in generated
    assert "price >= 0" in generated, "downgrade must use original expression"

    print("step 4: upgrade (alembic upgrade head)")
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        _expect_insert_fails(conn, 0, "after upgrade, price > 0 enforced")
        _expect_insert_ok(conn, 5, "after upgrade")
        conn.execute(text("DELETE FROM item"))
        conn.commit()

    print("step 5: downgrade (alembic downgrade base)")
    command.downgrade(cfg, "base")
    with engine.connect() as conn:
        _expect_insert_ok(conn, 0, "after downgrade, original restored")
        _expect_insert_fails(conn, -1, "after downgrade, price >= 0 enforced")
        conn.execute(text("DELETE FROM item"))
        conn.commit()

    print("step 6: upgrade again, behavior is repeatable")
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        _expect_insert_fails(conn, 0, "after re-upgrade, price > 0 enforced")

    elapsed = time.time() - started
    print(f"DEMO OK in {elapsed:.1f}s (budget: 90s)")


if __name__ == "__main__":
    main()
