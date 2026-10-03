#!/usr/bin/env python
"""Demo: boundary analysis report for migration operation trees.

This script demonstrates the boundary analyzer by building an in-memory
operation tree, running the analyzer, and printing the report.

The demo runs entirely in memory; no database connection is required.
It completes in well under 90 seconds.

Usage::

    python tools/boundary_demo.py

"""

from __future__ import annotations

import os
import sys
import time

# Ensure the local worktree takes precedence over any installed package.
_worktree = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _worktree not in sys.path:
    sys.path.insert(0, _worktree)

from sqlalchemy import Column
from sqlalchemy import Integer

from alembic.operations import BoundaryMarker
from alembic.operations import analyze_operations
from alembic.operations.ops import AddColumnOp
from alembic.operations.ops import CreateIndexOp
from alembic.operations.ops import DropColumnOp
from alembic.operations.ops import ExecuteSQLOp
from alembic.operations.ops import ModifyTableOps


class _CustomOp:
    """A custom unknown operation for demonstration purposes."""

    def reverse(self):
        return _CustomOp()

    def to_diff_tuple(self):
        return ("custom",)


def build_demo_operations():
    """Build a demo operation tree with boundaries and unknown operations."""
    return [
        # Safe: add column
        AddColumnOp("users", Column("email", Integer)),
        # Safe: create index
        CreateIndexOp("ix_users_email", "users", ["email"]),
        # Explicit boundary marker
        BoundaryMarker(reason="switch to autocommit for special op"),
        # Unknown: custom operation (not recognized by analyzer)
        _CustomOp(),
        # Unknown: ExecuteSQLOp (cannot verify safety without executing SQL)
        ExecuteSQLOp("VACUUM FULL"),
        # Explicit boundary marker
        BoundaryMarker(reason="return to transactional DDL"),
        # Bulk table operation (batch alter table)
        ModifyTableOps(
            "users",
            [
                AddColumnOp("users", Column("name", Integer)),
                DropColumnOp("users", "old_name"),
            ],
        ),
        # Safe: drop column
        DropColumnOp("users", "email"),
    ]


def main() -> None:
    print("=" * 60)
    print("Boundary Analysis Demo")
    print("=" * 60)
    print()
    print("Building in-memory operation tree...")
    print()

    operations = build_demo_operations()

    print(f"Operation tree has {len(operations)} top-level operations:")
    for i, op in enumerate(operations):
        print(f"  [{i}] {type(op).__name__}")
    print()

    print("Running boundary analyzer (no SQL execution)...")
    start = time.monotonic()
    report = analyze_operations(operations)
    elapsed = time.monotonic() - start
    print(f"Analysis completed in {elapsed:.4f} seconds")
    print()

    print(report.summary())
    print()

    # Print safety conclusion
    print("=" * 60)
    print("Safety Conclusion")
    print("=" * 60)
    safe_count = sum(1 for s in report.segments if s.status.value == "safe")
    unknown_count = sum(1 for s in report.segments if s.status.value == "unknown")
    bulk_count = sum(1 for s in report.segments if s.status.value == "bulk")
    print(f"  Safe segments:    {safe_count}")
    print(f"  Unknown segments: {unknown_count} (cannot claim safety)")
    print(f"  Bulk segments:    {bulk_count}")
    print(f"  Boundaries:       {len(report.boundaries)}")
    print()

    if unknown_count > 0:
        print("WARNING: Some segments contain unknown operations.")
        print("These segments cannot be verified as safe within a")
        print("transaction boundary. Review them manually before running.")
    else:
        print("All segments are safe within transaction boundaries.")
    print()


if __name__ == "__main__":
    main()
