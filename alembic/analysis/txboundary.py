"""Transaction-boundary analysis for migration operation trees.

This module implements a *static*, dependency-free analyzer used to preview,
before an offline ("``--sql``") script is generated, which contiguous
sections of a migration are expected to run inside a single transaction.

Design constraints (see ``docs/build/transaction_analysis.rst``):

* No SQL is executed and no database is ever contacted.  Only the operation
  objects themselves are inspected, read-only.
* Each leaf operation is classified as:

  ``"safe"``
      A known operation ordinarily allowed inside a transaction.

  ``"nontransactional"``
      A known operation that must run outside of a transaction (e.g.
      ``CREATE INDEX CONCURRENTLY``) or an explicit boundary marker.

  ``"unknown"``
      The operation cannot be classified from the object alone.  Unknown
      operations are never folded into a ``"safe"`` fragment.

* An explicit boundary marker forces the fragment before it to end.
* Splitting never reorders operations; every emitted item keeps the
  original op's index path into the source tree.
* Empty containers and redundant (leading, trailing or repeated) markers
  never produce empty fragments.

"""

from __future__ import annotations

from collections.abc import Iterator
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import cast
from typing import Literal
from typing import TypeAlias

from ..operations.ops import AddColumnOp
from ..operations.ops import AddConstraintOp
from ..operations.ops import AlterColumnOp
from ..operations.ops import BulkInsertOp
from ..operations.ops import CreateIndexOp
from ..operations.ops import CreateTableOp
from ..operations.ops import DropColumnOp
from ..operations.ops import DropConstraintOp
from ..operations.ops import DropIndexOp
from ..operations.ops import DropTableOp
from ..operations.ops import ExecuteSQLOp
from ..operations.ops import MigrateOperation
from ..operations.ops import MigrationScript
from ..operations.ops import OpContainer
from ..operations.ops import RenameTableOp

#: Classification of a fragment or single operation.
Classification: TypeAlias = Literal["safe", "nontransactional", "unknown"]

#: Zero-based index path into the analyzed tree, e.g. ``(2, 0, 1)``.
IndexPath: TypeAlias = tuple[int, ...]

#: Accepted input of :func:`analyze_transaction_boundaries`.
OperationTree: TypeAlias = MigrateOperation | Sequence[MigrateOperation]

#: Sentinel SQL text recognised as an explicit transaction boundary when the
#: migration only had an :class:`~alembic.operations.ops.ExecuteSQLOp`
#: available.  Matching is exact after stripping surrounding whitespace and
#: a trailing semicolon.
BOUNDARY_SQL_SENTINEL = "-- alembic:transaction-boundary"

#: Concrete, well-understood operations known to be allowed inside a
#: transaction across the backends Alembic targets.  Anything not listed
#: here (and not special-cased in :func:`_classify`) stays ``"unknown"``.
_KNOWN_TRANSACTIONAL_OPS: tuple[type[MigrateOperation], ...] = (
    AddColumnOp,
    AddConstraintOp,
    AlterColumnOp,
    BulkInsertOp,
    CreateTableOp,
    DropColumnOp,
    DropConstraintOp,
    DropIndexOp,
    DropTableOp,
    RenameTableOp,
)


class TransactionBoundaryOp(MigrateOperation):
    """Explicit transaction-boundary marker (analysis-only operation).

    Place this op inside an :class:`~alembic.operations.ops.UpgradeOps`,
    :class:`~alembic.operations.ops.DowngradeOps` or
    :class:`~alembic.operations.ops.ModifyTableOps` sequence to declare that
    the offline migration script must commit at this point and start a fresh
    transaction afterwards.

    The marker carries no SQL of its own, is never executed against a live
    connection and is only interpreted by the static transaction-boundary
    analyzer.
    """

    def __init__(self, *, note: str | None = None) -> None:
        self.note = note

    def reverse(self) -> TransactionBoundaryOp:
        return TransactionBoundaryOp(note=self.note)

    def to_diff_tuple(self) -> tuple[str, str | None]:
        return ("transaction_boundary", self.note)


