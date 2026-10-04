Rewriter temporary-rule scope demo
==================================

This example demonstrates :meth:`.Rewriter.scope`, a context manager that
installs temporary rewrite rules on a :class:`~alembic.autogenerate.Rewriter`
and restores the previous rules on exit (including when a traversal raises).

It builds an in-memory operation tree - the same kind of object passed to
``process_revision_directives`` during autogeneration - and never connects
to a database.

Run it from the root of an Alembic checkout with the local package
importable, e.g.::

    python examples/rewriter_scope_demo/rewriter_scope_demo.py

Expected output shows the column name being marked ``A`` by the permanent
rule, ``B`` by a temporary rule inside a scope, and ``A`` again once the
scope is exited, followed by a second traversal of the same tree that is
not skipped.
