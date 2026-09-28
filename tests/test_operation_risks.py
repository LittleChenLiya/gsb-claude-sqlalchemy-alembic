"""Tests for read-only risk analysis of migration operation trees."""

import pickle

from sqlalchemy import Column
from sqlalchemy import Integer
from sqlalchemy import String

from alembic.operations import analyze_risks
from alembic.operations.ops import AddColumnOp
from alembic.operations.ops import AlterColumnOp
from alembic.operations.ops import DowngradeOps
from alembic.operations.ops import DropColumnOp
from alembic.operations.ops import DropTableOp
from alembic.operations.ops import ModifyTableOps
from alembic.operations.ops import UpgradeOps
from alembic.testing import assert_raises_message
from alembic.testing import eq_
from alembic.testing import is_false
from alembic.testing import is_true
from alembic.testing import TestBase


def _add_column_op(column_name="note"):
    return AddColumnOp(
        "account", Column(column_name, String(50), nullable=True)
    )


def _shorten_op():
    return AlterColumnOp(
        "account",
        "name",
        existing_type=String(100),
        modify_type=String(20),
        existing_nullable=True,
    )


class RiskAnalysisTest(TestBase):
    def test_nested_ordering_and_non_risks(self):
        upgrade_ops = UpgradeOps(
            [
                ModifyTableOps(
                    "account",
                    [
                        _shorten_op(),
                        DropColumnOp("account", "legacy_token"),
                        _add_column_op(),
                    ],
                )
            ]
        )

        risks = analyze_risks(upgrade_ops)

        eq_(
            [(risk.risk_type, risk.column_name) for risk in risks],
            [
                ("shorten_string", "name"),
                ("drop_column", "legacy_token"),
            ],
        )
        eq_(risks[0].location, (0, 0))
        eq_(risks[1].location, (0, 1))
        is_true(risks[0].determinable)
        is_true(risks[1].determinable)

    def test_drop_table_and_nested_traversal(self):
        upgrade_ops = UpgradeOps(
            [
                DropTableOp("obsolete_cache"),
                ModifyTableOps(
                    "account",
                    [
                        ModifyTableOps(
                            "account",
                            [DropColumnOp("account", "legacy_token")],
                        )
                    ],
                ),
            ]
        )

        risks = analyze_risks(upgrade_ops)

        eq_(
            [risk.risk_type for risk in risks],
            ["drop_table", "drop_column"],
        )
        eq_(risks[0].table_name, "obsolete_cache")
        eq_(risks[0].column_name, None)
        eq_(risks[0].location, (0,))
        eq_(risks[1].location, (1, 0, 0))

    def test_missing_old_type_is_undeterminable(self):
        upgrade_ops = UpgradeOps(
            [
                ModifyTableOps(
                    "account",
                    [
                        AlterColumnOp(
                            "account", "name", modify_type=String(20)
                        ),
                    ],
                )
            ]
        )

        risks = analyze_risks(upgrade_ops)

        eq_(len(risks), 1)
        risk = risks[0]
        eq_(risk.risk_type, "shorten_string")
        is_false(risk.determinable)
        is_true("无法判断" in risk.reason)
        is_true("旧类型" in risk.reason)

    def test_missing_old_nullable_is_undeterminable(self):
        upgrade_ops = UpgradeOps(
            [
                ModifyTableOps(
                    "account",
                    [
                        AlterColumnOp(
                            "account",
                            "name",
                            modify_nullable=False,
                            modify_type=String(100),
                            existing_type=String(100),
                        ),
                    ],
                )
            ]
        )

        risks = analyze_risks(upgrade_ops)

        eq_(len(risks), 1)
        risk = risks[0]
        eq_(risk.risk_type, "not_nullable")
        is_false(risk.determinable)
        is_true("无法判断" in risk.reason)
        is_true("nullable" in risk.reason)

    def test_nullable_true_to_false_is_risk(self):
        upgrade_ops = UpgradeOps(
            [
                ModifyTableOps(
                    "account",
                    [
                        AlterColumnOp(
                            "account",
                            "name",
                            existing_nullable=True,
                            modify_nullable=False,
                        ),
                    ],
                )
            ]
        )

        risks = analyze_risks(upgrade_ops)

        eq_(len(risks), 1)
        eq_(risks[0].risk_type, "not_nullable")
        is_true(risks[0].determinable)

    def test_longer_string_and_benign_changes_are_not_reported(self):
        upgrade_ops = UpgradeOps(
            [
                ModifyTableOps(
                    "account",
                    [
                        AlterColumnOp(
                            "account",
                            "name",
                            existing_type=String(20),
                            modify_type=String(100),
                        ),
                        AlterColumnOp(
                            "account",
                            "name",
                            existing_type=String(50),
                            modify_type=String(50),
                        ),
                        AlterColumnOp(
                            "account",
                            "name",
                            modify_name="display_name",
                        ),
                        AlterColumnOp(
                            "account",
                            "name",
                            existing_nullable=False,
                            modify_nullable=True,
                        ),
                        AlterColumnOp(
                            "account",
                            "name",
                            existing_type=Integer(),
                            modify_type=String(20),
                        ),
                        _add_column_op(),
                    ],
                )
            ]
        )

        eq_(analyze_risks(upgrade_ops), [])

    def test_reverse_migration_reports_reversed_risks(self):
        upgrade_ops = UpgradeOps(
            [
                ModifyTableOps(
                    "account",
                    [
                        _add_column_op("note"),
                        AlterColumnOp(
                            "account",
                            "name",
                            existing_type=String(20),
                            modify_type=String(100),
                            existing_nullable=False,
                            modify_nullable=True,
                        ),
                    ],
                )
            ]
        )
        eq_(analyze_risks(upgrade_ops), [])

        downgrade_ops = upgrade_ops.reverse()
        risks = analyze_risks(downgrade_ops)

        eq_(
            [risk.risk_type for risk in risks],
            ["shorten_string", "not_nullable", "drop_column"],
        )
        eq_(risks[0].column_name, "name")
        eq_(risks[1].column_name, "name")
        eq_(risks[2].column_name, "note")
        is_true(risks[0].reason.startswith("字符串长度由 100 缩短为 20"))
        is_true(all(risk.determinable for risk in risks))

    def test_string_without_length_is_undeterminable(self):
        upgrade_ops = UpgradeOps(
            [
                ModifyTableOps(
                    "account",
                    [
                        AlterColumnOp(
                            "account",
                            "name",
                            existing_type=String,
                            modify_type=String(),
                        ),
                    ],
                )
            ]
        )

        risks = analyze_risks(upgrade_ops)
        eq_(len(risks), 1)
        is_false(risks[0].determinable)

    def test_input_tree_is_not_modified(self):
        upgrade_ops = UpgradeOps(
            [
                DropTableOp("obsolete_cache"),
                ModifyTableOps(
                    "account",
                    [
                        _shorten_op(),
                        DropColumnOp("account", "legacy_token"),
                    ],
                ),
            ]
        )
        before = pickle.dumps(upgrade_ops)

        analyze_risks(upgrade_ops)

        eq_(pickle.dumps(upgrade_ops), before)

    def test_downgrade_ops_accepted(self):
        downgrade_ops = DowngradeOps([DropTableOp("account")])

        risks = analyze_risks(downgrade_ops)

        eq_(len(risks), 1)
        eq_(risks[0].risk_type, "drop_table")

    def test_invalid_input_raises_type_error(self):
        assert_raises_message(
            TypeError,
            "analyze_risks.. expects an OpContainer",
            analyze_risks,
            DropColumnOp("account", "name"),
        )
