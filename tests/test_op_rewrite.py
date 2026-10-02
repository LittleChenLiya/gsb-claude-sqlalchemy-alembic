"""Tests for alembic.operations.rewrite: post-generation table renaming
of operation trees.  All tests are database-free."""

from sqlalchemy import Column
from sqlalchemy import Integer
from sqlalchemy import String

from alembic.operations.ops import AddColumnOp
from alembic.operations.ops import CreateForeignKeyOp
from alembic.operations.ops import CreateIndexOp
from alembic.operations.ops import DowngradeOps
from alembic.operations.ops import DropColumnOp
from alembic.operations.ops import DropConstraintOp
from alembic.operations.ops import DropIndexOp
from alembic.operations.ops import MigrateOperation
from alembic.operations.ops import MigrationScript
from alembic.operations.ops import ModifyTableOps
from alembic.operations.ops import UpgradeOps
from alembic.operations.rewrite import format_op_tree
from alembic.operations.rewrite import rename_table
from alembic.operations.rewrite import RenameTablePlan
from alembic.operations.rewrite import TableRenameError
from alembic.operations.rewrite import UnsupportedOperationError
from alembic.testing import assert_raises_message
from alembic.testing import eq_
from alembic.testing import is_
from alembic.testing import is_not_
from alembic.testing import TestBase


class MyCustomOp(MigrateOperation):
    """A custom operation unknown to the renamer."""

    def __init__(self, table_name):
        self.table_name = table_name

    def reverse(self):
        return MyCustomOp(self.table_name)


def _sales_orders_upgrade_ops():
    return UpgradeOps(
        ops=[
            AddColumnOp("orders", Column("total", Integer), schema="sales"),
            DropColumnOp("orders", "legacy_code", schema="sales"),
            CreateIndexOp(
                "ix_orders_total", "orders", ["total"], schema="sales"
            ),
            DropIndexOp("ix_orders_old", table_name="orders", schema="sales"),
        ]
    )


class ColumnAndIndexRenameTest(TestBase):
    """sales.orders -> sales.purchases: column and index operations
    follow, while public.orders is left alone."""

    def test_add_and_drop_column_renamed(self):
        ops = _sales_orders_upgrade_ops()
        new_ops = rename_table(ops, "orders", "purchases", schema="sales")
        eq_([op.table_name for op in new_ops.ops], ["purchases"] * 4)
        eq_([op.schema for op in new_ops.ops], ["sales"] * 4)

    def test_original_tree_not_modified(self):
        ops = _sales_orders_upgrade_ops()
        rename_table(ops, "orders", "purchases", schema="sales")
        eq_([op.table_name for op in ops.ops], ["orders"] * 4)

    def test_other_schema_same_table_name_untouched(self):
        ops = UpgradeOps(
            ops=[
                AddColumnOp(
                    "orders", Column("total", Integer), schema="sales"
                ),
                AddColumnOp(
                    "orders", Column("note", String(50)), schema="public"
                ),
                CreateIndexOp(
                    "ix_orders_note", "orders", ["note"], schema="public"
                ),
                DropIndexOp(
                    "ix_orders_note", table_name="orders", schema="public"
                ),
                # no schema at all: also a different identity
                AddColumnOp("orders", Column("x", Integer)),
            ]
        )
        new_ops = rename_table(ops, "orders", "purchases", schema="sales")
        eq_(
            [(op.table_name, op.schema) for op in new_ops.ops],
            [
                ("purchases", "sales"),
                ("orders", "public"),
                ("orders", "public"),
                ("orders", "public"),
                ("orders", None),
            ],
        )

    def test_rename_without_schema_only_matches_schema_none(self):
        ops = UpgradeOps(
            ops=[
                AddColumnOp("orders", Column("x", Integer)),
                AddColumnOp("orders", Column("y", Integer), schema="sales"),
            ]
        )
        new_ops = rename_table(ops, "orders", "purchases")
        eq_(
            [(op.table_name, op.schema) for op in new_ops.ops],
            [("purchases", None), ("orders", "sales")],
        )

    def test_index_attributes_preserved(self):
        ops = UpgradeOps(
            ops=[
                CreateIndexOp(
                    "ix_orders_total",
                    "orders",
                    ["total", "created"],
                    schema="sales",
                    unique=True,
                    postgresql_using="gin",
                )
            ]
        )
        new_ops = rename_table(ops, "orders", "purchases", schema="sales")
        idx = new_ops.ops[0]
        eq_(idx.index_name, "ix_orders_total")
        eq_(idx.table_name, "purchases")
        eq_(idx.columns, ["total", "created"])
        is_(idx.unique, True)
        eq_(idx.kw, {"postgresql_using": "gin"})

    def test_drop_column_reverse_rewritten(self):
        reverse = AddColumnOp.from_column_and_tablename(
            "sales", "orders", Column("total", Integer)
        )
        ops = UpgradeOps(
            ops=[
                DropColumnOp(
                    "orders", "total", schema="sales", _reverse=reverse
                )
            ]
        )
        new_ops = rename_table(ops, "orders", "purchases", schema="sales")
        drop_op = new_ops.ops[0]
        eq_(drop_op.table_name, "purchases")
        eq_(drop_op._reverse.table_name, "purchases")
        # original reverse op untouched
        eq_(ops.ops[0]._reverse.table_name, "orders")


