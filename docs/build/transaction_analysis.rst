=============================
Transaction Boundary Analysis
=============================

.. currentmodule:: alembic.analysis

Overview
========

Before generating an offline SQL migration script (``alembic upgrade
--sql``) it is useful to know which contiguous sections of a migration can
be wrapped in a single transaction and which sections must be committed
separately -- for example because they contain ``CREATE INDEX
CONCURRENTLY`` or other statements that cannot run inside a transaction.

The :mod:`alembic.analysis` package provides a **static analyzer** for
exactly this preview.

* It executes **no SQL**.
* It **never connects to a database** and never compiles a statement
  against a dialect to infer capabilities.
* It only **reads** the operation tree; the schema objects and the
  operations themselves are never mutated.
* Anything it cannot prove is reported as ``unknown`` -- the analyzer never
  claims an operation is safe merely because it is unfamiliar.

The public entry point is
:func:`~alembic.analysis.analyze_transaction_boundaries`::

    from alembic.analysis import analyze_transaction_boundaries

    analysis = analyze_transaction_boundaries(upgrade_ops)

    for fragment in analysis.fragments:
        print(fragment.describe())

A self-contained, in-memory demonstration is also available::

    python -m alembic.analysis

It builds operation objects directly, prints a report and finishes
in well under a second without any database or configuration.

Classifications
===============

Each leaf operation is assigned exactly one classification:

``safe``
    A concrete, well-known operation that is ordinarily allowed inside a
    transaction, such as :class:`~alembic.operations.ops.AddColumnOp`,
    :class:`~alembic.operations.ops.AddConstraintOp`,
    :class:`~alembic.operations.ops.AlterColumnOp`,
    :class:`~alembic.operations.ops.BulkInsertOp`,
    :class:`~alembic.operations.ops.CreateTableOp`,
    :class:`~alembic.operations.ops.DropColumnOp`,
    :class:`~alembic.operations.ops.DropConstraintOp`,
    :class:`~alembic.operations.ops.DropTableOp`,
    :class:`~alembic.operations.ops.RenameTableOp`, a plain
    :class:`~alembic.operations.ops.CreateIndexOp` /
    :class:`~alembic.operations.ops.DropIndexOp`, and the table/column
    comment operations (subclasses of
    :class:`~alembic.operations.ops.AlterTableOp`).

``nontransactional``
    A known operation that must run outside a transaction.  Currently this
    means:

    * :class:`~alembic.operations.ops.CreateIndexOp` created with
      ``postgresql_concurrently=True`` (``CREATE INDEX CONCURRENTLY``);
    * :class:`~alembic.operations.ops.DropIndexOp` with the same flag;
    * an explicit boundary marker (see below).

``unknown``
    The analyzer cannot decide from the object alone.  This includes:

    * :class:`~alembic.operations.ops.ExecuteSQLOp` carrying arbitrary SQL
      (inspecting its text would require compiling or executing it);
    * custom or third-party :class:`~alembic.operations.ops.MigrateOperation`
      subclasses with no built-in rule.

    Unknown operations form their own fragments and are **never** merged
    into a ``safe`` fragment.  Each
    :class:`~alembic.analysis.OperationItem` carries a human-readable
    ``reason`` explaining the verdict.

Boundary markers
================

An explicit commit point is declared with
:class:`~alembic.analysis.TransactionBoundaryOp`::

    from alembic.analysis import TransactionBoundaryOp

    upgrade_ops = UpgradeOps(
        ops=[
            op_obj_add_column,
            TransactionBoundaryOp(note="commit before reindex"),
            op_obj_create_index,
        ]
    )

For migrations that only have raw SQL available, an
:class:`~alembic.operations.ops.ExecuteSQLOp` whose text is exactly the
sentinel

.. code-block:: text

    -- alembic:transaction-boundary

(ignoring surrounding whitespace and a trailing ``;``) is recognised as
the same marker.

A marker terminates the fragment preceding it but generates **no fragment
of its own**.  Leading, trailing or immediately repeated markers do not
produce empty fragments.

Fragment splitting rules
========================

Given the classified, ordered leaf operations:

1. Contiguous operations with the same classification form one
   :class:`~alembic.analysis.Fragment`.
2. A change of classification ends the current fragment.
3. An explicit boundary marker ends the current fragment -- even when the
   operations on both sides are both ``safe``.
4. An ``unknown`` operation always occupies an ``unknown`` fragment; it is
   never folded into an adjacent ``safe`` fragment.
5. Splitting never reorders operations.
6. Empty containers -- in particular an empty batch-mode
   :class:`~alembic.operations.ops.ModifyTableOps` -- yield nothing and so
   never produce an empty fragment.

Original operation positions
============================

Every :class:`~alembic.analysis.OperationItem` records:

* ``index`` -- the flat, zero-based execution-order position.  Every leaf
  occupies a position, including boundary markers, so ``index`` matches the
  op's place in the flattened source tree;
* ``path`` -- a tuple of zero-based container indices reaching the
  operation from the root that was analyzed, e.g. ``(1, 0, 2)`` means
  ``root.ops[1].ops[0].ops[2]``.

Containers (:class:`~alembic.operations.ops.UpgradeOps`,
:class:`~alembic.operations.ops.DowngradeOps`,
:class:`~alembic.operations.ops.ModifyTableOps`, plain lists and tuples)
are descended into recursively.  A
:class:`~alembic.operations.ops.MigrationScript` input is analyzed through
its ``upgrade_ops``.

Report
======

:meth:`.TransactionAnalysis.render_report` produces a deterministic,
dependency-free text report listing the ordered fragments, the original op
indices and paths, the operation type and the reason for the verdict::

    Transaction boundary analysis
    ============================
    fragments: 2
    unknown operations: 0

    fragment 1: safe (op[0])
      - op[0] path=0 AddColumnOp: known transactional operation (...)

    fragment 2: safe (op[2])
      - op[2] path=2 CreateIndexOp: ordinary CREATE INDEX ...

API
===

.. autofunction:: analyze_transaction_boundaries

.. autoclass:: TransactionAnalysis
    :members:

.. autoclass:: Fragment
    :members:

.. autoclass:: OperationItem
    :members:

.. autoclass:: TransactionBoundaryOp
    :members:

.. autodata:: BOUNDARY_SQL_SENTINEL
