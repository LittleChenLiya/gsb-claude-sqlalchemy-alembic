.. _boundary_analysis:

=================================
Boundary Analysis for Migrations
=================================

.. note:: This section describes the **boundary analyzer**, a static
   analysis tool that examines migration operation trees to determine
   which contiguous segments can maintain a transaction boundary.  The
   analyzer does not execute SQL, does not connect to a database, and does
   not mutate the operation tree or schema objects.

.. versionadded:: 1.14

Overview
========

When running migrations, Alembic wraps each migration in a transaction (for
databases that support transactional DDL).  However, some operations cannot
run within a transaction boundary -- for example, PostgreSQL's
``CREATE INDEX CONCURRENTLY`` or ``ALTER TYPE ... ADD VALUE``.  When such
operations are present, the transaction must be committed before the
operation and a new transaction started afterward.

The **boundary analyzer** examines a migration operation tree and produces
an ordered list of **segments**.  Each segment represents a contiguous run
of operations and indicates whether those operations can maintain a
transaction boundary.

The analyzer is purely static: it reads the operation tree in memory and
classifies each operation based on known rules.  It does **not** connect
to a database, execute SQL, or infer capabilities from a live connection.

Key concepts
------------

* **Segment** -- a contiguous run of operations in the original operation
  tree.  Each segment has a status: ``safe``, ``unknown``, or ``bulk``.
* **Boundary marker** -- an explicit marker (:class:`.BoundaryMarker`) that
  splits segments.  When the analyzer encounters a boundary marker, it
  finalizes the current segment and records the boundary position.
* **Bulk table operation** -- a :class:`.ModifyTableOps` (batch alter
  table) operation.  Bulk operations are treated as a single unit.
* **Unknown operation** -- an operation that the analyzer cannot verify as
  safe within a transaction.  Unknown operations are marked ``unknown``;
  the analyzer does not claim they are safe.

Classification rules
====================

The analyzer classifies each operation into one of the following categories:

.. list-table:: Classification rules
   :header-rows: 1
   :widths: 20 80

   * - Category
     - Description
   * - ``safe``
     - Known DDL operations that are safe within a transaction (e.g.
       :class:`.AddColumnOp`, :class:`.CreateIndexOp`,
       :class:`.CreateTableOp`, :class:`.DropColumnOp`).
   * ``unknown``
     - Operations that cannot be verified as safe without executing SQL
       (e.g. :class:`.ExecuteSQLOp`) or operations that are not recognized
       by the analyzer (custom operation subclasses).
   * ``bulk``
     - Non-empty :class:`.ModifyTableOps` (batch table operations) whose
       inner operations are all known-safe.
   * ``boundary``
     - :class:`.BoundaryMarker` operations that split segments.

Special cases
-------------

* **Empty bulk containers** -- an empty :class:`.ModifyTableOps` does not
  generate a segment.  The analyzer skips it entirely.
* **Bulk with unknown inner operations** -- if a bulk container contains an
  unknown inner operation, the entire bulk segment is marked ``unknown``.
* **ExecuteSQLOp** -- always marked ``unknown``, because arbitrary SQL
  (e.g. ``COMMIT``, ``VACUUM``, ``CREATE INDEX CONCURRENTLY``) may not be
  safe within a transaction.
* **Consecutive unknown operations** -- grouped into a single ``unknown``
  segment.

Usage
=====

Basic usage
-----------

The simplest way to use the analyzer is with the :func:`.analyze_operations`
convenience function::

    from alembic.operations import analyze_operations
    from alembic.operations.ops import AddColumnOp, CreateIndexOp
    from sqlalchemy import Column, Integer

    operations = [
        AddColumnOp("t1", Column("a", Integer)),
        CreateIndexOp("ix_t1_a", "t1", ["a"]),
    ]

    report = analyze_operations(operations)

    for segment in report.segments:
        print(f"Segment {segment.index}: {segment.status.value}")
        print(f"  Positions: {segment.start_position}-{segment.end_position}")
        for pos, op in zip(segment.positions, segment.operations):
            print(f"  [{pos}] {type(op).__name__}")

Using the BoundaryAnalyzer class
---------------------------------

For more control, use the :class:`.BoundaryAnalyzer` class directly::

    from alembic.operations import BoundaryAnalyzer

    analyzer = BoundaryAnalyzer()
    report = analyzer.analyze(operations)

Adding explicit boundary markers
---------------------------------

To mark an explicit transaction boundary, insert a
:class:`.BoundaryMarker` into the operation tree::

    from alembic.operations import BoundaryMarker

    operations = [
        AddColumnOp("t1", Column("a", Integer)),
        BoundaryMarker(reason="switch to autocommit"),
        CreateIndexOp("ix_t1_a", "t1", ["a"]),
    ]

    report = analyze_operations(operations)
    # report.segments has 2 segments: [AddColumnOp] and [CreateIndexOp]
    # report.boundaries has 1 entry: [1]

The boundary marker itself is not included in any segment; instead its
position is recorded in ``report.boundaries``.

Generating a report
-------------------

The :class:`.BoundaryReport` object has a :meth:`~.BoundaryReport.summary`
method that produces a human-readable report::

    print(report.summary())

Example output::

    Boundary Analysis Report
    ========================================
    Total operations: 3
    Total positions:  3
    Boundaries:       1
    Segments:         2

    Segment 0: SAFE (positions 0-0, 1 operation(s))
      [0] AddColumnOp(table='t1')

    Segment 1: SAFE (positions 2-2, 1 operation(s))
      [2] CreateIndexOp(table='t1')

    Boundary positions:
      - position 1

Demo
====

A demo script is available at ``tools/boundary_demo.py``.  It builds an
in-memory operation tree with boundaries and unknown operations, runs the
analyzer, and prints the report.  The demo runs entirely in memory and
completes in well under 90 seconds::

    python tools/boundary_demo.py

API Reference
=============

.. autoclass:: BoundaryAnalyzer
   :members:
   :undoc-members:

.. autoclass:: BoundaryMarker
   :members:
   :undoc-members:

.. autoclass:: BoundaryReport
   :members:
   :undoc-members:

.. autoclass:: Segment
   :members:
   :undoc-members:

.. autoclass:: SegmentStatus
   :members:
   :undoc-members:

.. autofunction:: analyze_operations

