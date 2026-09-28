"""Database-free demonstration of :func:`.risks.analyze_risks`.

Run with::

    python -m alembic.operations

The script builds an :class:`.UpgradeOps` tree in memory, prints the
reported risks and verifies that analysis leaves the input tree
unchanged.  No database connection is made and no SQL is executed.

"""

from __future__ import annotations

import pickle

from sqlalchemy import Column
from sqlalchemy import String

from .ops import AddColumnOp
from .ops import AlterColumnOp
from .ops import DropColumnOp
from .ops import DropTableOp
from .ops import ModifyTableOps
from .ops import UpgradeOps
from .risks import RiskRecord
from .risks import analyze_risks


def _build_demo_ops() -> UpgradeOps:
    return UpgradeOps(
        [
            DropTableOp("obsolete_cache"),
            ModifyTableOps(
                "account",
                [
                    AlterColumnOp(
                        "account",
                        "name",
                        existing_type=String(100),
                        modify_type=String(20),
                        existing_nullable=True,
                    ),
                    DropColumnOp("account", "legacy_token"),
                    AddColumnOp(
                        "account",
                        Column("note", String(50), nullable=True),
                    ),
                ],
            ),
        ]
    )


def _print_risks(risks: list[RiskRecord]) -> None:
    print("迁移操作树风险清单（仅为提示，不代表禁止操作）")
    print("=" * 72)
    if not risks:
        print("未发现需要特别留意的操作。")
        return

    for index, risk in enumerate(risks, 1):
        target = risk.table_name
        if risk.column_name is not None:
            target = f"{target}.{risk.column_name}"
        status = "无法判断" if not risk.determinable else "风险"
        print(
            f"{index}. [{status}] {risk.risk_type}  对象={target}  "
            f"位置={risk.location_text}"
        )
        print(f"    原因: {risk.reason}")


def main() -> None:
    upgrade_ops = _build_demo_ops()
    before = pickle.dumps(upgrade_ops)

    risks = analyze_risks(upgrade_ops)

    after = pickle.dumps(upgrade_ops)
    if after != before:
        raise AssertionError("风险分析修改了输入的操作树")

    _print_risks(risks)
    print("=" * 72)
    print(f"共 {len(risks)} 条记录；输入的 UpgradeOps 对象未发生变化。")


if __name__ == "__main__":
    main()