class ForeignKeyRenameTest(TestBase):
    """Foreign keys referencing sales.orders are retargeted."""

    def test_source_table_renamed(self):
        ops = UpgradeOps(
            ops=[
                CreateForeignKeyOp(
                    "fk_orders_customer",
                    "orders",
                    "customer",
                    ["customer_id"],
                    ["id"],
                    source_schema="sales",
                    referent_schema="sales",
                )
            ]
        )
        new_ops = rename_table(ops, "orders", "purchases", schema="sales")
        fk = new_ops.ops[0]
        eq_(fk.source_table, "purchases")
        eq_(fk.referent_table, "customer")
        eq_(fk.kw["source_schema"], "sales")

    def test_referent_table_renamed(self):
        ops = UpgradeOps(
            ops=[
                CreateForeignKeyOp(
                    "fk_shipment_order",
                    "shipment",
                    "orders",
                    ["order_id"],
                    ["id"],
                    source_schema="sales",
                    referent_schema="sales",
                )
            ]
        )
        new_ops = rename_table(ops, "orders", "purchases", schema="sales")
        fk = new_ops.ops[0]
        eq_(fk.source_table, "shipment")
        eq_(fk.referent_table, "purchases")
        eq_(fk.local_cols, ["order_id"])
        eq_(fk.remote_cols, ["id"])

    def test_fk_to_other_schema_orders_untouched(self):
        ops = UpgradeOps(
            ops=[
                CreateForeignKeyOp(
                    "fk_audit_order",
                    "audit_log",
                    "orders",
                    ["order_id"],
                    ["id"],
                    source_schema="sales",
                    referent_schema="public",
                )
            ]
        )
        new_ops = rename_table(ops, "orders", "purchases", schema="sales")
        fk = new_ops.ops[0]
        eq_(fk.referent_table, "orders")
        eq_(fk.kw["referent_schema"], "public")

    def test_drop_constraint_renamed(self):
        ops = UpgradeOps(
            ops=[
                DropConstraintOp(
                    "fk_orders_customer",
                    "orders",
                    type_="foreignkey",
                    schema="sales",
                ),
                DropConstraintOp(
                    "fk_public_orders",
                    "orders",
                    type_="foreignkey",
                    schema="public",
                ),
            ]
        )
        new_ops = rename_table(ops, "orders", "purchases", schema="sales")
        eq_(
            [(op.table_name, op.schema) for op in new_ops.ops],
            [("purchases", "sales"), ("orders", "public")],
        )

    def test_drop_constraint_reverse_fk_rewritten(self):
        reverse_fk = CreateForeignKeyOp(
            "fk_orders_customer",
            "orders",
            "customer",
            ["customer_id"],
            ["id"],
            source_schema="sales",
            referent_schema="sales",
        )
        ops = UpgradeOps(
            ops=[
                DropConstraintOp(
                    "fk_orders_customer",
                    "orders",
                    type_="foreignkey",
                    schema="sales",
                    _reverse=reverse_fk,
                )
            ]
        )
        new_ops = rename_table(ops, "orders", "purchases", schema="sales")
        drop_op = new_ops.ops[0]
        eq_(drop_op.table_name, "purchases")
        eq_(drop_op._reverse.source_table, "purchases")
        # original reverse op untouched
        eq_(ops.ops[0]._reverse.source_table, "orders")


