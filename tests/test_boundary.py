"""Tests for the boundary analyzer.

These tests use pure in-memory operation fixtures; no database connection
is required.
"""

from sqlalchemy import Column
from sqlalchemy import Integer

from alembic.operations import MigrateOperation
from alembic.operations import ops
from alembic.operations.boundary import BoundaryAnalyzer
from alembic.operations.boundary import BoundaryMarker
from alembic.operations.boundary import SegmentStatus
from alembic.operations.boundary import analyze_operations
from alembic.testing import eq_
from alembic.testing.fixtures import TestBase


class BoundaryAnalyzerTest(TestBase):
    """Tests for :class:`.BoundaryAnalyzer`."""

    def _make_ops(self, *specs):
        """Build a list of operations from a list of (type, args) tuples.

        Each spec is a tuple of (op_type, *args) where op_type is one of:
        - "add_column": AddColumnOp(table_name, column)
        - "drop_column": DropColumnOp(table_name, column_name)
        - "create_index": CreateIndexOp(name, table_name, columns)
        - "drop_index": DropIndexOp(name, table_name)
        - "create_table": CreateTableOp(table_name, columns)
        - "drop_table": DropTableOp(table_name)
        - "execute_sql": ExecuteSQLOp(sqltext)
        - "bulk": ModifyTableOps(table_name, inner_ops)
        - "boundary": BoundaryMarker(reason)
        - "unknown": a custom unknown operation
        """
        result = []
        for spec in specs:
            op_type = spec[0]
            args = spec[1:]
            if op_type == "add_column":
                table_name, column = args
                result.append(ops.AddColumnOp(table_name, column))
            elif op_type == "drop_column":
                table_name, column_name = args
                result.append(ops.DropColumnOp(table_name, column_name))
            elif op_type == "create_index":
                name, table_name, columns = args
                result.append(ops.CreateIndexOp(name, table_name, columns))
            elif op_type == "drop_index":
                name, table_name = args
                result.append(ops.DropIndexOp(name, table_name))
            elif op_type == "create_table":
                table_name, columns = args
                result.append(ops.CreateTableOp(table_name, columns))
            elif op_type == "drop_table":
                table_name = args
                result.append(ops.DropTableOp(table_name))
            elif op_type == "execute_sql":
                (sqltext,) = args
                result.append(ops.ExecuteSQLOp(sqltext))
            elif op_type == "bulk":
                table_name, inner_specs = args
                inner_ops = self._make_ops(*inner_specs)
                result.append(ops.ModifyTableOps(table_name, inner_ops))
            elif op_type == "boundary":
                (reason,) = args
                result.append(BoundaryMarker(reason=reason))
            elif op_type == "unknown":
                result.append(UnknownOp())
            else:
                raise ValueError(f"Unknown op type: {op_type}")
        return result

    def test_add_column_boundary_create_index(self):
        """Normal add column, boundary marker, create index -> two segments."""
        operations = self._make_ops(
            ("add_column", "t1", Column("a", Integer)),
            ("boundary", "explicit boundary"),
            ("create_index", "ix_t1_a", "t1", ["a"]),
        )
        report = analyze_operations(operations)

        eq_(len(report.segments), 2)
        eq_(report.boundaries, [1])

        # First segment: add_column at position 0
        seg0 = report.segments[0]
        eq_(seg0.status, SegmentStatus.SAFE)
        eq_(seg0.start_position, 0)
        eq_(seg0.end_position, 0)
        eq_(len(seg0.operations), 1)
        eq_(type(seg0.operations[0]).__name__, "AddColumnOp")

        # Second segment: create_index at position 2
        seg1 = report.segments[1]
        eq_(seg1.status, SegmentStatus.SAFE)
        eq_(seg1.start_position, 2)
        eq_(seg1.end_position, 2)
        eq_(len(seg1.operations), 1)
        eq_(type(seg1.operations[0]).__name__, "CreateIndexOp")

    def test_unknown_operation_not_merged_into_safe(self):
        """Unknown operations show as unknown, not merged into safe segment."""
        operations = self._make_ops(
            ("add_column", "t1", Column("a", Integer)),
            ("unknown",),
            ("drop_column", "t1", "a"),
        )
        report = analyze_operations(operations)

        eq_(len(report.segments), 3)

        # First segment: safe add_column
        seg0 = report.segments[0]
        eq_(seg0.status, SegmentStatus.SAFE)
        eq_(seg0.start_position, 0)
        eq_(seg0.end_position, 0)

        # Second segment: unknown operation
        seg1 = report.segments[1]
        eq_(seg1.status, SegmentStatus.UNKNOWN)
        eq_(seg1.start_position, 1)
        eq_(seg1.end_position, 1)
        eq_(len(seg1.reasons), 1)
        assert "unknown operation type" in seg1.reasons[0]

        # Third segment: safe drop_column
        seg2 = report.segments[2]
        eq_(seg2.status, SegmentStatus.SAFE)
        eq_(seg2.start_position, 2)
        eq_(seg2.end_position, 2)

    def test_empty_bulk_container_no_segment(self):
        """Empty bulk container does not generate an empty segment."""
        operations = self._make_ops(
            ("add_column", "t1", Column("a", Integer)),
            ("bulk", "t1", []),
            ("drop_column", "t1", "a"),
        )
        report = analyze_operations(operations)

        # Empty bulk is skipped; add_column and drop_column merge into one
        # safe segment (positions 0 and 2, since the empty bulk at position
        # 1 is skipped)
        eq_(len(report.segments), 1)
        seg0 = report.segments[0]
        eq_(seg0.status, SegmentStatus.SAFE)
        eq_(seg0.start_position, 0)
        eq_(seg0.end_position, 2)
        eq_(len(seg0.operations), 2)

    def test_non_empty_bulk_container(self):
        """Non-empty bulk container generates a bulk segment."""
        operations = self._make_ops(
            ("add_column", "t1", Column("a", Integer)),
            ("bulk", "t1", [("add_column", "t1", Column("b", Integer))]),
            ("drop_column", "t1", "a"),
        )
        report = analyze_operations(operations)

        eq_(len(report.segments), 3)

        # First segment: safe add_column
        seg0 = report.segments[0]
        eq_(seg0.status, SegmentStatus.SAFE)
        eq_(seg0.start_position, 0)

        # Second segment: bulk
        seg1 = report.segments[1]
        eq_(seg1.status, SegmentStatus.BULK)
        eq_(seg1.start_position, 1)
        eq_(seg1.end_position, 1)
        eq_(len(seg1.operations), 1)
        eq_(type(seg1.operations[0]).__name__, "ModifyTableOps")

        # Third segment: safe drop_column
        seg2 = report.segments[2]
        eq_(seg2.status, SegmentStatus.SAFE)
        eq_(seg2.start_position, 2)

    def test_bulk_with_unknown_inner(self):
        """Bulk container with unknown inner operation is marked unknown."""
        operations = self._make_ops(
            ("add_column", "t1", Column("a", Integer)),
            ("bulk", "t1", [("execute_sql", "SELECT 1")]),
            ("drop_column", "t1", "a"),
        )
        report = analyze_operations(operations)

        eq_(len(report.segments), 3)

        # Second segment: unknown (bulk with unknown inner)
        seg1 = report.segments[1]
        eq_(seg1.status, SegmentStatus.UNKNOWN)
        eq_(seg1.start_position, 1)
        assert len(seg1.reasons) >= 1

    def test_nested_boundaries(self):
        """Nested operation containers with boundaries are analyzed correctly."""
        # Build a nested structure: UpgradeOps containing a bulk container
        # which contains operations, with boundaries at multiple levels
        inner_ops = self._make_ops(
            ("add_column", "t1", Column("b", Integer)),
            ("boundary", "inner boundary"),
            ("drop_column", "t1", "b"),
        )
        bulk_op = ops.ModifyTableOps("t1", inner_ops)

        operations = [
            ops.AddColumnOp("t1", Column("a", Integer)),
            BoundaryMarker(reason="outer boundary"),
            bulk_op,
            ops.DropColumnOp("t1", "a"),
        ]
        report = analyze_operations(operations)

        # The bulk container is treated as a single operation, so the inner
        # boundary is not visible at the top level.  The outer boundary splits
        # the segments.
        eq_(len(report.segments), 3)
        eq_(report.boundaries, [1])

        # First segment: safe add_column
        seg0 = report.segments[0]
        eq_(seg0.status, SegmentStatus.SAFE)
        eq_(seg0.start_position, 0)

        # Second segment: bulk (contains inner boundary but treated as unit)
        seg1 = report.segments[1]
        eq_(seg1.status, SegmentStatus.BULK)
        eq_(seg1.start_position, 2)

        # Third segment: safe drop_column
        seg2 = report.segments[2]
        eq_(seg2.status, SegmentStatus.SAFE)
        eq_(seg2.start_position, 3)

    def test_repeated_boundaries(self):
        """Repeated boundary markers split segments correctly."""
        operations = self._make_ops(
            ("add_column", "t1", Column("a", Integer)),
            ("boundary", "boundary 1"),
            ("create_index", "ix_t1_a", "t1", ["a"]),
            ("boundary", "boundary 2"),
            ("drop_column", "t1", "a"),
        )
        report = analyze_operations(operations)

        eq_(len(report.segments), 3)
        eq_(report.boundaries, [1, 3])

        # Segment 0: add_column
        seg0 = report.segments[0]
        eq_(seg0.status, SegmentStatus.SAFE)
        eq_(seg0.start_position, 0)
        eq_(seg0.end_position, 0)

        # Segment 1: create_index
        seg1 = report.segments[1]
        eq_(seg1.status, SegmentStatus.SAFE)
        eq_(seg1.start_position, 2)
        eq_(seg1.end_position, 2)

        # Segment 2: drop_column
        seg2 = report.segments[2]
        eq_(seg2.status, SegmentStatus.SAFE)
        eq_(seg2.start_position, 4)
        eq_(seg2.end_position, 4)

    def test_consecutive_unknown_operations(self):
        """Consecutive unknown operations are grouped into one unknown segment."""
        operations = self._make_ops(
            ("add_column", "t1", Column("a", Integer)),
            ("unknown",),
            ("unknown",),
            ("drop_column", "t1", "a"),
        )
        report = analyze_operations(operations)

        eq_(len(report.segments), 3)

        # Second segment: unknown (two consecutive unknown ops)
        seg1 = report.segments[1]
        eq_(seg1.status, SegmentStatus.UNKNOWN)
        eq_(seg1.start_position, 1)
        eq_(seg1.end_position, 2)
        eq_(len(seg1.operations), 2)

    def test_empty_operations(self):
        """Empty operation list returns empty report."""
        report = analyze_operations([])
        eq_(len(report.segments), 0)
        eq_(len(report.boundaries), 0)
        eq_(report.total_operations, 0)

    def test_execute_sql_is_unknown(self):
        """ExecuteSQLOp is marked unknown (cannot verify safety)."""
        operations = self._make_ops(
            ("add_column", "t1", Column("a", Integer)),
            ("execute_sql", "SELECT 1"),
            ("drop_column", "t1", "a"),
        )
        report = analyze_operations(operations)

        eq_(len(report.segments), 3)

        seg1 = report.segments[1]
        eq_(seg1.status, SegmentStatus.UNKNOWN)
        assert "ExecuteSQLOp" in seg1.reasons[0]

    def test_original_positions_preserved(self):
        """Original operation positions are preserved in segments."""
        operations = self._make_ops(
            ("add_column", "t1", Column("a", Integer)),
            ("create_index", "ix_t1_a", "t1", ["a"]),
            ("drop_column", "t1", "a"),
        )
        report = analyze_operations(operations)

        # All safe operations merge into one segment
        eq_(len(report.segments), 1)
        seg0 = report.segments[0]
        eq_(seg0.positions, [0, 1, 2])
        eq_(seg0.start_position, 0)
        eq_(seg0.end_position, 2)

    def test_report_summary(self):
        """Report summary produces human-readable output."""
        operations = self._make_ops(
            ("add_column", "t1", Column("a", Integer)),
            ("boundary", "test"),
            ("create_index", "ix_t1_a", "t1", ["a"]),
        )
        report = analyze_operations(operations)
        summary = report.summary()

        assert "Boundary Analysis Report" in summary
        assert "Total operations: 3" in summary
        assert "Boundaries:       1" in summary
        assert "Segments:         2" in summary
        assert "Segment 0: SAFE" in summary
        assert "Segment 1: SAFE" in summary
        assert "Boundary positions:" in summary


