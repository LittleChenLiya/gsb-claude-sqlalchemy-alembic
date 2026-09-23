.. change::
    :tags: feature, autogenerate

    Added the :paramref:`.EnvironmentContext.configure.column_renames`
    option, an optional mapping of ``(schema, table_name, old_column_name)``
    to new column names that lets autogenerate render column renames as
    reversible :meth:`.Operations.alter_column` operations with
    ``new_column_name`` set, instead of data-destroying drop/add column
    pairs.  Mappings are validated in full before any operations are
    generated; indexes, unique constraints, and foreign keys (including
    composite foreign keys) that span a renamed column are matched by
    their new column identity.

