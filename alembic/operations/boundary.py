"""Boundary analysis for migration operation trees.

This module provides a static analyzer that traverses a migration operation
tree and produces segments describing which contiguous runs of operations
can maintain a transaction boundary.

The analyzer does not execute SQL, does not connect to a database, and does
not mutate the operation tree or schema objects.  It is intended to be used
as an offline, pre-flight check before generating or running a migration
script.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Any
from typing import Iterator
from typing import Sequence

from .ops import AddColumnOp
from .ops import AddConstraintOp
from .ops import AlterColumnOp
from .ops import AlterTableOp
from .ops import BulkInsertOp
from .ops import CreateIndexOp
from .ops import CreateTableOp
from .ops import DropColumnOp
from .ops import DropConstraintOp
from .ops import DropIndexOp
from .ops import DropTableOp
from .ops import ExecuteSQLOp
from .ops import MigrateOperation
from .ops import ModifyTableOps
from .ops import OpContainer
from .ops import RenameTableOp


class SegmentStatus(Enum):
    """Status of a boundary segment."""

    SAFE = "safe"
    UNKNOWN = "unknown"
    BULK = "bulk"


@dataclass
class Segment:
    """A contiguous segment of migration operations."""

    index: int
    status: SegmentStatus
    operations: list[MigrateOperation]
    positions: list[int]
    reasons: list[str] = field(default_factory=list)

    @property
    def start_position(self) -> int:
        return self.positions[0] if self.positions else -1

    @property
    def end_position(self) -> int:
        return self.positions[-1] if self.positions else -1

    @property
    def is_empty(self) -> bool:
        return not self.operations

    def __repr__(self) -> str:
        return (
            f"Segment(index={self.index}, status={self.status.value!r}, "
            f"positions={self.start_position}-{self.end_position}, "
            f"operations={len(self.operations)})"
        )


class BoundaryMarker(MigrateOperation):
    """An explicit transaction boundary marker."""

    def __init__(self, *, reason: str | None = None) -> None:
        self.reason = reason

    def reverse(self) -> "BoundaryMarker":
        return BoundaryMarker(reason=self.reason)

    def to_diff_tuple(self) -> tuple[str, Any]:
        return ("boundary", self.reason)

    def __repr__(self) -> str:
        return f"BoundaryMarker(reason={self.reason!r})"


@dataclass
class BoundaryReport:
    """The output of a boundary analysis."""

    segments: list[Segment]
    boundaries: list[int]
    total_operations: int
    total_positions: int

    def __repr__(self) -> str:
        return (
            f"BoundaryReport(segments={len(self.segments)}, "
            f"boundaries={len(self.boundaries)}, "
            f"operations={self.total_operations})"
        )

    def summary(self) -> str:
        lines = [
            "Boundary Analysis Report",
            "=" * 40,
            f"Total operations: {self.total_operations}",
            f"Total positions:  {self.total_positions}",
            f"Boundaries:       {len(self.boundaries)}",
            f"Segments:         {len(self.segments)}",
            "",
        ]
        for seg in self.segments:
            status_label = seg.status.value.upper()
            lines.append(
                f"Segment {seg.index}: {status_label} "
                f"(positions {seg.start_position}-{seg.end_position}, "
                f"{len(seg.operations)} operation(s))"
            )
            for pos, op in zip(seg.positions, seg.operations):
                lines.append(f"  [{pos}] {_describe(op)}")
            if seg.reasons:
                lines.append("  Reasons:")
                for reason in seg.reasons:
                    lines.append(f"    - {reason}")
            lines.append("")
        if self.boundaries:
            lines.append("Boundary positions:")
            for pos in self.boundaries:
                lines.append(f"  - position {pos}")
        return "\n".join(lines)


def _describe(op: MigrateOperation) -> str:
    name = type(op).__name__
    if hasattr(op, "table_name"):
        return f"{name}(table={op.table_name!r})"
    if hasattr(op, "table"):
        table = op.table
        if hasattr(table, "name"):
            return f"{name}(table={table.name!r})"
        return f"{name}(table={table!r})"
    return f"{name}()"


class BoundaryAnalyzer:
    """Analyzes a migration operation tree for transaction boundaries."""

    _known_safe_ops: frozenset[type[MigrateOperation]] = frozenset(
        {
            AddColumnOp,
            DropColumnOp,
            CreateIndexOp,
            DropIndexOp,
            CreateTableOp,
            DropTableOp,
            AlterColumnOp,
            AlterTableOp,
            RenameTableOp,
            AddConstraintOp,
            DropConstraintOp,
            BulkInsertOp,
        }
    )

    def __init__(self) -> None:
        self._segments: list[Segment] = []
        self._boundaries: list[int] = []
        self._position = 0

    def analyze(
        self, operations: Sequence[MigrateOperation]
    ) -> BoundaryReport:
        self._segments = []
        self._boundaries = []
        self._position = 0
        flat_ops = list(self._flatten(operations))
        self._build_segments(flat_ops)
        return BoundaryReport(
            segments=self._segments,
            boundaries=self._boundaries,
            total_operations=len(flat_ops),
            total_positions=self._position,
        )

    def _flatten(
        self, operations: Sequence[MigrateOperation]
    ) -> Iterator[tuple[int, MigrateOperation]]:
        for op in operations:
            if isinstance(op, ModifyTableOps):
                # Bulk table operation: treat as a single unit (do not
                # flatten).  Its inner operations are analyzed by
                # _classify_bulk.
                yield (self._position, op)
                self._position += 1
            elif isinstance(op, OpContainer):
                yield from self._flatten(op.ops)
            else:
                yield (self._position, op)
                self._position += 1

    def _build_segments(
        self, flat_ops: list[tuple[int, MigrateOperation]]
    ) -> None:
        if not flat_ops:
            return
        current_status: SegmentStatus | None = None
        current_ops: list[MigrateOperation] = []
        current_positions: list[int] = []
        current_reasons: list[str] = []

        for pos, op in flat_ops:
            classification, reasons = self._classify(op)
            if classification == "boundary":
                if current_ops:
                    self._segments.append(
                        Segment(
                            index=len(self._segments),
                            status=current_status,
                            operations=current_ops,
                            positions=current_positions,
                            reasons=current_reasons,
                        )
                    )
                    current_ops = []
                    current_positions = []
                    current_status = None
                    current_reasons = []
                self._boundaries.append(pos)
                continue
            if classification == "empty":
                continue
            if current_status is not None and classification != current_status.value:
                self._segments.append(
                    Segment(
                        index=len(self._segments),
                        status=current_status,
                        operations=current_ops,
                        positions=current_positions,
                        reasons=current_reasons,
                    )
                )
                current_ops = []
                current_positions = []
                current_reasons = []
            current_status = SegmentStatus(classification)
            current_ops.append(op)
            current_positions.append(pos)
            current_reasons.extend(reasons)

        if current_ops:
            self._segments.append(
                Segment(
                    index=len(self._segments),
                    status=current_status,
                    operations=current_ops,
                    positions=current_positions,
                    reasons=current_reasons,
                )
            )

    def _classify(self, op: MigrateOperation) -> tuple[str, list[str]]:
        if isinstance(op, BoundaryMarker):
            return ("boundary", [])
        if isinstance(op, ModifyTableOps):
            return self._classify_bulk(op)
        if isinstance(op, ExecuteSQLOp):
            return (
                "unknown",
                ["ExecuteSQLOp cannot be verified as safe without executing SQL"],
            )
        for safe_type in self._known_safe_ops:
            if isinstance(op, safe_type):
                return ("safe", [])
        return ("unknown", [f"unknown operation type: {type(op).__name__}"])

    def _classify_bulk(
        self, op: ModifyTableOps
    ) -> tuple[str, list[str]]:
        if op.is_empty():
            return ("empty", [])
        reasons: list[str] = []
        all_safe = True
        for inner_op in op.ops:
            # Boundary markers inside a bulk container are not SQL
            # operations; they do not affect the bulk classification.
            if isinstance(inner_op, BoundaryMarker):
                continue
            classification, inner_reasons = self._classify(inner_op)
            if classification != "safe":
                all_safe = False
                reasons.extend(inner_reasons)
        if all_safe:
            return ("bulk", [])
        return ("unknown", reasons)


def analyze_operations(
    operations: Sequence[MigrateOperation],
) -> BoundaryReport:
    analyzer = BoundaryAnalyzer()
    return analyzer.analyze(operations)


__all__ = [
    "BoundaryAnalyzer",
    "BoundaryMarker",
    "BoundaryReport",
    "Segment",
    "SegmentStatus",
    "analyze_operations",
]