class UnknownOp(MigrateOperation):
    """A custom unknown operation for testing."""

    def reverse(self):
        return UnknownOp()

    def to_diff_tuple(self):
        return ("unknown",)


class BoundaryMarkerTest(TestBase):
    """Tests for :class:`.BoundaryMarker`."""

    def test_boundary_marker_creation(self):
        marker = BoundaryMarker(reason="test reason")
        eq_(marker.reason, "test reason")

    def test_boundary_marker_reverse(self):
        marker = BoundaryMarker(reason="test")
        reversed_marker = marker.reverse()
        eq_(reversed_marker.reason, "test")

    def test_boundary_marker_to_diff_tuple(self):
        marker = BoundaryMarker(reason="test")
        eq_(marker.to_diff_tuple(), ("boundary", "test"))

    def test_boundary_marker_no_reason(self):
        marker = BoundaryMarker()
        eq_(marker.reason, None)


class BoundaryAnalyzerWithUpgradeOpsTest(TestBase):
    """Tests using :class:`.UpgradeOps` containers."""

    def test_upgrade_ops_with_boundaries(self):
        """UpgradeOps containing operations and boundaries are analyzed."""
        upgrade_ops = ops.UpgradeOps(
            ops=[
                ops.AddColumnOp("t1", Column("a", Integer)),
                BoundaryMarker(reason="split"),
                ops.CreateIndexOp("ix_t1_a", "t1", ["a"]),
            ]
        )
        report = analyze_operations(upgrade_ops.ops)

        eq_(len(report.segments), 2)
        eq_(report.boundaries, [1])

    def test_nested_upgrade_ops(self):
        """Nested UpgradeOps containers are flattened correctly."""
        inner_ops = ops.UpgradeOps(
            ops=[
                ops.AddColumnOp("t1", Column("b", Integer)),
            ]
        )
        outer_ops = ops.UpgradeOps(
            ops=[
                ops.AddColumnOp("t1", Column("a", Integer)),
                inner_ops,
                ops.DropColumnOp("t1", "a"),
            ]
        )
        report = analyze_operations(outer_ops.ops)

        # All operations are safe, so they merge into one segment
        eq_(len(report.segments), 1)
        seg0 = report.segments[0]
        eq_(seg0.positions, [0, 1, 2])

