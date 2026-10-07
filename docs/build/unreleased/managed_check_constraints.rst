.. change::
    :tags: feature, autogenerate

    Added support for explicitly managed CHECK constraints in autogenerate via
    the new :paramref:`.EnvironmentContext.configure.managed_check_constraints`
    parameter.  The parameter maps table names to collections of CHECK
    constraint names to track; autogenerate compares each listed constraint
    against the database and generates add, drop, and expression-replacement
    (drop + create) operations.  Expression comparison normalizes insignificant
    whitespace and redundant outer parentheses only, preserving string literals,
    quoted identifiers, and operator order, and conservatively reports a
    difference when an expression cannot be normalized.  On SQLite the
    operations are rendered using batch mode (table recreate); other backends
    use the standard constraint DDL directives.  Downgrades restore the
    original expression text.  See :ref:`autogenerate_managed_check_constraints`.