class NestedContainerTest(TestBase):
    """Nested UpgradeOps / DowngradeOps / ModifyTableOps / MigrationScript
    containers are traversed recursively."""

    def _nested_script(self):
        return MigrationScript(
            "abc123",
            UpgradeOps(
                ops=[
                    ModifyTableOps(
                        "orders",
                        ops=[
                            AddColumnOp(
                                "orders",
                                Column("total", Integer),
                                schema="sales",
                            ),
                            CreateIndexOp(
                                "ix_orders_total",
                                "orders",
                                ["total"],
                                schema="sales",
                            ),
                        ],
                        schema="sales",
                    ),
                    # an arbitrarily nested container
                    UpgradeOps(
                        ops=[
                            DropColumnOp(
                                "orders", "legacy_code", schema="sales"
                            )
                        ],
                        upgrade_token="nested_upgrades",
                    ),
                ]
            ),
            DowngradeOps(
                ops=[
                    ModifyTableOps(
                        "orders",
                        ops=[
                            DropIndexOp(
                                "ix_orders_total",
                                table_name="orders",
                                schema="sales",
                            )
                        ],
                        schema="sales",
                    )
                ]
            ),
            message="add total to orders",
        )

    def test_nested_containers_rewritten(self):
        script = self._nested_script()
        new_script = rename_table(
            script, "orders", "purchases", schema="sales"
        )
        is_not_(new_script, script)

        up = new_script.upgrade_ops
        mod_table = up.ops[0]
        eq_(mod_table.table_name, "purchases")
        eq_(mod_table.ops[0].table_name, "purchases")
        eq_(mod_table.ops[1].table_name, "purchases")

        nested_up = up.ops[1]
        eq_(nested_up.upgrade_token, "nested_upgrades")
        eq_(nested_up.ops[0].table_name, "purchases")

        down = new_script.downgrade_ops
        eq_(down.ops[0].table_name, "purchases")
        eq_(down.ops[0].ops[0].table_name, "purchases")

    def test_nested_original_untouched(self):
        script = self._nested_script()
        rename_table(script, "orders", "purchases", schema="sales")
        eq_(script.upgrade_ops.ops[0].table_name, "orders")
        eq_(script.upgrade_ops.ops[0].ops[0].table_name, "orders")
        eq_(script.upgrade_ops.ops[1].ops[0].table_name, "orders")
        eq_(script.downgrade_ops.ops[0].ops[0].table_name, "orders")

    def test_migration_script_metadata_preserved(self):
        script = self._nested_script()
        new_script = rename_table(
            script, "orders", "purchases", schema="sales"
        )
        eq_(new_script.rev_id, "abc123")
        eq_(new_script.message, "add total to orders")
        eq_(new_script.upgrade_ops.upgrade_token, "upgrades")
        eq_(new_script.downgrade_ops.downgrade_token, "downgrades")

    def test_multi_pass_migration_script(self):
        # a MigrationScript with multiple UpgradeOps / DowngradeOps
        # entries, as produced by multi-database autogenerate templates
        script = MigrationScript(
            "abc123",
            [
                UpgradeOps(
                    ops=[
                        AddColumnOp(
                            "orders", Column("a", Integer), schema="sales"
                        )
                    ]
                ),
                UpgradeOps(
                    ops=[
                        AddColumnOp(
                            "orders", Column("b", Integer), schema="sales"
                        )
                    ]
                ),
            ],
            [
                DowngradeOps(
                    ops=[DropColumnOp("orders", "a", schema="sales")]
                ),
                DowngradeOps(
                    ops=[DropColumnOp("orders", "b", schema="sales")]
                ),
            ],
        )
        new_script = rename_table(
            script, "orders", "purchases", schema="sales"
        )
        eq_(len(new_script.upgrade_ops_list), 2)
        eq_(len(new_script.downgrade_ops_list), 2)
        for container in new_script.upgrade_ops_list:
            eq_(container.ops[0].table_name, "purchases")
        for container in new_script.downgrade_ops_list:
            eq_(container.ops[0].table_name, "purchases")
        # originals untouched
        eq_(script.upgrade_ops_list[0].ops[0].table_name, "orders")
        eq_(script.downgrade_ops_list[1].ops[0].table_name, "orders")

    def test_plan_changes_report_nested_paths(self):
        script = self._nested_script()
        plan = RenameTablePlan(script, "orders", "purchases", schema="sales")
        eq_(
            {(c.kind, c.path) for c in plan.changes},
            {
                ("table", ("upgrade_ops", 0, 0)),
                ("table", ("upgrade_ops", 0, 0, 0)),
                ("table", ("upgrade_ops", 0, 0, 1)),
                ("table", ("upgrade_ops", 0, 1, 0)),
                ("table", ("downgrade_ops", 0, 0)),
                ("table", ("downgrade_ops", 0, 0, 0)),
            },
        )
        for change in plan.changes:
            eq_(change.old_table_name, "orders")
            eq_(change.new_table_name, "purchases")
            eq_(change.schema, "sales")


