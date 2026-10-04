.. change::
    :tags: feature, autogenerate
    :tickets:

    Added :meth:`.Rewriter.scope`, a context manager that installs a local
    registration scope on a :class:`.Rewriter`. Rewrite rules registered
    through the returned scope object only override the active rules for
    the duration of the ``with`` block; nested scopes are restored in
    last-in/first-out order on exit, restoration also takes place when an
    exception is raised, and temporary registrations on one
    :class:`.Rewriter` are never visible to another independently created
    instance. Traversal "already processed" markers are now scoped to a
    single traversal as well, so that traversing the same operation tree a
    second time runs the currently active rules again rather than skipping
    directives marked during the previous traversal.
