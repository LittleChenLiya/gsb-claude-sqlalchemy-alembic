"""Database-free example: print an operation tree before and after a rename.

This script demonstrates the logical table rename feature without any
database connection.  It builds an operation tree, prints it, plans a
rename, applies it to a copy, and prints the result.

Run with::

    python tools/print_op_tree_example.py

"""

from __future__ import annotations

from sqlalchemy import Column
from sqlalchemy import Integer
from sqlalchemy import String

from alembic.operations import apply_table_rename
from alembic.operations import op_tree_to_str
from alembic.operations import plan_table_rename
from alembic.operations.ops import AddColumnOp
from alembic.operations.ops import CreateForeignKeyOp
from alembic.operations.ops import CreateIndexOp
from alembic.operations.ops import CreateTableOp
from alembic.operations.ops import DowngradeOps
from alembic.operations.ops import DropTableOp
from alembic.operations.ops import MigrationScript
from alembic.operations.ops import UpgradeOps


def build_tree() -> MigrationScript:
    upgrade = UpgradeOps(
        [
            CreateTableOp(
                "orders",
                [Column("id", Integer), Column("name", String(50))],
                schema="sales",
            ),
            AddColumnOp(
                "orders", Column("email", String(100)), schema="sales"
            ),
            CreateIndexOp(
                "ix_orders_name", "orders", ["name"], schema="sales"
            ),
            CreateForeignKeyOp(
                "fk_orders_cust",
                "orders",
                "customers",
                ["cust_id"],
                ["id"],
                source_schema="sales",
                referent_schema="sales",
            ),
            # public.orders is in a different schema and must not be touched
            CreateTableOp(
                "orders", [Column("id", Integer)], schema="public"
            ),
        ]
    )
    downgrade = DowngradeOps(
        [
            DropTableOp("orders", schema="sales"),
        ]
    )
    return MigrationScript("abc123", upgrade, downgrade)


def main() -> None:
    tree = build_tree()

    print("=== BEFORE (original tree) ===")
    print(op_tree_to_str(tree))
    print()

    # Plan is read-only; the tree is not modified.
    plan = plan_table_rename(tree, "orders", "purchases", schema="sales")
    print("=== PLAN ===")
    print(plan)
    print()

    # Apply to a deep copy; the original tree is left untouched.
    new_tree = apply_table_rename(
        tree, "orders", "purchases", schema="sales"
    )

    print("=== AFTER (renamed copy) ===")
    print(op_tree_to_str(new_tree))
    print()

    print("=== ORIGINAL (unchanged) ===")
    print(op_tree_to_str(tree))


if __name__ == "__main__":
    main()
