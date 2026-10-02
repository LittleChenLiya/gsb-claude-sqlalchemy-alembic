# Copyright (C) 2009-2026 Alembic authors
#
# This module is part of Alembic and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php
"""Post-generation rewriting of Alembic operation trees.

This module provides a small, database-free toolkit for renaming the
*logical* table name referenced by an already-generated tree of
:class:`~alembic.operations.ops.MigrateOperation` objects, e.g. the
:class:`~alembic.operations.ops.UpgradeOps` /
:class:`~alembic.operations.ops.DowngradeOps` containers produced by
autogenerate.  A typical use is "the migration was generated against
``sales.orders`` but the table has been logically renamed to
``sales.purchases``; rewrite the plan before it is rendered or invoked".

The API has two halves:

* a **read-only plan**, :class:`RenameTablePlan`, which scans an existing
  operation tree *without modifying it* and reports every reference that
  would be retargeted, and

* a **replica apply** step, :meth:`RenameTablePlan.apply`, which returns a
  *new* operation tree (a replica) with the rename applied.  The original
  tree, its operation objects and their ``.info`` dictionaries are never
  mutated.

Identity is the ``(table_name, schema)`` pair: only operations whose table
name **and** schema both match are rewritten, so ``public.orders`` is left
alone when ``sales.orders`` is renamed.

Supported operations
--------------------

The rewriter understands exactly the following operation types; anything
else (including custom ``MigrateOperation`` subclasses, ``CreateTableOp``,
``AlterColumnOp``, ``ExecuteSQLOp``, unknown containers, ...) is considered
*unsupported*, because it may reference the renamed table in ways the
rewriter cannot see:

* :class:`~alembic.operations.ops.AddColumnOp` /
  :class:`~alembic.operations.ops.DropColumnOp`
* :class:`~alembic.operations.ops.CreateIndexOp` /
  :class:`~alembic.operations.ops.DropIndexOp`
* :class:`~alembic.operations.ops.CreateForeignKeyOp` (both the source
  table and the referent table are retargeted)
* :class:`~alembic.operations.ops.DropConstraintOp` (used to drop foreign
  key and other constraints)
* the containers :class:`~alembic.operations.ops.UpgradeOps`,
  :class:`~alembic.operations.ops.DowngradeOps`,
  :class:`~alembic.operations.ops.ModifyTableOps` and
  :class:`~alembic.operations.ops.MigrationScript`, which are traversed
  recursively.

If an unsupported operation is found, :class:`UnsupportedOperationError`
is raised while the plan is being built and the **whole batch is
rejected**: no half-renamed tree is ever produced and the original tree
is left exactly as it was.

Example
-------

Planning and applying a rename requires no database connection::

    from sqlalchemy import Column, Integer
    from alembic.operations.ops import (
        AddColumnOp, CreateIndexOp, UpgradeOps,
    )
    from alembic.operations.rewrite import RenameTablePlan, format_op_tree

    upgrade_ops = UpgradeOps(
        ops=[
            AddColumnOp("orders", Column("total", Integer), schema="sales"),
            CreateIndexOp("ix_total", "orders", ["total"], schema="sales"),
        ]
    )

    plan = RenameTablePlan(upgrade_ops, "orders", "purchases", schema="sales")
    for change in plan.changes:          # read-only view of what will move
        print(change.kind, change.path)

    new_ops = plan.apply()               # a replica; upgrade_ops is untouched
    print(format_op_tree(new_ops))

producing::

    UpgradeOps(token='upgrades')
      AddColumnOp(table=sales.purchases, column='total')
      CreateIndexOp(index='ix_total', table=sales.purchases, columns=['total'])

.. versionadded:: 1.17

"""

from __future__ import annotations

from typing import Any
from typing import cast
from typing import NamedTuple

from .ops import AddColumnOp
from .ops import AddConstraintOp
from .ops import CreateForeignKeyOp
from .ops import CreateIndexOp
from .ops import DowngradeOps
from .ops import DropColumnOp
from .ops import DropConstraintOp
from .ops import DropIndexOp
from .ops import MigrateOperation
from .ops import MigrationScript
from .ops import ModifyTableOps
from .ops import OpContainer
from .ops import UpgradeOps

__all__ = [
    "RenameTablePlan",
    "TableIdentity",
    "TableRename",
    "TableRenameError",
    "UnsupportedOperationError",
    "format_op_tree",
    "print_op_tree",
    "rename_table",
]


class TableRenameError(Exception):
    """Base class for errors raised while planning a table rename."""


