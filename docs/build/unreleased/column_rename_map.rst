.. change::
    :tags: feature, autogenerate

    Added the :paramref:`.EnvironmentContext.configure.column_rename_map`
    parameter, which allows intended column renames to be declared so that
    autogenerate emits a single :class:`.AlterColumnOp` carrying
    ``new_column_name`` instead of a data-destroying ``drop_column()`` /
    ``add_column()`` pair.  Entries are keyed by ``(schema, table,
    old_column_name)`` and are fully validated before any operations are
    generated; invalid entries abort the run with a diagnostic naming the
    table and columns involved.  Type, nullability, and server default
    changes coinciding with a rename are preserved on the same operation,
    and constraint, index, and foreign key comparison (including composite
    foreign keys and explicitly named constraints) matches columns by
    their renamed identity.  See :ref:`autogenerate_column_renames`.
