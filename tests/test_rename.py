"""Tests for logical table renaming across the operation tree."""

from sqlalchemy import Column
from sqlalchemy import Integer
from sqlalchemy import String

from alembic.operations import apply_table_rename
from alembic.operations import MigrateOperation
from alembic.operations import op_tree_to_str
from alembic.operations import plan_table_rename
from alembic.operations import UnsupportedTableRenameError
from alembic.operations.ops import AddColumnOp
from alembic.operations.ops import AlterColumnOp
from alembic.operations.ops import CreateForeignKeyOp
from alembic.operations.ops import CreateIndexOp
from alembic.operations.ops import CreateTableOp
from alembic.operations.ops import DowngradeOps
from alembic.operations.ops import DropColumnOp
from alembic.operations.ops import DropIndexOp
from alembic.operations.ops import DropTableOp
from alembic.operations.ops import MigrationScript
from alembic.operations.ops import ModifyTableOps
from alembic.operations.ops import UpgradeOps
from alembic.testing import assert_raises
from alembic.testing import eq_
from alembic.testing.fixtures import TestBase


class TableRenameTest(TestBase):
    def _fixture(self):
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
                    "ix_orders_name",
                    "orders",
                    ["name"],
                    schema="sales",
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
                CreateTableOp(
                    "orders", [Column("id", Integer)], schema="public"
                ),
                AddColumnOp(
                    "orders", Column("x", String(10)), schema="public"
                ),
                CreateIndexOp(
                    "ix_orders_x", "orders", ["x"], schema="public"
                ),
            ]
        )
        downgrade = DowngradeOps(
            [
                DropTableOp("orders", schema="sales"),
            ]
        )
        return upgrade, downgrade

    def test_plan_is_read_only(self):
        upgrade, _ = self._fixture()
        plan = plan_table_rename(
            upgrade, "orders", "purchases", schema="sales"
        )

        eq_(plan.old_identity, "sales.orders")
        eq_(plan.new_identity, "sales.purchases")
        eq_(len(plan), 4)
        eq_(
            [type(op).__name__ for op in plan],
            [
                "CreateTableOp",
                "AddColumnOp",
                "CreateIndexOp",
                "CreateForeignKeyOp",
            ],
        )
        for op in upgrade.ops[:4]:
            name = (
                op.table_name
                if hasattr(op, "table_name")
                else op.source_table
            )
            eq_(name, "orders")
        for op in upgrade.ops[4:]:
            eq_(op.table_name, "orders")
            eq_(op.schema, "public")

    def test_apply_renames_matching_identity(self):
        upgrade, _ = self._fixture()
        new_upgrade = apply_table_rename(
            upgrade, "orders", "purchases", schema="sales"
        )

        ops = new_upgrade.ops
        eq_(ops[0].table_name, "purchases")
        eq_(ops[0].schema, "sales")
        eq_(ops[1].table_name, "purchases")
        eq_(ops[1].schema, "sales")
        eq_(ops[2].table_name, "purchases")
        eq_(ops[2].schema, "sales")
        eq_(ops[3].source_table, "purchases")
        eq_(ops[3].kw["source_schema"], "sales")
        eq_(ops[4].table_name, "orders")
        eq_(ops[4].schema, "public")
        eq_(ops[5].table_name, "orders")
        eq_(ops[5].schema, "public")
        eq_(ops[6].table_name, "orders")
        eq_(ops[6].schema, "public")

    def test_apply_leaves_original_unchanged(self):
        upgrade, _ = self._fixture()
        original_ops = list(upgrade.ops)
        apply_table_rename(upgrade, "orders", "purchases", schema="sales")

        for orig, op in zip(original_ops, upgrade.ops):
            assert orig is op
        eq_(upgrade.ops[0].table_name, "orders")
        eq_(upgrade.ops[1].table_name, "orders")
        eq_(upgrade.ops[3].source_table, "orders")

    def test_apply_returns_independent_objects(self):
        upgrade, _ = self._fixture()
        new_upgrade = apply_table_rename(
            upgrade, "orders", "purchases", schema="sales"
        )

        for orig, new in zip(upgrade.ops, new_upgrade.ops):
            assert orig is not new

        orig_col = upgrade.ops[1].column
        new_col = new_upgrade.ops[1].column
        assert orig_col is not new_col
        eq_(new_col.name, "email")

        upgrade.ops[1].info["note"] = "original"
        new_upgrade2 = apply_table_rename(
            upgrade, "orders", "purchases", schema="sales"
        )
        new_upgrade2.ops[1].info["note"] = "changed"
        eq_(upgrade.ops[1].info["note"], "original")

    def test_foreign_key_referent_renamed(self):
        upgrade = UpgradeOps(
            [
                CreateForeignKeyOp(
                    "fk_ref",
                    "order_items",
                    "orders",
                    ["order_id"],
                    ["id"],
                    source_schema="sales",
                    referent_schema="sales",
                ),
            ]
        )
        new_upgrade = apply_table_rename(
            upgrade, "orders", "purchases", schema="sales"
        )
        op = new_upgrade.ops[0]
        eq_(op.referent_table, "purchases")
        eq_(op.source_table, "order_items")
        eq_(op.kw["referent_schema"], "sales")
        eq_(op.kw["source_schema"], "sales")

    def test_foreign_key_self_reference(self):
        upgrade = UpgradeOps(
            [
                CreateForeignKeyOp(
                    "fk_self",
                    "orders",
                    "orders",
                    ["parent_id"],
                    ["id"],
                    source_schema="sales",
                    referent_schema="sales",
                ),
            ]
        )
        new_upgrade = apply_table_rename(
            upgrade, "orders", "purchases", schema="sales"
        )
        op = new_upgrade.ops[0]
        eq_(op.source_table, "purchases")
        eq_(op.referent_table, "purchases")

    def test_drop_column_and_drop_index_renamed(self):
        upgrade = UpgradeOps(
            [
                DropColumnOp(
                    "orders", Column("a", String(10)), schema="sales"
                ),
                DropIndexOp("ix_a", "orders", schema="sales"),
            ]
        )
        new_upgrade = apply_table_rename(
            upgrade, "orders", "purchases", schema="sales"
        )
        eq_(new_upgrade.ops[0].table_name, "purchases")
        eq_(new_upgrade.ops[1].table_name, "purchases")

    def test_drop_table_renamed(self):
        upgrade = UpgradeOps([DropTableOp("orders", schema="sales")])
        new_upgrade = apply_table_rename(
            upgrade, "orders", "purchases", schema="sales"
        )
        eq_(new_upgrade.ops[0].table_name, "purchases")
        eq_(upgrade.ops[0].table_name, "orders")

    def test_modify_table_ops_container_renamed(self):
        modify = ModifyTableOps(
            "orders",
            [
                AddColumnOp(
                    "orders", Column("b", String(10)), schema="sales"
                ),
                CreateIndexOp("ix_b", "orders", ["b"], schema="sales"),
            ],
            schema="sales",
        )
        upgrade = UpgradeOps([modify])
        new_upgrade = apply_table_rename(
            upgrade, "orders", "purchases", schema="sales"
        )
        new_modify = new_upgrade.ops[0]
        eq_(new_modify.table_name, "purchases")
        eq_(new_modify.ops[0].table_name, "purchases")
        eq_(new_modify.ops[1].table_name, "purchases")
        eq_(modify.table_name, "orders")
        eq_(modify.ops[0].table_name, "orders")

    def test_nested_upgrade_ops(self):
        inner = UpgradeOps(
            [AddColumnOp("orders", Column("c", String(10)), schema="sales")]
        )
        outer = UpgradeOps([inner])
        new_outer = apply_table_rename(
            outer, "orders", "purchases", schema="sales"
        )
        eq_(new_outer.ops[0].ops[0].table_name, "purchases")
        eq_(outer.ops[0].ops[0].table_name, "orders")

    def test_migration_script_root(self):
        upgrade, downgrade = self._fixture()
        script = MigrationScript("abc123", upgrade, downgrade)
        new_script = apply_table_rename(
            script, "orders", "purchases", schema="sales"
        )

        eq_(new_script.upgrade_ops.ops[0].table_name, "purchases")
        eq_(new_script.upgrade_ops.ops[1].table_name, "purchases")
        eq_(new_script.downgrade_ops.ops[0].table_name, "purchases")
        eq_(script.upgrade_ops.ops[0].table_name, "orders")
        eq_(script.downgrade_ops.ops[0].table_name, "orders")

    def test_custom_operation_rejected(self):
        class CustomOp(MigrateOperation):
            def __init__(self, table_name):
                self.table_name = table_name

        upgrade = UpgradeOps(
            [
                AddColumnOp(
                    "orders", Column("a", String(10)), schema="sales"
                ),
                CustomOp("orders"),
            ]
        )
        assert_raises(
            UnsupportedTableRenameError,
            plan_table_rename,
            upgrade,
            "orders",
            "purchases",
            schema="sales",
        )
        eq_(upgrade.ops[0].table_name, "orders")
        eq_(upgrade.ops[1].table_name, "orders")

    def test_custom_operation_rejected_on_apply(self):
        class CustomOp(MigrateOperation):
            def __init__(self, table_name):
                self.table_name = table_name

        upgrade = UpgradeOps(
            [
                AddColumnOp(
                    "orders", Column("a", String(10)), schema="sales"
                ),
                CustomOp("orders"),
            ]
        )
        assert_raises(
            UnsupportedTableRenameError,
            apply_table_rename,
            upgrade,
            "orders",
            "purchases",
            schema="sales",
        )
        eq_(upgrade.ops[0].table_name, "orders")
        eq_(upgrade.ops[1].table_name, "orders")

    def test_builtin_unsupported_operation_rejected(self):
        upgrade = UpgradeOps(
            [AlterColumnOp("orders", "a", schema="sales")]
        )
        assert_raises(
            UnsupportedTableRenameError,
            plan_table_rename,
            upgrade,
            "orders",
            "purchases",
            schema="sales",
        )
        eq_(upgrade.ops[0].table_name, "orders")

    def test_builtin_unsupported_other_table_left_alone(self):
        upgrade = UpgradeOps(
            [AlterColumnOp("other", "a", schema="sales")]
        )
        plan = plan_table_rename(
            upgrade, "orders", "purchases", schema="sales"
        )
        eq_(len(plan), 0)
        new_upgrade = apply_table_rename(
            upgrade, "orders", "purchases", schema="sales"
        )
        eq_(new_upgrade.ops[0].table_name, "other")

    def test_schema_is_part_of_identity(self):
        upgrade = UpgradeOps(
            [AddColumnOp("orders", Column("a", String(10)), schema="sales")]
        )
        plan = plan_table_rename(upgrade, "orders", "purchases")
        eq_(len(plan), 0)

        upgrade2 = UpgradeOps(
            [AddColumnOp("orders", Column("a", String(10)), schema="public")]
        )
        plan2 = plan_table_rename(
            upgrade2, "orders", "purchases", schema="sales"
        )
        eq_(len(plan2), 0)

    def test_schema_less_table_renamed(self):
        upgrade = UpgradeOps(
            [AddColumnOp("orders", Column("a", String(10)))]
        )
        new_upgrade = apply_table_rename(upgrade, "orders", "purchases")
        eq_(new_upgrade.ops[0].table_name, "purchases")
        eq_(new_upgrade.ops[0].schema, None)

    def test_op_tree_to_str(self):
        upgrade, _ = self._fixture()
        output = op_tree_to_str(upgrade)
        assert "UpgradeOps()" in output
        assert "CreateTableOp(sales.orders)" in output
        assert "AddColumnOp(sales.orders)" in output
        assert "CreateIndexOp(sales.orders)" in output
        assert "CreateForeignKeyOp(sales.orders -> sales.customers)" in output
        assert "CreateTableOp(public.orders)" in output
