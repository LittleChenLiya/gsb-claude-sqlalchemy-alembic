"""Pure in-memory tests for the transaction-boundary analyzer.

No database, no configuration and no SQL execution is involved; every
fixture is built directly from operation objects.
"""

from __future__ import annotations

from sqlalchemy import Column
from sqlalchemy import String
from sqlalchemy import text

from alembic.analysis import analyze_transaction_boundaries
from alembic.analysis import Fragment
from alembic.analysis import TransactionBoundaryOp
from alembic.analysis.txboundary import BOUNDARY_SQL_SENTINEL
from alembic.operations.ops import AddColumnOp
from alembic.operations.ops import CreateIndexOp
from alembic.operations.ops import DowngradeOps
from alembic.operations.ops import ExecuteSQLOp
from alembic.operations.ops import MigrateOperation
from alembic.operations.ops import MigrationScript
from alembic.operations.ops import ModifyTableOps
from alembic.operations.ops import UpgradeOps
from alembic.testing import eq_
from alembic.testing import is_
from alembic.testing import is_false
from alembic.testing.fixtures import TestBase


def add_column(name="email"):
    return AddColumnOp("account", Column(name, String(100)))


def create_index(name="ix_account_email", **kw):
    return CreateIndexOp(name, "account", ["email"], **kw)


class CustomPluginOp(MigrateOperation):
    """A third-party style op the analyzer knows nothing about."""

    def reverse(self):  # pragma: no cover - not exercised
        return self

    def to_diff_tuple(self):  # pragma: no cover - not exercised
        return ("custom_plugin_op",)


class TestAcceptanceScenario(TestBase):
    def test_addcolumn_marker_reindex_two_fragments(self):
        tree = UpgradeOps(
            ops=[
                add_column(),
                TransactionBoundaryOp(note="cut"),
                create_index(),
            ]
        )

        analysis = analyze_transaction_boundaries(tree)

        eq_(len(analysis.fragments), 2)
        first, second = analysis.fragments
        # ordered fragments, original flat op indices retained
        eq_((first.sequence, first.classification), (1, "safe"))
        eq_((second.sequence, second.classification), (2, "safe"))
        eq_(first.index_range, (0, 0))
        eq_(second.index_range, (2, 2))
        eq_(first.operations[0].path, (0,))
        eq_(second.operations[0].path, (2,))
        is_(first.operations[0].op.__class__, AddColumnOp)
        is_(second.operations[0].op.__class__, CreateIndexOp)

    def test_marker_position_is_skipped_not_renumbered(self):
        tree = UpgradeOps(ops=[add_column(), TransactionBoundaryOp()])

        analysis = analyze_transaction_boundaries(tree)

        eq_(len(analysis.fragments), 1)
        eq_(analysis.fragments[0].operations[0].index, 0)


class TestUnknownOperations(TestBase):
    def test_raw_sql_is_unknown_not_merged_into_safe(self):
        tree = UpgradeOps(
            ops=[
                add_column("a"),
                ExecuteSQLOp("VACUUM ANALYZE account"),
                add_column("b"),
            ]
        )

        analysis = analyze_transaction_boundaries(tree)

        eq_(
            [f.classification for f in analysis.fragments],
            ["safe", "unknown", "safe"],
        )
        unknown = analysis.fragments[1]
        eq_(unknown.operations[0].index, 1)
        assert "raw SQL" in unknown.operations[0].reason
        eq_(analysis.unknown_count, 1)
        is_false(analysis.is_fully_safe())

    def test_custom_op_is_unknown(self):
        tree = UpgradeOps(ops=[add_column(), CustomPluginOp()])

        analysis = analyze_transaction_boundaries(tree)

        eq_(
            [f.classification for f in analysis.fragments],
            ["safe", "unknown"],
        )
        assert "CustomPluginOp" in analysis.fragments[1].operations[0].reason

    def test_adjacent_unknowns_share_fragment_but_never_join_safe(self):
        tree = UpgradeOps(
            ops=[
                ExecuteSQLOp("VACUUM"),
                ExecuteSQLOp("NOTIFY x"),
                add_column(),
            ]
        )

        analysis = analyze_transaction_boundaries(tree)

        eq_(
            [f.classification for f in analysis.fragments],
            ["unknown", "safe"],
        )
        eq_(analysis.fragments[0].index_range, (0, 1))


