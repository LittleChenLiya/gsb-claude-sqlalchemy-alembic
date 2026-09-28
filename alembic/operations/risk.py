"""Read-only risk analysis over migration operation trees.

This module provides :func:`analyze_ops`, a small reporting interface
that walks an existing :class:`.UpgradeOps` or :class:`.DowngradeOps`
object (recursing into nested :class:`.ModifyTableOps` containers) and
reports operations that typically deserve extra attention before a
migration goes live:

* dropping a table
* dropping a column
* shortening the length of a string column
* changing a nullable column to not-nullable

The analysis only uses information already carried by the operation
objects themselves.  It never connects to a database, never inspects
live schema, and never executes or mutates the operation tree.  When
the operation object does not carry enough information to decide (for
example "existing_type" or "existing_nullable" was not given), the
record is emitted with "uncertain=True" and a "无法判断" reason
instead of guessing.

A reported risk is informational only; it does not prevent the
operation from running, and the operation tree as well as the SQL
generated from it are left untouched.

Usage::

    from alembic.operations.ops import UpgradeOps
    from alembic.operations.risk import analyze_ops

    upgrade_ops = UpgradeOps(ops=[...])
    for record in analyze_ops(upgrade_ops):
        print(record.position, record.operation, record.reason)

A self-contained demonstration that requires no database connection is
available via::

    python -m alembic.operations.risk

"""

from __future__ import annotations

import dataclasses
from typing import List
from typing import Optional
from typing import Union

from sqlalchemy import String

from .ops import AlterColumnOp
from .ops import DowngradeOps
from .ops import DropColumnOp
from .ops import DropTableOp
from .ops import MigrateOperation
from .ops import OpContainer
from .ops import UpgradeOps

UNKNOWN_REASON = "无法判断"


@dataclasses.dataclass(frozen=True)
class RiskRecord:
    """A single risky-operation finding within an operation tree."""

    operation: str
    """Short identifier of the operation kind, e.g. "drop_table"."""

    table_name: Optional[str]
    """Name of the table the operation targets, if applicable."""

    column_name: Optional[str]
    """Name of the column the operation targets, if applicable."""

    position: str
    """Position of the operation within the original operation tree,
    e.g. "ops[1].ops[0]"."""

    reason: str
    """Human readable explanation of why the operation was flagged."""

    uncertain: bool = False
    """True when the operation object did not carry enough information
    to decide and the record is marked as "无法判断" instead of a
    definite risk."""


def analyze_ops(ops: Union[UpgradeOps, DowngradeOps]) -> List[RiskRecord]:
    """Return risk records for an upgrade or downgrade operation tree.

    The tree is walked in its original order, recursing into
    :class:`.ModifyTableOps` containers.  The input object is only read,
    never modified, and nothing is executed against a database.
    """
    records: List[RiskRecord] = []
    for index, op in enumerate(ops.ops):
        _collect(op, "ops[%d]" % index, records)
    return records


def _collect(
    op: MigrateOperation, position: str, records: List[RiskRecord]
) -> None:
    if isinstance(op, OpContainer):
        for index, sub_op in enumerate(op.ops):
            _collect(sub_op, "%s.ops[%d]" % (position, index), records)
        return

    if isinstance(op, DropTableOp):
        records.append(
            RiskRecord(
                operation="drop_table",
                table_name=op.table_name,
                column_name=None,
                position=position,
                reason="删除表，表中数据将一并丢失",
            )
        )
    elif isinstance(op, DropColumnOp):
        records.append(
            RiskRecord(
                operation="drop_column",
                table_name=op.table_name,
                column_name=op.column_name,
                position=position,
                reason="删除列，列中数据将一并丢失",
            )
        )
    elif isinstance(op, AlterColumnOp):
        _collect_alter_column(op, position, records)


def _collect_alter_column(
    op: AlterColumnOp, position: str, records: List[RiskRecord]
) -> None:
    if op.modify_type is not None:
        existing_type = op.existing_type
        modify_type = op.modify_type
        if existing_type is None:
            records.append(
                RiskRecord(
                    operation="modify_type",
                    table_name=op.table_name,
                    column_name=op.column_name,
                    position=position,
                    reason="%s：操作对象未携带旧类型信息" % UNKNOWN_REASON,
                    uncertain=True,
                )
            )
        else:
            old_length = getattr(existing_type, "length", None)
            new_length = getattr(modify_type, "length", None)
            if (
                isinstance(existing_type, String)
                and isinstance(modify_type, String)
                and old_length is not None
                and new_length is not None
                and new_length < old_length
            ):
                records.append(
                    RiskRecord(
                        operation="modify_type",
                        table_name=op.table_name,
                        column_name=op.column_name,
                        position=position,
                        reason="字符串长度由 %s 缩短为 %s，可能截断已有数据"
                        % (old_length, new_length),
                    )
                )

    if op.modify_nullable is not None and op.modify_nullable is False:
        if op.existing_nullable is None:
            records.append(
                RiskRecord(
                    operation="modify_nullable",
                    table_name=op.table_name,
                    column_name=op.column_name,
                    position=position,
                    reason="%s：操作对象未携带旧 nullable 信息"
                    % UNKNOWN_REASON,
                    uncertain=True,
                )
            )
        elif op.existing_nullable is True:
            records.append(
                RiskRecord(
                    operation="modify_nullable",
                    table_name=op.table_name,
                    column_name=op.column_name,
                    position=position,
                    reason="列由可空改为不可空，已有 NULL 值会导致迁移失败",
                )
            )


def _demo() -> None:
    """Print a sample risk report without touching any database."""
    import sqlalchemy as sa

    from .ops import AddColumnOp
    from .ops import ModifyTableOps

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

    diffs_before = repr(upgrade_ops.as_diffs())
    snapshot = list(upgrade_ops.ops)

    records = analyze_ops(upgrade_ops)

    print("风险清单（%d 条）：" % len(records))
    for record in records:
        target = record.table_name or "-"
        if record.column_name:
            target = "%s.%s" % (target, record.column_name)
        marker = " [无法判断]" if record.uncertain else ""
        print(
            "  %s %-16s %-24s %s%s"
            % (
                record.position,
                record.operation,
                target,
                record.reason,
                marker,
            )
        )

    assert list(upgrade_ops.ops) == snapshot
    assert repr(upgrade_ops.as_diffs()) == diffs_before
    print("输入操作树未发生变化（校验通过）")


if __name__ == "__main__":
    _demo()
