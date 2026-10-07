"""Demonstrate autogenerate of explicitly managed CHECK constraints.

Creates an in-memory SQLite database holding a table ``items`` with a
managed CHECK constraint ``ck_price CHECK (price >= 0)``, then:

1. **generates** upgrade/downgrade migration code against a target
   metadata that changes the constraint to ``CHECK (price > 0)``,
2. runs the **upgrade** and shows that ``INSERT ... VALUES (0)`` is now
   rejected,
3. runs the **downgrade** and shows that the insert is accepted again,
4. re-runs the **upgrade** to show the restored behavior can be changed
   again, and that autogenerate then reports no further diff.

Run from the repository root (completes in a few seconds)::

    python tools/demo_managed_check_constraints.py
"""

from pathlib import Path
import sys
import time

# prefer the alembic sources from this checkout over any installed copy
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    import sqlalchemy as sa
    from sqlalchemy import CheckConstraint
    from sqlalchemy import Column
    from sqlalchemy import create_engine
    from sqlalchemy import Integer
    from sqlalchemy import MetaData
    from sqlalchemy import Table
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from alembic.autogenerate import api
    from alembic.autogenerate import produce_migrations
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    def items_metadata(expr):
        m = MetaData()
        Table(
            "items",
            m,
            Column("id", Integer, primary_key=True),
            Column("price", Integer),
            CheckConstraint(expr, name="ck_price"),
        )
        return m

    def run_generated(code, conn):
        ctx = MigrationContext.configure(conn)
        op = Operations(ctx)
        ns = {"op": op, "sa": sa}
        exec("def migrate():\n" + code, ns)
        ns["migrate"]()

    def zero_insertable(conn):
        try:
            conn.execute(text("INSERT INTO items (price) VALUES (0)"))
        except IntegrityError:
            conn.rollback()
            return False
        else:
            conn.rollback()
            return True

    def report(step, conn):
        print(
            "%-38s INSERT 0 into items: %s"
            % (step, "accepted" if zero_insertable(conn) else "REJECTED")
        )

    started = time.time()

    engine = create_engine("sqlite://")
    items_metadata("price >= 0").create_all(engine)

    target = items_metadata("price > 0")
    opts = {
        "target_metadata": target,
        "managed_check_constraints": {"items": ["ck_price"]},
    }

    print("=== 1. generate (autogenerate against target metadata) ===")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts=opts)
        script = produce_migrations(ctx, target)
        upgrade_code = api.render_python_code(
            script.upgrade_ops, migration_context=ctx
        )
        downgrade_code = api.render_python_code(
            script.downgrade_ops, migration_context=ctx
        )

    print("--- upgrade() ---")
    print(upgrade_code)
    print("--- downgrade() ---")
    print(downgrade_code)

    with engine.connect() as conn:
        report("before migration:", conn)

        print("=== 2. upgrade (price >= 0  ->  price > 0) ===")
        run_generated(upgrade_code, conn)
        report("after upgrade:", conn)

        print("=== 3. downgrade (restores price >= 0) ===")
        run_generated(downgrade_code, conn)
        report("after downgrade:", conn)

        print("=== 4. re-upgrade ===")
        run_generated(upgrade_code, conn)
        report("after re-upgrade:", conn)

    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts=opts)
        remaining = produce_migrations(ctx, target).upgrade_ops.as_diffs()
        print("remaining autogenerate diff after upgrade: %r" % (remaining,))

    print("completed in %.2f seconds" % (time.time() - started,))


if __name__ == "__main__":
    main()