class UnsupportedOperationError(TableRenameError):
    """Raised when an operation tree contains an operation that the
    table renamer does not support.

    Because an unsupported operation may still reference the table being
    renamed (for example a custom ``ExecuteSQLOp`` or a user-defined
    ``MigrateOperation``), the entire batch is rejected rather than
    producing a partially renamed tree.  The original operation tree is
    never modified.

    """

    def __init__(self, op: MigrateOperation, path: tuple[Any, ...]) -> None:
        self.op = op
        self.path = path
        super().__init__(
            "cannot plan a table rename: operation of type %r at path %r "
            "is not supported and may reference the table being renamed"
            % (type(op).__name__, path)
        )


class TableIdentity(NamedTuple):
    """The identity of a table: its name and schema together."""

    table_name: str
    schema: str | None = None

    def matches(self, table_name: str | None, schema: str | None) -> bool:
        """Return True if ``table_name``/``schema`` identify this table."""
        return self.table_name == table_name and self.schema == schema

    def __str__(self) -> str:
        if self.schema:
            return "%s.%s" % (self.schema, self.table_name)
        return self.table_name


class TableRename(NamedTuple):
    """Describes a single table reference that a plan will retarget."""

    path: tuple[Any, ...]
    """Position of the operation within the tree."""

    kind: str
    """Which reference moved: ``"table"``, ``"source_table"`` or
    ``"referent_table"``."""

    operation: MigrateOperation
    """The original (unmodified) operation object."""

    old_table_name: str
    new_table_name: str
    schema: str | None


class RenameTablePlan:
    """A read-only plan to rename every reference to one table within an
    operation tree.

    The plan scans the tree given at construction time.  If any operation
    in the tree is not supported by the rewriter,
    :class:`UnsupportedOperationError` is raised immediately and no plan
    is created; the tree is never modified by scanning.

    The plan itself is read-only: :attr:`changes` describes what *would*
    change, and :meth:`apply` produces a new tree, leaving the original
    one untouched.

    """

    def __init__(
        self,
        ops: MigrateOperation,
        table_name: str,
        new_table_name: str,
        *,
        schema: str | None = None,
    ) -> None:
        self._ops = ops
        self.identity = TableIdentity(table_name, schema)
        self.new_table_name = new_table_name
        changes: list[TableRename] = []
        # validation scan: raises UnsupportedOperationError if any
        # operation in the tree is unsupported.  Nothing is built or
        # mutated here.
        _rewrite_op(ops, self.identity, new_table_name, (), changes, False)
        self._changes = tuple(changes)

    @property
    def ops(self) -> MigrateOperation:
        """The original operation tree this plan was built from.

        This object must be treated as read-only; use :meth:`apply` to
        obtain a rewritten replica.
        """
        return self._ops

    @property
    def changes(self) -> tuple[TableRename, ...]:
        """A tuple of :class:`TableRename` records describing every table
        reference that :meth:`apply` will retarget."""
        return self._changes

    def __len__(self) -> int:
        return len(self._changes)

    def __bool__(self) -> bool:
        return bool(self._changes)

    def apply(self) -> MigrateOperation:
        """Return a new operation tree with the rename applied.

        The returned tree is a replica: every operation and container in
        it is a new object, list and dictionary-valued attributes
        (including each operation's ``.info`` dictionary) are copied, so
        the replica may be mutated freely without affecting the original
        tree.  Scalar column/constraint objects passed to the operations
        (e.g. the :class:`~sqlalchemy.schema.Column` of an
        :class:`~alembic.operations.ops.AddColumnOp`) are shared by
        reference and should be treated as read-only.

        """
        return _rewrite_op(
            self._ops, self.identity, self.new_table_name, (), None, True
        )


def rename_table(
    ops: MigrateOperation,
    table_name: str,
    new_table_name: str,
    *,
    schema: str | None = None,
) -> MigrateOperation:
    """Rename all references to one table within an operation tree.

    Convenience function equivalent to
    ``RenameTablePlan(ops, table_name, new_table_name,
    schema=schema).apply()``.

    :param ops: the root of the operation tree; not modified.
    :param table_name: the existing logical table name.
    :param new_table_name: the new logical table name.
    :param schema: the schema that, together with ``table_name``, forms
     the identity to match.  Operations on same-named tables in other
     schemas are left alone.
    :return: a new operation tree.
    :raises UnsupportedOperationError: if the tree contains any operation
     the rewriter does not support; the whole batch is rejected.

    """
    return RenameTablePlan(
        ops, table_name, new_table_name, schema=schema
    ).apply()


def _copy_info(src: MigrateOperation, dest: MigrateOperation) -> None:
    if src.info:
        dest.info.update(src.info)


