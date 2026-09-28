"""Tests for :mod:`alembic.operations.risk`."""

import sqlalchemy as sa

from alembic.operations.ops import AddColumnOp
from alembic.operations.ops import AlterColumnOp
from alembic.operations.ops import DowngradeOps
from alembic.operations.ops import DropColumnOp
from alembic.operations.ops import DropTableOp
from alembic.operations.ops import ModifyTableOps
from alembic.operations.ops import UpgradeOps
from alembic.operations.risk import analyze_ops
from alembic.operations.risk import UNKNOWN_REASON

from alembic.testing.fixtures import TestBase


def _nested_upgrade_ops():
    return UpgradeOps(
        ops=[
            ModifyTableOps(
                "account",
                ops=[
                    AlterColumnOp(
                        "account",
                        "email",
                        existing_type=sa.String(100),
                        modify_type=sa.String(20),
                    ),
                    DropColumnOp("account", "legacy_flag"),
                    AddColumnOp(
                        "account",
                        sa.Column("nickname", sa.String(50), nullable=True),
                    ),
                ],
            ),
            DropTableOp("obsolete_stats"),
        ]
    )


class TestAnalyzeOps(TestBase):
    def test_nested_ops_reported_in_original_order(self):
        ops = _nested_upgrade_ops()

        records = analyze_ops(ops)

        assert [r.operation for r in records] == [
            "modify_type",
            "drop_column",
            "drop_table",
        ]
        assert [r.position for r in records] == [
            "ops[0].ops[0]",
            "ops[0].ops[1]",
            "ops[1]",
        ]
        assert records[0].table_name == "account"
        assert records[0].column_name == "email"
        assert "100" in records[0].reason
        assert "20" in records[0].reason
        assert records[1].table_name == "account"
        assert records[1].column_name == "legacy_flag"
        assert records[2].table_name == "obsolete_stats"
        assert records[2].column_name is None
        assert all(not r.uncertain for r in records)

    def test_input_tree_is_not_modified(self):
        ops = _nested_upgrade_ops()
        diffs_before = repr(ops.as_diffs())
        top_before = list(ops.ops)

        analyze_ops(ops)

        assert list(ops.ops) == top_before
        assert repr(ops.as_diffs()) == diffs_before

    def test_missing_existing_type_is_uncertain(self):
        ops = UpgradeOps(
            ops=[AlterColumnOp("t", "c", modify_type=sa.String(10))]
        )

        (record,) = analyze_ops(ops)

        assert record.operation == "modify_type"
        assert record.uncertain
        assert UNKNOWN_REASON in record.reason

    def test_missing_existing_nullable_is_uncertain(self):
        ops = UpgradeOps(ops=[AlterColumnOp("t", "c", modify_nullable=False)])

        (record,) = analyze_ops(ops)

        assert record.operation == "modify_nullable"
        assert record.uncertain
        assert UNKNOWN_REASON in record.reason

    def test_lengthening_string_is_not_reported(self):
        ops = UpgradeOps(
            ops=[
                AlterColumnOp(
                    "t",
                    "c",
                    existing_type=sa.String(20),
                    modify_type=sa.String(100),
                )
            ]
        )

        assert analyze_ops(ops) == []

    def test_rename_column_is_not_reported(self):
        ops = UpgradeOps(ops=[AlterColumnOp("t", "c", modify_name="new_c")])

        assert analyze_ops(ops) == []

    def test_nullable_to_not_null_is_reported(self):
        ops = UpgradeOps(
            ops=[
                AlterColumnOp(
                    "t",
                    "c",
                    existing_nullable=True,
                    modify_nullable=False,
                )
            ]
        )

        (record,) = analyze_ops(ops)

        assert record.operation == "modify_nullable"
        assert not record.uncertain

    def test_not_null_to_nullable_is_not_reported(self):
        ops = UpgradeOps(
            ops=[
                AlterColumnOp(
                    "t",
                    "c",
                    existing_nullable=False,
                    modify_nullable=True,
                )
            ]
        )

        assert analyze_ops(ops) == []

    def test_downgrade_ops_are_analyzed(self):
        ops = DowngradeOps(
            ops=[
                ModifyTableOps(
                    "account",
                    ops=[DropColumnOp("account", "nickname")],
                )
            ]
        )

        (record,) = analyze_ops(ops)

        assert record.operation == "drop_column"
        assert record.position == "ops[0].ops[0]"

    def test_reversed_upgrade_ops(self):
        upgrade_ops = UpgradeOps(
            ops=[
                ModifyTableOps(
                    "account",
                    ops=[
                        AlterColumnOp(
                            "account",
                            "email",
                            existing_type=sa.String(100),
                            modify_type=sa.String(20),
                        ),
                        AddColumnOp(
                            "account",
                            sa.Column("nickname", sa.String(50)),
                        ),
                    ],
                )
            ]
        )

        records = analyze_ops(upgrade_ops.reverse())

        # the reversed stream drops the added column and lengthens the
        # string back to 100, so only the drop is a risk
        assert [r.operation for r in records] == ["drop_column"]
        assert records[0].column_name == "nickname"