class TestNontransactional(TestBase):
    def test_concurrent_create_index_is_nontransactional(self):
        tree = UpgradeOps(
            ops=[
                add_column(),
                create_index(postgresql_concurrently=True),
                add_column("b"),
            ]
        )

        analysis = analyze_transaction_boundaries(tree)

        eq_(
            [f.classification for f in analysis.fragments],
            ["safe", "nontransactional", "safe"],
        )
        eq_(analysis.fragments[1].index_range, (1, 1))

    def test_plain_create_index_is_safe(self):
        analysis = analyze_transaction_boundaries(
            UpgradeOps(ops=[create_index()])
        )
        eq_(analysis.fragments[0].classification, "safe")

    def test_sql_sentinel_marker_splits_fragments(self):
        tree = UpgradeOps(
            ops=[
                add_column(),
                ExecuteSQLOp(BOUNDARY_SQL_SENTINEL + ";"),
                create_index(),
            ]
        )

        analysis = analyze_transaction_boundaries(tree)

        eq_(len(analysis.fragments), 2)
        eq_(analysis.fragments[0].index_range, (0, 0))
        eq_(analysis.fragments[1].index_range, (2, 2))

    def test_sql_sentinel_accepted_as_textclause(self):
        # op.execute() coerces plain strings into text(); the analyzer must
        # recognise the sentinel in that form without compiling it.
        tree = UpgradeOps(
            ops=[
                add_column(),
                ExecuteSQLOp(text(BOUNDARY_SQL_SENTINEL)),
                create_index(),
            ]
        )

        analysis = analyze_transaction_boundaries(tree)

        eq_(len(analysis.fragments), 2)
        eq_([f.classification for f in analysis.fragments], ["safe", "safe"])

    def test_arbitrary_textclause_sql_is_unknown(self):
        analysis = analyze_transaction_boundaries(
            UpgradeOps(ops=[ExecuteSQLOp(text("VACUUM"))])
        )
        eq_(analysis.fragments[0].classification, "unknown")


class TestEmptyContainersAndRedundantMarkers(TestBase):
    def test_empty_batch_container_emits_nothing(self):
        tree = UpgradeOps(
            ops=[ModifyTableOps("account", ops=[]), add_column()]
        )

        analysis = analyze_transaction_boundaries(tree)

        eq_(len(analysis.fragments), 1)
        eq_(analysis.fragments[0].index_range, (0, 0))

    def test_completely_empty_tree_has_no_fragments(self):
        for empty in (
            UpgradeOps(ops=[]),
            ModifyTableOps("account", ops=[]),
            UpgradeOps(
                ops=[
                    TransactionBoundaryOp(),
                    ModifyTableOps("account", ops=[]),
                    TransactionBoundaryOp(),
                ]
            ),
        ):
            analysis = analyze_transaction_boundaries(empty)
            eq_(analysis.fragments, [])
            is_false(analysis.is_fully_safe())

    def test_leading_trailing_and_repeated_markers_make_no_empty_fragments(
        self,
    ):
        tree = UpgradeOps(
            ops=[
                TransactionBoundaryOp(),
                add_column("a"),
                TransactionBoundaryOp(),
                TransactionBoundaryOp(),
                add_column("b"),
                TransactionBoundaryOp(),
            ]
        )

        analysis = analyze_transaction_boundaries(tree)

        eq_(len(analysis.fragments), 2)
        assert all(f.operations for f in analysis.fragments)
        eq_(analysis.fragments[0].index_range, (1, 1))
        eq_(analysis.fragments[1].index_range, (4, 4))


