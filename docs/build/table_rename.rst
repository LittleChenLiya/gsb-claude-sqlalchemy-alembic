.. _table_rename:

======================================
Logical Table Rename Across an Op Tree
======================================

.. note:: This section discusses the **internal API of Alembic** as regards
   the internal system of defining migration operation directives.
   This section is only useful for developers who wish to extend the
   capabilities of Alembic.

The :func:`.plan_table_rename` and :func:`.apply_table_rename` functions
provide a post-generation transformation that uniformly renames a table's
logical identity -- the tuple ``(schema, table_name)`` -- across an
operation tree produced by autogenerate, without touching the database and
without mutating the original tree.

This is useful when autogenerate produces operations for the wrong logical
table name (for example, due to a schema or naming mismatch) and you want to
fix the generated tree before it is rendered to a migration script.

.. _table_rename_overview:

Overview
========

Two interfaces are provided:

* :func:`.plan_table_rename` traverses the tree and returns a read-only
  :class:`.TableRenamePlan` describing every operation that would be
  modified.  The tree itself is not changed.
* :func:`.apply_table_rename` returns a deep copy of the tree with the
  rename applied.  The original tree is left untouched.

Only a known set of operation types are supported:

* :class:`.CreateTableOp` and :class:`.DropTableOp`
* :class:`.AddColumnOp` and :class:`.DropColumnOp`
* :class:`.CreateIndexOp` and :class:`.DropIndexOp`
* :class:`.CreateForeignKeyOp`
* the :class:`.ModifyTableOps` container

Any other operation that may reference the table -- in particular
user-defined custom operations -- causes
:class:`.UnsupportedTableRenameError` to be raised before any modification
is made, so a partially modified tree is never produced.

.. _table_rename_identity:

Table and Schema as Identity
============================

The table identity is the tuple ``(schema, table_name)``.  When matching,
both the schema and the table name must match.  This means that renaming
``sales.orders`` to ``sales.purchases`` will not touch a table named
``orders`` in another schema (e.g. ``public.orders``).

The rename only changes the ``table_name``; the ``schema`` stays the same.

.. _table_rename_example:

Example
=======

The following example builds an operation tree, plans a rename, and applies
it to a copy -- all without a database connection::

    from sqlalchemy import Column, Integer, String
    from alembic.operations.ops import (
        AddColumnOp,
        CreateForeignKeyOp,
        CreateIndexOp,
        CreateTableOp,
        UpgradeOps,
    )
    from alembic.operations import (
        apply_table_rename,
        op_tree_to_str,
        plan_table_rename,
    )

    upgrade = UpgradeOps([
        CreateTableOp(
            "orders",
            [Column("id", Integer), Column("name", String(50))],
            schema="sales",
        ),
        AddColumnOp("orders", Column("email", String(100)), schema="sales"),
        CreateIndexOp("ix_orders_name", "orders", ["name"], schema="sales"),
        CreateForeignKeyOp(
            "fk_orders_cust",
            "orders",
            "customers",
            ["cust_id"],
            ["id"],
            source_schema="sales",
            referent_schema="sales",
        ),
        # public.orders is in a different schema and must not be touched
        CreateTableOp("orders", [Column("id", Integer)], schema="public"),
    ])

    # Plan (read-only; the tree is not modified)
    plan = plan_table_rename(upgrade, "orders", "purchases", schema="sales")
    print(plan)
    # TableRenamePlan('sales.orders' -> 'sales.purchases', operations=4)

    # Apply to a deep copy (the original is left untouched)
    new_upgrade = apply_table_rename(
        upgrade, "orders", "purchases", schema="sales"
    )

    print(op_tree_to_str(new_upgrade))
    # UpgradeOps()
    #   CreateTableOp(sales.purchases)
    #   AddColumnOp(sales.purchases)
    #   CreateIndexOp(sales.purchases)
    #   CreateForeignKeyOp(sales.purchases -> sales.customers)
    #   CreateTableOp(public.orders)

The ``op_tree_to_str`` function is a database-free debugging aid that walks
the tree and prints each operation together with its table identity.

.. _table_rename_unsupported:

Unsupported Operations
======================

If the tree contains an operation that is not one of the supported types
and may reference the table being renamed,
:class:`.UnsupportedTableRenameError` is raised.  This includes any
user-defined custom operation, since it may hold a reference to the table in
any way.  The error is raised during the planning phase, before any copy is
made, so the original tree is left unchanged::

    from alembic.operations import MigrateOperation

    class CustomOp(MigrateOperation):
        def __init__(self, table_name):
            self.table_name = table_name

    upgrade = UpgradeOps([
        AddColumnOp("orders", Column("a", String(10)), schema="sales"),
        CustomOp("orders"),
    ])

    plan_table_rename(upgrade, "orders", "purchases", schema="sales")
    # UnsupportedTableRenameError: cannot rename table sales.orders:
    # unsupported operation 'CustomOp' may reference the table

.. seealso::

    :ref:`operation_objects`

    :ref:`customizing_revision`
