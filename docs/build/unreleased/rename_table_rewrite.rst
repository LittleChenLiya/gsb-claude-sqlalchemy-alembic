.. change::
    :tags: feature, operations

    Added :mod:`alembic.operations.rewrite`, a database-free toolkit for
    renaming the logical table name referenced by an already-generated
    operation tree.  :class:`.RenameTablePlan` provides a read-only plan
    view of the changes and its :meth:`~.RenameTablePlan.apply` method
    returns a rewritten replica of the tree, leaving the original
    operations and their ``.info`` dictionaries untouched.  Table identity
    is the ``(table_name, schema)`` pair, so same-named tables in other
    schemas are unaffected; column, index and foreign key operations are
    retargeted, and any unsupported operation rejects the whole batch.
