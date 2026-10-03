.. change::
    :tags: usecase, operations

    Added the new :mod:`alembic.analysis` package with a static,
    no-database transaction-boundary analyzer
    :func:`~alembic.analysis.analyze_transaction_boundaries`.  Given an
    operation tree (or :class:`~alembic.operations.ops.MigrationScript`),
    it returns ordered fragments of operations that can be expected to run
    inside a single transaction, preserving each operation's original
    position.  Known operations are classified as ``safe``; operations
    known to require running outside of a transaction (such as
    ``CREATE INDEX CONCURRENTLY``) and explicit
    :class:`~alembic.analysis.TransactionBoundaryOp` markers are classified
    as ``nontransactional``; raw SQL and unrecognised custom operations are
    reported as ``unknown`` with a reason rather than assumed safe.  The
    analyzer executes no SQL, makes no database connection and never
    mutates its inputs.  A report demo is available via
    ``python -m alembic.analysis``.