@dataclass(frozen=True)
class OperationItem:
    """A leaf operation paired with its original position in the source tree.

    ``op`` is the original :class:`.MigrateOperation`, never copied or
    mutated.  ``path`` is a tuple of zero-based indices reaching it from the
    analyzed root.  ``index`` is its flat, zero-based execution-order
    position; every leaf counts, including boundary markers, so that the
    index stays equal to the op's position in the flattened source tree
    (containers themselves are not counted).  ``classification`` is one of
    ``"safe"``, ``"nontransactional"`` or ``"unknown"`` and ``reason``
    explains the verdict.
    """

    op: MigrateOperation
    path: IndexPath
    index: int
    classification: Classification
    reason: str


@dataclass(frozen=True)
class Fragment:
    """An ordered, non-empty run of operations sharing one classification."""

    sequence: int
    classification: Classification
    operations: list[OperationItem] = field(default_factory=list)

    @property
    def index_range(self) -> tuple[int, int]:
        """Flat index span covered by the fragment, inclusive."""

        return (self.operations[0].index, self.operations[-1].index)

    def describe(self) -> str:
        """One-line summary including the original flat op indices."""

        first, last = self.index_range
        span = str(first) if first == last else "%d..%d" % (first, last)
        return "fragment %d: %s (op[%s])" % (
            self.sequence,
            self.classification,
            span,
        )


@dataclass(frozen=True)
class TransactionAnalysis:
    """Result of :func:`analyze_transaction_boundaries`."""

    fragments: list[Fragment]

    @property
    def unknown_count(self) -> int:
        return sum(
            1
            for fragment in self.fragments
            for item in fragment.operations
            if item.classification == "unknown"
        )

    def is_fully_safe(self) -> bool:
        """Return True only when every fragment is transaction-safe."""

        return bool(self.fragments) and all(
            fragment.classification == "safe" for fragment in self.fragments
        )

    def render_report(self) -> str:
        """Render a deterministic, dependency-free text report."""

        lines = [
            "Transaction boundary analysis",
            "============================",
            "fragments: %d" % len(self.fragments),
            "unknown operations: %d" % self.unknown_count,
            "",
        ]
        for fragment in self.fragments:
            lines.append(fragment.describe())
            for item in fragment.operations:
                path = ".".join(str(i) for i in item.path)
                lines.append(
                    "  - op[%d] path=%s %s: %s"
                    % (
                        item.index,
                        path,
                        type(item.op).__name__,
                        item.reason,
                    )
                )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _sql_text_for(op: ExecuteSQLOp) -> str:
    sqltext: object = op.sqltext
    if isinstance(sqltext, str):
        return sqltext
    # TextClause/Executable: read the literal SQL only; never compile against
    # a dialect (no database contact, no dialect inference).
    compiled = getattr(sqltext, "text", None)
    if compiled is None:
        return ""
    return str(compiled)


def _is_sql_boundary(op: ExecuteSQLOp) -> bool:
    text = _sql_text_for(op).strip()
    if text.endswith(";"):
        text = text[:-1].strip()
    return text == BOUNDARY_SQL_SENTINEL


def _is_marker(op: MigrateOperation) -> bool:
    if isinstance(op, TransactionBoundaryOp):
        return True
    return isinstance(op, ExecuteSQLOp) and _is_sql_boundary(op)