class UnsupportedOperationTest(TestBase):
    """An unknown custom operation anywhere in the tree fails the whole
    plan; nothing is renamed and the original tree is untouched."""

    def _tree_with_custom_op(self):
        custom_op = MyCustomOp("orders")
        custom_op.info["tag"] = "custom"
        ops = UpgradeOps(
            ops=[
                AddColumnOp(
                    "orders", Column("total", Integer), schema="sales"
                ),
                custom_op,
                CreateIndexOp(
                    "ix_orders_total", "orders", ["total"], schema="sales"
                ),
            ]
        )
        return ops, custom_op

    def test_plan_rejected(self):
        ops, custom_op = self._tree_with_custom_op()
        assert_raises_message(
            UnsupportedOperationError,
            r"operation of type 'MyCustomOp' at path \(1,\) is not "
            "supported",
            RenameTablePlan,
            ops,
            "orders",
            "purchases",
            schema="sales",
        )

    def test_rename_table_rejected(self):
        ops, custom_op = self._tree_with_custom_op()
        assert_raises_message(
            UnsupportedOperationError,
            "is not supported and may reference the table",
            rename_table,
            ops,
            "orders",
            "purchases",
            schema="sales",
        )

    def test_error_is_a_table_rename_error(self):
        ops, custom_op = self._tree_with_custom_op()
        try:
            RenameTablePlan(ops, "orders", "purchases", schema="sales")
        except TableRenameError as err:
            is_(err.op, custom_op)
            eq_(err.path, (1,))
        else:
            raise AssertionError("expected TableRenameError")

    def test_failed_plan_leaves_original_tree_untouched(self):
        ops, custom_op = self._tree_with_custom_op()
        before = list(ops.ops)
        try:
            rename_table(ops, "orders", "purchases", schema="sales")
        except UnsupportedOperationError:
            pass
        else:
            raise AssertionError("expected UnsupportedOperationError")
        # same objects, same names, same info: no half-renamed tree
        eq_(ops.ops, before)
        for op in before:
            is_(op.info, op.info)
        eq_(ops.ops[0].table_name, "orders")
        eq_(ops.ops[2].table_name, "orders")
        eq_(custom_op.info, {"tag": "custom"})

    def test_custom_op_nested_in_containers_rejected(self):
        script = MigrationScript(
            "abc123",
            UpgradeOps(
                ops=[
                    ModifyTableOps(
                        "orders",
                        ops=[MyCustomOp("orders")],
                        schema="sales",
                    )
                ]
            ),
            DowngradeOps(ops=[]),
        )
        assert_raises_message(
            UnsupportedOperationError,
            "MyCustomOp",
            RenameTablePlan,
            script,
            "orders",
            "purchases",
            schema="sales",
        )
        eq_(script.upgrade_ops.ops[0].table_name, "orders")