def _record(
    changes: list[TableRename] | None,
    path: tuple[Any, ...],
    kind: str,
    op: MigrateOperation,
    identity: TableIdentity,
    new_table_name: str,
) -> None:
    if changes is not None:
        changes.append(
            TableRename(
                path,
                kind,
                op,
                identity.table_name,
                new_table_name,
                identity.schema,
            )
        )


def _rewrite_op(
    op: MigrateOperation,
    identity: TableIdentity,
    new_table_name: str,
    path: tuple[Any, ...],
    changes: list[TableRename] | None,
    build: bool,
) -> MigrateOperation:
    if isinstance(op, MigrationScript):
        return _rewrite_migration_script(
            op, identity, new_table_name, path, changes, build
        )
    elif isinstance(op, ModifyTableOps):
        return _rewrite_modify_table_ops(
            op, identity, new_table_name, path, changes, build
        )
    elif isinstance(op, UpgradeOps):
        children = _rewrite_children(
            op, identity, new_table_name, path, changes, build
        )
        if not build:
            return op
        new_upgrade = UpgradeOps(children, upgrade_token=op.upgrade_token)
        _copy_info(op, new_upgrade)
        return new_upgrade
    elif isinstance(op, DowngradeOps):
        children = _rewrite_children(
            op, identity, new_table_name, path, changes, build
        )
        if not build:
            return op
        new_downgrade = DowngradeOps(
            children, downgrade_token=op.downgrade_token
        )
        _copy_info(op, new_downgrade)
        return new_downgrade
    elif isinstance(op, OpContainer):
        # an OpContainer subclass we don't know; its semantics are
        # unknown so it may reference the renamed table.
        raise UnsupportedOperationError(op, path)
    elif isinstance(op, AddColumnOp):
        return _rewrite_add_column(
            op, identity, new_table_name, path, changes, build
        )
    elif isinstance(op, DropColumnOp):
        return _rewrite_drop_column(
            op, identity, new_table_name, path, changes, build
        )
    elif isinstance(op, CreateIndexOp):
        return _rewrite_create_index(
            op, identity, new_table_name, path, changes, build
        )
    elif isinstance(op, DropIndexOp):
        return _rewrite_drop_index(
            op, identity, new_table_name, path, changes, build
        )
    elif isinstance(op, CreateForeignKeyOp):
        return _rewrite_create_fk(
            op, identity, new_table_name, path, changes, build
        )
    elif isinstance(op, DropConstraintOp):
        return _rewrite_drop_constraint(
            op, identity, new_table_name, path, changes, build
        )
    else:
        raise UnsupportedOperationError(op, path)


def _rewrite_children(
    op: OpContainer,
    identity: TableIdentity,
    new_table_name: str,
    path: tuple[Any, ...],
    changes: list[TableRename] | None,
    build: bool,
) -> list[MigrateOperation]:
    return [
        _rewrite_op(
            child, identity, new_table_name, path + (index,), changes, build
        )
        for index, child in enumerate(op.ops)
    ]


def _rewrite_migration_script(
    op: MigrationScript,
    identity: TableIdentity,
    new_table_name: str,
    path: tuple[Any, ...],
    changes: list[TableRename] | None,
    build: bool,
) -> MigrateOperation:
    upgrades = [
        cast(
            "UpgradeOps",
            _rewrite_op(
                child,
                identity,
                new_table_name,
                path + ("upgrade_ops", index),
                changes,
                build,
            ),
        )
        for index, child in enumerate(op.upgrade_ops_list)
    ]
    downgrades = [
        cast(
            "DowngradeOps",
            _rewrite_op(
                child,
                identity,
                new_table_name,
                path + ("downgrade_ops", index),
                changes,
                build,
            ),
        )
        for index, child in enumerate(op.downgrade_ops_list)
    ]
    if not build:
        return op
    new_op = MigrationScript(
        op.rev_id,
        UpgradeOps(),
        DowngradeOps(),
        message=op.message,
        imports=set(op.imports),
        head=op.head,
        splice=op.splice,
        branch_label=op.branch_label,
        version_path=op.version_path,
        depends_on=op.depends_on,
    )
    # the __init__ signature only allows for a single UpgradeOps /
    # DowngradeOps; the property setters accept the list form, which
    # preserves multi-entry scripts.
    new_op.upgrade_ops = upgrades
    new_op.downgrade_ops = downgrades
    if hasattr(op, "_needs_render"):
        new_op._needs_render = op._needs_render
    _copy_info(op, new_op)
    return new_op