class TestNestingOrderAndReadonly(TestBase):
    def test_nested_batch_marker_preserves_order_and_paths(self):
        tree = UpgradeOps(
            ops=[
                ModifyTableOps(
                    "account",
                    ops=[
                        add_column("a"),
                        TransactionBoundaryOp(),
                        create_index(),
                    ],
                ),
                add_column("b"),
            ]
        )

        analysis = analyze_transaction_boundaries(tree)

        # the nested marker forces a cut even though both sides are "safe";
        # create_index and the following outer add_column merge afterwards
        eq_(len(analysis.fragments), 2)
        eq_(
            [f.classification for f in analysis.fragments],
            ["safe", "safe"],
        )
        item0 = analysis.fragments[0].operations[0]
        second = analysis.fragments[1].operations
        eq_(second[0].path, (0, 2))
        eq_(second[1].path, (1,))
        # flat indices follow original execution order, marker counted at
        # its original position
        eq_(item0.index, 0)
        eq_([second[0].index, second[1].index], [2, 3])

    def test_deeply_nested_empty_containers_are_skipped(self):
        tree = UpgradeOps(
            ops=[
                ModifyTableOps(
                    "account",
                    ops=[
                        ModifyTableOps("account", ops=[]),
                    ],
                ),
            ]
        )
        eq_(analyze_transaction_boundaries(tree).fragments, [])

    def test_plain_sequence_input(self):
        analysis = analyze_transaction_boundaries(
            [add_column(), TransactionBoundaryOp(), create_index()]
        )
        eq_(len(analysis.fragments), 2)

    def test_migration_script_input_uses_upgrade_ops(self):
        script = MigrationScript(
            rev_id="rev",
            upgrade_ops=UpgradeOps(
                ops=[add_column(), TransactionBoundaryOp(), create_index()]
            ),
            downgrade_ops=DowngradeOps(ops=[]),
        )

        analysis = analyze_transaction_boundaries(script)

        eq_(len(analysis.fragments), 2)

    def test_input_tree_is_not_mutated(self):
        marker = TransactionBoundaryOp()
        batch = ModifyTableOps(
            "account", ops=[add_column(), marker, create_index()]
        )
        tree = UpgradeOps(ops=[batch])
        before = list(tree.ops)
        batch_before = list(batch.ops)

        analyze_transaction_boundaries(tree)

        eq_(tree.ops, before)
        eq_(batch.ops, batch_before)
        is_(batch.ops[1], marker)

    def test_unknown_inside_nested_batch_stays_unknown(self):
        tree = UpgradeOps(
            ops=[
                add_column("a"),
                ModifyTableOps(
                    "account",
                    ops=[ExecuteSQLOp("VACUUM")],
                ),
            ]
        )

        analysis = analyze_transaction_boundaries(tree)

        eq_(
            [f.classification for f in analysis.fragments],
            ["safe", "unknown"],
        )
        eq_(analysis.fragments[1].operations[0].path, (1, 0))


class TestReport(TestBase):
    def test_report_lists_fragments_indices_and_unknown_reason(self):
        tree = UpgradeOps(
            ops=[
                add_column(),
                ExecuteSQLOp("VACUUM"),
                TransactionBoundaryOp(),
                create_index(postgresql_concurrently=True),
            ]
        )

        report = analyze_transaction_boundaries(tree).render_report()

        assert "fragments: 3" in report
        assert "unknown operations: 1" in report
        assert "fragment 1: safe (op[0])" in report
        assert "fragment 2: unknown (op[1])" in report
        assert "fragment 3: nontransactional (op[3])" in report
        assert "path=0" in report
        assert "path=1" in report
        assert "path=3" in report

    def test_fragment_dataclass_shape(self):
        analysis = analyze_transaction_boundaries(
            UpgradeOps(ops=[add_column()])
        )
        fragment = analysis.fragments[0]
        is_(fragment.__class__, Fragment)
        eq_(fragment.sequence, 1)
        eq_(fragment.describe(), "fragment 1: safe (op[0])")