def _classify(op: MigrateOperation) -> tuple[Classification, str]:
    """Classify one leaf operation. Pure and read-only."""

    if isinstance(op, TransactionBoundaryOp):
        return (
            "nontransactional",
            "explicit transaction boundary marker",
        )
    if isinstance(op, ExecuteSQLOp):
        if _is_sql_boundary(op):
            return (
                "nontransactional",
                "explicit transaction boundary marker (SQL sentinel)",
            )
        return (
            "unknown",
            "raw SQL cannot be inspected without compiling/running it; "
            "transaction safety not asserted",
        )
    if isinstance(op, CreateIndexOp):
        if op.kw.get("postgresql_concurrently"):
            return (
                "nontransactional",
                "CREATE INDEX CONCURRENTLY cannot run inside a transaction",
            )
        return (
            "safe",
            "ordinary CREATE INDEX is transactional on supported backends",
        )
    if isinstance(op, DropIndexOp):
        if op.kw.get("postgresql_concurrently"):
            return (
                "nontransactional",
                "DROP INDEX CONCURRENTLY cannot run inside a transaction",
            )
        return (
            "safe",
            "ordinary DROP INDEX is transactional on supported backends",
        )
    for known in _KNOWN_TRANSACTIONAL_OPS:
        if isinstance(op, known):
            return (
                "safe",
                "known transactional operation (%s)" % type(op).__name__,
            )
    # Custom/third-party ops and anything else unrecognised stay unknown;
    # the analyzer never claims safety on their behalf.
    return (
        "unknown",
        "unrecognised operation type %s; no transaction-safety rule applies"
        % type(op).__name__,
    )


def _iter_leaves(
    node: OperationTree, path: IndexPath = ()
) -> Iterator[tuple[MigrateOperation, IndexPath]]:
    """Yield ``(leaf op, index path)`` pairs in original execution order.

    Containers -- including empty :class:`.ModifyTableOps` batch
    containers -- are descended into read-only and are never emitted as
    leaves.
    """

    current: OperationTree
    if isinstance(node, MigrationScript):
        # A rendered script may carry one or several upgrade streams
        # (multi-database template).
        streams = node.upgrade_ops_list
        current = streams[0] if len(streams) == 1 else list(streams)
    else:
        current = node
    if isinstance(current, OpContainer):
        children: Sequence[MigrateOperation] = current.ops
    elif isinstance(current, (list, tuple)):
        children = current
    else:
        # Not a container: current is a leaf MigrateOperation.
        yield cast(MigrateOperation, current), path
        return
    for idx, child in enumerate(children):
        child_path = path + (idx,)
        if isinstance(child, (OpContainer, list, tuple)):
            yield from _iter_leaves(child, child_path)
        else:
            yield child, child_path


def analyze_transaction_boundaries(
    operations: OperationTree,
) -> TransactionAnalysis:
    """Split an operation tree into ordered transaction-boundary fragments.

    ``operations`` may be an :class:`~alembic.operations.ops.UpgradeOps`,
    :class:`~alembic.operations.ops.DowngradeOps`,
    :class:`~alembic.operations.ops.ModifyTableOps`,
    :class:`~alembic.operations.ops.MigrationScript`, a plain sequence of
    operations, or any nested combination of these.

    The input objects are never mutated, no database connection is made and
    no SQL is executed or compiled against a dialect.

    Fragment rules:

    * contiguous operations sharing one classification form one fragment;
    * an explicit boundary marker (:class:`TransactionBoundaryOp`, or an
      :class:`~alembic.operations.ops.ExecuteSQLOp` whose text is exactly
      :data:`BOUNDARY_SQL_SENTINEL`) terminates the current fragment without
      producing a fragment itself;
    * unknown operations always form their own ``"unknown"`` fragments and
      are never folded into ``"safe"`` fragments;
    * leading, trailing or repeated markers, as well as empty containers,
      never produce empty fragments.
    """

    items: list[OperationItem] = []
    flat_index = 0
    for op, op_path in _iter_leaves(operations):
        classification, reason = _classify(op)
        items.append(
            OperationItem(
                op=op,
                path=op_path,
                index=flat_index,
                classification=classification,
                reason=reason,
            )
        )
        flat_index += 1

    fragments: list[Fragment] = []
    current: list[OperationItem] = []

    def flush() -> None:
        if current:
            fragments.append(
                Fragment(
                    sequence=len(fragments) + 1,
                    classification=current[0].classification,
                    operations=list(current),
                )
            )
            current.clear()

    for item in items:
        if _is_marker(item.op):
            # A marker is an explicit commit point: terminate the fragment
            # before it but emit nothing for the marker itself.
            flush()
            continue
        if current and current[-1].classification != item.classification:
            flush()
        current.append(item)
    flush()

    return TransactionAnalysis(fragments=fragments)