def _rewrite_modify_table_ops(
    op: ModifyTableOps,
    identity: TableIdentity,
    new_table_name: str,
    path: tuple[Any, ...],
    changes: list[TableRename] | None,
    build: bool,
) -> MigrateOperation:
    matched = identity.matches(op.table_name, op.schema)
    if matched:
        _record(changes, path, "table", op, identity, new_table_name)
    children = _rewrite_children(
        op, identity, new_table_name, path, changes, build
    )
    if not build:
        return op
    new_op = ModifyTableOps(
        new_table_name if matched else op.table_name,
        children,
        schema=op.schema,
    )
    _copy_info(op, new_op)
    return new_op


def _rewrite_add_column(
    op: AddColumnOp,
    identity: TableIdentity,
    new_table_name: str,
    path: tuple[Any, ...],
    changes: list[TableRename] | None,
    build: bool,
) -> MigrateOperation:
    matched = identity.matches(op.table_name, op.schema)
    if matched:
        _record(changes, path, "table", op, identity, new_table_name)
    if not build:
        return op
    new_op = AddColumnOp(
        new_table_name if matched else op.table_name,
        op.column,
        schema=op.schema,
        if_not_exists=op.if_not_exists,
        inline_references=op.inline_references,
        inline_primary_key=op.inline_primary_key,
        **op.kw,
    )
    _copy_info(op, new_op)
    return new_op


def _rewrite_drop_column(
    op: DropColumnOp,
    identity: TableIdentity,
    new_table_name: str,
    path: tuple[Any, ...],
    changes: list[TableRename] | None,
    build: bool,
) -> MigrateOperation:
    matched = identity.matches(op.table_name, op.schema)
    if matched:
        _record(changes, path, "table", op, identity, new_table_name)
    reverse = None
    if op._reverse is not None:
        reverse = cast(
            "AddColumnOp",
            _rewrite_op(
                op._reverse,
                identity,
                new_table_name,
                path + ("_reverse",),
                None,
                build,
            ),
        )
    if not build:
        return op
    new_op = DropColumnOp(
        new_table_name if matched else op.table_name,
        op.column_name,
        schema=op.schema,
        if_exists=op.if_exists,
        _reverse=reverse,
        **op.kw,
    )
    _copy_info(op, new_op)
    return new_op


def _rewrite_create_index(
    op: CreateIndexOp,
    identity: TableIdentity,
    new_table_name: str,
    path: tuple[Any, ...],
    changes: list[TableRename] | None,
    build: bool,
) -> MigrateOperation:
    matched = identity.matches(op.table_name, op.schema)
    if matched:
        _record(changes, path, "table", op, identity, new_table_name)
    if not build:
        return op
    new_op = CreateIndexOp(
        op.index_name,
        new_table_name if matched else op.table_name,
        list(op.columns),
        schema=op.schema,
        unique=op.unique,
        if_not_exists=op.if_not_exists,
        **dict(op.kw),
    )
    _copy_info(op, new_op)
    return new_op


def _rewrite_drop_index(
    op: DropIndexOp,
    identity: TableIdentity,
    new_table_name: str,
    path: tuple[Any, ...],
    changes: list[TableRename] | None,
    build: bool,
) -> MigrateOperation:
    matched = identity.matches(op.table_name, op.schema)
    if matched:
        _record(changes, path, "table", op, identity, new_table_name)
    reverse = None
    if op._reverse is not None:
        reverse = cast(
            "CreateIndexOp",
            _rewrite_op(
                op._reverse,
                identity,
                new_table_name,
                path + ("_reverse",),
                None,
                build,
            ),
        )
    if not build:
        return op
    new_op = DropIndexOp(
        op.index_name,
        table_name=new_table_name if matched else op.table_name,
        schema=op.schema,
        if_exists=op.if_exists,
        _reverse=reverse,
        **dict(op.kw),
    )
    _copy_info(op, new_op)
    return new_op


def _rewrite_create_fk(
    op: CreateForeignKeyOp,
    identity: TableIdentity,
    new_table_name: str,
    path: tuple[Any, ...],
    changes: list[TableRename] | None,
    build: bool,
) -> MigrateOperation:
    source_matched = identity.matches(
        op.source_table, op.kw.get("source_schema")
    )
    referent_matched = identity.matches(
        op.referent_table, op.kw.get("referent_schema")
    )
    if source_matched:
        _record(changes, path, "source_table", op, identity, new_table_name)
    if referent_matched:
        _record(changes, path, "referent_table", op, identity, new_table_name)
    if not build:
        return op
    new_op = CreateForeignKeyOp(
        op.constraint_name,
        new_table_name if source_matched else op.source_table,
        new_table_name if referent_matched else op.referent_table,
        list(op.local_cols),
        list(op.remote_cols),
        **dict(op.kw),
    )
    _copy_info(op, new_op)
    return new_op