class ObjectIndependenceTest(TestBase):
    """The applied tree is a replica: mutating it never affects the
    original operation objects or their info dicts."""

    def _tree(self):
        ops = UpgradeOps(
            ops=[
                AddColumnOp(
                    "orders", Column("total", Integer), schema="sales"
                ),
                CreateIndexOp(
                    "ix_orders_total",
                    "orders",
                    ["total"],
                    schema="sales",
                ),
            ]
        )
        ops.ops[0].info["source"] = "autogenerate"
        return ops

    def test_apply_returns_new_objects(self):
        ops = self._tree()
        new_ops = rename_table(ops, "orders", "purchases", schema="sales")
        is_not_(new_ops, ops)
        is_not_(new_ops.ops, ops.ops)
        for old_op, new_op in zip(ops.ops, new_ops.ops):
            is_not_(new_op, old_op)

    def test_mutation_of_replica_does_not_affect_original(self):
        ops = self._tree()
        new_ops = rename_table(ops, "orders", "purchases", schema="sales")

        new_ops.ops[0].table_name = "something_else"
        new_ops.ops[0].info["source"] = "edited"
        new_ops.ops[1].columns.append("extra")
        new_ops.ops.append(DropColumnOp("orders", "x", schema="sales"))

        eq_(ops.ops[0].table_name, "orders")
        eq_(ops.ops[0].info, {"source": "autogenerate"})
        eq_(ops.ops[1].columns, ["total"])
        eq_(len(ops.ops), 2)

    def test_info_dict_copied_not_shared(self):
        ops = self._tree()
        new_ops = rename_table(ops, "orders", "purchases", schema="sales")
        eq_(new_ops.ops[0].info, {"source": "autogenerate"})
        is_not_(new_ops.ops[0].info, ops.ops[0].info)

    def test_apply_twice_gives_independent_replicas(self):
        ops = self._tree()
        plan = RenameTablePlan(ops, "orders", "purchases", schema="sales")
        first = plan.apply()
        second = plan.apply()
        is_not_(first, second)
        is_not_(first.ops[0], second.ops[0])
        first.ops[0].table_name = "edited"
        eq_(second.ops[0].table_name, "purchases")
        eq_(ops.ops[0].table_name, "orders")


class ReadOnlyPlanTest(TestBase):
    """The plan object is a read-only view; scanning does not mutate."""

    def test_plan_construction_does_not_modify_tree(self):
        ops = _sales_orders_upgrade_ops()
        plan = RenameTablePlan(ops, "orders", "purchases", schema="sales")
        is_(plan.ops, ops)
        eq_([op.table_name for op in ops.ops], ["orders"] * 4)
        eq_(len(plan), 4)
        is_(bool(plan), True)

    def test_plan_changes_reference_original_ops(self):
        ops = _sales_orders_upgrade_ops()
        plan = RenameTablePlan(ops, "orders", "purchases", schema="sales")
        eq_(
            [change.operation for change in plan.changes],
            list(ops.ops),
        )
        eq_(
            [change.kind for change in plan.changes],
            ["table"] * 4,
        )

    def test_plan_with_no_matching_ops(self):
        ops = UpgradeOps(
            ops=[AddColumnOp("orders", Column("x", Integer), schema="public")]
        )
        plan = RenameTablePlan(ops, "orders", "purchases", schema="sales")
        eq_(plan.changes, ())
        is_(bool(plan), False)
        new_ops = plan.apply()
        eq_(new_ops.ops[0].table_name, "orders")
        is_not_(new_ops, ops)


class FormatOpTreeTest(TestBase):
    """Database-free printing of an operation tree."""

    def test_format_op_tree(self):
        script = MigrationScript(
            "abc123",
            UpgradeOps(
                ops=[
                    AddColumnOp(
                        "orders", Column("total", Integer), schema="sales"
                    ),
                    CreateForeignKeyOp(
                        "fk_orders_customer",
                        "orders",
                        "customer",
                        ["customer_id"],
                        ["id"],
                        source_schema="sales",
                        referent_schema="sales",
                    ),
                ]
            ),
            DowngradeOps(
                ops=[DropColumnOp("orders", "total", schema="sales")]
            ),
            message="add total",
        )
        new_script = rename_table(
            script, "orders", "purchases", schema="sales"
        )
        eq_(
            format_op_tree(new_script),
            "MigrationScript(rev_id='abc123', message='add total')\n"
            "  UpgradeOps(token='upgrades')\n"
            "    AddColumnOp(table=sales.purchases, column='total')\n"
            "    CreateForeignKeyOp(constraint='fk_orders_customer', "
            "source=sales.purchases, referent=sales.customer)\n"
            "  DowngradeOps(token='downgrades')\n"
            "    DropColumnOp(table=sales.purchases, column='total')",
        )

    def test_format_op_tree_handles_unknown_ops(self):
        ops = UpgradeOps(ops=[MyCustomOp("orders")])
        eq_(
            format_op_tree(ops),
            "UpgradeOps(token='upgrades')\n  MyCustomOp(...)",
        )
