.. change::
    :tags: feature, autogenerate

    Added support for explicitly managed CHECK constraints in autogenerate
    via the new
    :paramref:`.EnvironmentContext.configure.managed_check_constraints`
    parameter, which names specific CHECK constraints per table to be
    tracked.  Managed constraints are compared between the reflected
    database and the target metadata, generating add, drop, and
    drop+create expression-replacement operations; expression comparison
    conservatively normalizes only whitespace and redundant outer
    parentheses while preserving string literals, quoted identifiers,
    case, and token order.  On SQLite the generated operations are
    rendered using the batch "recreate the table" strategy; other
    backends use the standard constraint DDL operations.  Constraints
    not named in the option keep their existing handling.  See
    :ref:`autogenerate_managed_check_constraints`.