def _rewrite_drop_constraint(
    op: DropConstraintOp,
    identity: TableIdentity,
    new_table_name: str,
    path: tuple[Any, ...],
    changes: list[TableRename] | None,
    build: bool,
) -> MigrateOperation:
    matched = identity.matches(op.table_name, op.schema)
    if matched:
        _record(changes, path, "table", op, identity, new_table_name)
    reverse: AddConstraintOp | None = op._reverse
    if isinstance(reverse, CreateForeignKeyOp):
        reverse = cast(
            "CreateForeignKeyOp",
            _rewrite_op(
                reverse,
                identity,
                new_table_name,
                path + ("_reverse",),
                None,
                build,
            ),
        )
    if not build:
        return op
    new_op = DropConstraintOp(
        op.constraint_name,
        new_table_name if matched else op.table_name,
        type_=op.constraint_type,
        schema=op.schema,
        if_exists=op.if_exists,
        _reverse=reverse,
    )
    _copy_info(op, new_op)
    return new_op


def _qualified_name(table_name: str | None, schema: str | None) -> str:
    if schema:
        return "%s.%s" % (schema, table_name)
    return str(table_name)


def _describe_op(op: MigrateOperation) -> str:
    if isinstance(op, MigrationScript):
        return "MigrationScript(rev_id=%r, message=%r)" % (
            op.rev_id,
            op.message,
        )
    elif isinstance(op, UpgradeOps):
        return "UpgradeOps(token=%r)" % (op.upgrade_token,)
    elif isinstance(op, DowngradeOps):
        return "DowngradeOps(token=%r)" % (op.downgrade_token,)
    elif isinstance(op, ModifyTableOps):
        return "ModifyTableOps(table=%s)" % _qualified_name(
            op.table_name, op.schema
        )
    elif isinstance(op, OpContainer):
        return "%s()" % type(op).__name__
    elif isinstance(op, AddColumnOp):
        return "AddColumnOp(table=%s, column=%r)" % (
            _qualified_name(op.table_name, op.schema),
            getattr(op.column, "name", op.column),
        )
    elif isinstance(op, DropColumnOp):
        return "DropColumnOp(table=%s, column=%r)" % (
            _qualified_name(op.table_name, op.schema),
            op.column_name,
        )
    elif isinstance(op, CreateIndexOp):
        columns = [c if isinstance(c, str) else str(c) for c in op.columns]
        return "CreateIndexOp(index=%r, table=%s, columns=%r)" % (
            op.index_name,
            _qualified_name(op.table_name, op.schema),
            columns,
        )
    elif isinstance(op, DropIndexOp):
        return "DropIndexOp(index=%r, table=%s)" % (
            op.index_name,
            _qualified_name(op.table_name, op.schema),
        )
    elif isinstance(op, CreateForeignKeyOp):
        return "CreateForeignKeyOp(constraint=%r, source=%s, referent=%s)" % (
            op.constraint_name,
            _qualified_name(op.source_table, op.kw.get("source_schema")),
            _qualified_name(op.referent_table, op.kw.get("referent_schema")),
        )
    elif isinstance(op, DropConstraintOp):
        return "DropConstraintOp(constraint=%r, table=%s, type=%r)" % (
            op.constraint_name,
            _qualified_name(op.table_name, op.schema),
            op.constraint_type,
        )
    else:
        return "%s(...)" % type(op).__name__


def _child_ops(op: MigrateOperation) -> list[MigrateOperation]:
    if isinstance(op, MigrationScript):
        return list(op.upgrade_ops_list) + list(op.downgrade_ops_list)
    elif isinstance(op, OpContainer):
        return list(op.ops)
    else:
        return []


def format_op_tree(ops: MigrateOperation) -> str:
    """Render an operation tree as an indented, human-readable listing.

    This is a database-free inspection helper: it only reads the
    operation objects and never requires a connection or a migration
    context.

    """
    lines: list[str] = []

    def _recurse(op: MigrateOperation, depth: int) -> None:
        lines.append("%s%s" % ("  " * depth, _describe_op(op)))
        for child in _child_ops(op):
            _recurse(child, depth + 1)

    _recurse(ops, 0)
    return "\n".join(lines)


def print_op_tree(ops: MigrateOperation, file: Any = None) -> None:
    """Print :func:`format_op_tree` output to ``file`` (default stdout)."""
    print(format_op_tree(ops), file=file)
