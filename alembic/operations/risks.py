"""Static risk analysis for migration operation trees.

The functions in this module walk an already constructed
:class:`.UpgradeOps` or :class:`.DowngradeOps` object and report
operations that may be destructive or otherwise require extra care
before a migration is deployed:

* ``drop_table`` -- dropping a table
* ``drop_column`` -- dropping a column
* ``shorten_string`` -- shrinking the length of a string column
* ``not_nullable`` -- changing a column from nullable to not nullable

Analysis uses only the information already present on the operation
objects.  When an operation does not carry enough information to make a
determination (for example an :class:`.AlterColumnOp` that changes the
column type without an ``existing_type``), a risk is still reported with
:attr:`.RiskRecord.determinable` set to ``False`` and the reason
``"无法判断"``; the database is never inspected and the operation tree
is never executed or modified.

A report is informational only: reporting a risk does not forbid the
operation.

Usage::

    from alembic.operations.ops import UpgradeOps
    from alembic.operations.risks import analyze_risks

    risks = analyze_risks(upgrade_ops)
    for risk in risks:
        print(risk.risk_type, risk.table_name, risk.column_name, risk.reason)

A runnable, database-free demonstration is available via
``python -m alembic.operations``.

"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy.types import String

from .ops import AlterColumnOp
from .ops import DropColumnOp
from .ops import DropTableOp
from .ops import MigrateOperation
from .ops import OpContainer

__all__ = ["RiskRecord", "analyze_risks"]


@dataclass(frozen=True, slots=True)
class RiskRecord:
    """A single potentially risky operation found in an operation tree.

    :ivar risk_type: one of ``"drop_table"``, ``"drop_column"``,
     ``"shorten_string"`` or ``"not_nullable"``.
    :ivar table_name: name of the affected table.
    :ivar column_name: name of the affected column, or ``None`` for
     table level risks.
    :ivar location: index path from the root container to the operation,
     e.g. ``(0, 1)`` means the second operation inside the first
     container.
    :ivar reason: human readable explanation of the risk, in the same
     language used by the analysis rules.  ``"无法判断"`` indicates that
     the operation objects do not carry enough information to decide.
    :ivar determinable: ``False`` when the risk cannot be confirmed from
     the operation objects alone.

    """

    risk_type: str
    table_name: str
    column_name: str | None
    location: tuple[int, ...]
    reason: str
    determinable: bool = True

    @property
    def location_text(self) -> str:
        """A human readable rendering of :attr:`location`."""

        return "root[" + "][".join(str(idx) for idx in self.location) + "]"


def analyze_risks(ops: OpContainer) -> list[RiskRecord]:
    """Return risk records for an existing operation tree.

    :param ops: an :class:`.UpgradeOps` or :class:`.DowngradeOps`
     instance.  Other :class:`.OpContainer` instances, such as
     :class:`.ModifyTableOps`, are accepted as well so that subtrees can
     be analyzed directly.
    :return: risk records in the same order as the operations that
     produced them, using depth first pre-order traversal.

    The operation tree is inspected but never modified or executed.

    """

    if not isinstance(ops, OpContainer):
        raise TypeError(
            "analyze_risks() expects an OpContainer such as "
            f"UpgradeOps or DowngradeOps, got {type(ops).__name__}"
        )
    return list(_walk(ops, ()))


def _walk(ops: OpContainer, location: tuple[int, ...]) -> Iterator[RiskRecord]:
    for index, operation in enumerate(ops.ops):
        op_location = location + (index,)
        if isinstance(operation, OpContainer):
            yield from _walk(operation, op_location)
            continue
        yield from _analyze_operation(operation, op_location)


def _analyze_operation(
    operation: MigrateOperation, location: tuple[int, ...]
) -> Iterator[RiskRecord]:
    if isinstance(operation, DropTableOp):
        yield RiskRecord(
            risk_type="drop_table",
            table_name=operation.table_name,
            column_name=None,
            location=location,
            reason="删表会删除整表及其全部数据",
        )
    elif isinstance(operation, DropColumnOp):
        yield RiskRecord(
            risk_type="drop_column",
            table_name=operation.table_name,
            column_name=operation.column_name,
            location=location,
            reason="删列会删除该列中的全部数据",
        )
    elif isinstance(operation, AlterColumnOp):
        yield from _analyze_alter_column(operation, location)


def _analyze_alter_column(
    operation: AlterColumnOp, location: tuple[int, ...]
) -> Iterator[RiskRecord]:
    if operation.modify_type is not None:
        shorten_risk = _shorten_string_risk(operation, location)
        if shorten_risk is not None:
            yield shorten_risk

    if (
        operation.modify_nullable is False
        and operation.existing_nullable is not False
    ):
        if operation.existing_nullable is None:
            reason = "无法判断：操作对象未携带旧 nullable 信息"
            determinable = False
        else:
            reason = (
                "列由可空 (nullable=True) 改为不可空 (nullable=False)，"
                "已有数据可能导致约束失败"
            )
            determinable = True
        yield RiskRecord(
            risk_type="not_nullable",
            table_name=operation.table_name,
            column_name=operation.column_name,
            location=location,
            reason=reason,
            determinable=determinable,
        )


def _shorten_string_risk(
    operation: AlterColumnOp, location: tuple[int, ...]
) -> RiskRecord | None:
    old_string = _as_string(operation.existing_type)
    new_string = _as_string(operation.modify_type)

    # Only string -> string type changes can shorten a string.  Other
    # type changes are outside the scope of this rule and are not
    # guessed at.
    if old_string is None or new_string is None:
        if operation.existing_type is None:
            reason = "无法判断：操作对象未携带旧类型信息"
            return RiskRecord(
                risk_type="shorten_string",
                table_name=operation.table_name,
                column_name=operation.column_name,
                location=location,
                reason=reason,
                determinable=False,
            )
        return None

    old_length = old_string.length
    new_length = new_string.length
    if (
        not isinstance(old_length, int)
        or isinstance(old_length, bool)
        or not isinstance(new_length, int)
        or isinstance(new_length, bool)
    ):
        reason = "无法判断：操作对象中的字符串类型缺少可比较的长度"
        return RiskRecord(
            risk_type="shorten_string",
            table_name=operation.table_name,
            column_name=operation.column_name,
            location=location,
            reason=reason,
            determinable=False,
        )

    if old_length <= new_length:
        return None

    return RiskRecord(
        risk_type="shorten_string",
        table_name=operation.table_name,
        column_name=operation.column_name,
        location=location,
        reason=(
            f"字符串长度由 {old_length} 缩短为 {new_length}，"
            "超长数据可能被截断或导致约束失败"
        ),
    )


def _as_string(type_: Any) -> String | None:
    """Return ``type_`` as a :class:`.String` instance, or ``None``.

    Both type classes and type instances may appear on operation
    objects, so classes are instantiated before inspecting them.

    """

    if isinstance(type_, type):
        try:
            type_ = type_()
        except Exception:
            return None

    return type_ if isinstance(type_, String) else None
