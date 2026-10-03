"""Logical table renaming across a migration operation tree.

This module implements a post-generation transformation that uniformly
renames a table's logical identity -- the tuple ``(schema, table_name)`` --
across an operation tree produced by autogenerate, without touching the
database and without mutating the original tree.

Two interfaces are provided:

* :func:`plan_table_rename` traverses the tree and returns a read-only
  :class:`TableRenamePlan` describing every operation that would be
  modified.  The tree itself is not changed.
* :func:`apply_table_rename` returns a deep copy of the tree with the
  rename applied.  The original tree is left untouched.

Only a known set of operation types are supported (create/drop table,
add/drop column, create/drop index, create foreign key, and the
:class:`.ModifyTableOps` container).  Any other operation that may
reference the table -- in particular user-defined custom operations --
causes :class:`UnsupportedTableRenameError` to be raised before any
modification is made, so a partially modified tree is never produced.

"""
from __future__ import annotations

from collections.abc import Iterator
import copy
from dataclasses import dataclass
from typing import cast
from typing import Union

from .ops import AddColumnOp
from .ops import CreateForeignKeyOp
from .ops import CreateIndexOp
from .ops import CreateTableOp
from .ops import DropColumnOp
from .ops import DropIndexOp
from .ops import DropTableOp
from .ops import MigrateOperation
from .ops import ModifyTableOps
from .ops import OpContainer

try:
    from sqlalchemy.schema import Table
except ImportError:  # pragma: no cover
    Table = None  # type: ignore[assignment,misc]

__all__ = [
    "TableRenamePlan",
    "UnsupportedTableRenameError",
    "apply_table_rename",
    "op_tree_to_str",
    "plan_table_rename",
]


def _identity_repr(schema: str | None, table_name: str | None) -> str:
    if table_name is None:
        return f"{schema}.<none>" if schema else "<none>"
    if schema:
        return f"{schema}.{table_name}"
    return table_name


class UnsupportedTableRenameError(Exception):
    """Raised when an operation cannot be safely renamed.

    This error is raised during the planning phase when an operation is
    encountered that is not one of the supported operation types and may
    reference the table being renamed.  Because the rename cannot be
    applied to such an operation, the entire plan is rejected before any
    modification is made; the original operation tree is left unchanged.

    """

    def __init__(
        self,
        operation: MigrateOperation,
        table_name: str,
        schema: str | None = None,
        *,
        reason: str | None = None,
    ) -> None:
        self.operation = operation
        self.table_name = table_name
        self.schema = schema
        self.reason = reason
        ident = _identity_repr(schema, table_name)
        message = (
            f"cannot rename table {ident}: unsupported operation "
            f"{type(operation).__name__!r} may reference the table"
        )
        if reason:
            message = f"{message} ({reason})"
        super().__init__(message)


@dataclass(frozen=True)
class TableRenamePlan:
    """A read-only plan for renaming a table across an operation tree.

    The plan is produced by :func:`plan_table_rename`.  It describes the
    old and new table identities and the operations that would be
    modified.  The plan does not mutate the operation tree.

    """

    old_table_name: str
    new_table_name: str
    schema: str | None
    operations: tuple[MigrateOperation, ...]

    @property
    def old_identity(self) -> str:
        """The old ``schema.table_name`` identity."""
        return _identity_repr(self.schema, self.old_table_name)

    @property
    def new_identity(self) -> str:
        """The new ``schema.table_name`` identity."""
        return _identity_repr(self.schema, self.new_table_name)

    def __len__(self) -> int:
        return len(self.operations)

    def __iter__(self) -> Iterator[MigrateOperation]:
        return iter(self.operations)

    def __contains__(self, operation: object) -> bool:
        return operation in self.operations

    def __repr__(self) -> str:
        return (
            f"TableRenamePlan({self.old_identity!r} -> "
            f"{self.new_identity!r}, operations={len(self.operations)})"
        )


# operation types that carry a ``table_name`` / ``schema`` identity and are
# supported for logical renaming.
_TABLE_NAME_OP_TYPES: tuple[type[MigrateOperation], ...] = (
    CreateTableOp,
    DropTableOp,
    AddColumnOp,
    DropColumnOp,
    CreateIndexOp,
    DropIndexOp,
)

# union of the above types, used for static type narrowing.
_TableNameOpUnion = Union[
    CreateTableOp,
    DropTableOp,
    AddColumnOp,
    DropColumnOp,
    CreateIndexOp,
    DropIndexOp,
]

# attribute pairs that reference a table identity, used when inspecting
# unsupported built-in operations.
_TABLE_REF_ATTR_PAIRS: tuple[tuple[str, str], ...] = (
    ("table_name", "schema"),
    ("source_table", "source_schema"),
    ("referent_table", "referent_schema"),
)


def _matches_identity(
    table_name: str,
    schema: str | None,
    candidate_table: str | None,
    candidate_schema: str | None,
) -> bool:
    return candidate_table == table_name and candidate_schema == schema


def _fk_schemas(op: CreateForeignKeyOp) -> tuple[str | None, str | None]:
    """Return the ``(source_schema, referent_schema)`` for a foreign key op.

    These are stored in ``self.kw`` rather than as direct attributes.
    """

    kw = op.kw
    return (kw.get("source_schema"), kw.get("referent_schema"))


def _op_references_table(
    op: MigrateOperation,
    table_name: str,
    schema: str | None,
) -> bool:
    """Return True if *op* carries a reference to the table identity."""

    for name_attr, schema_attr in _TABLE_REF_ATTR_PAIRS:
        if _matches_identity(
            table_name,
            schema,
            getattr(op, name_attr, None),
            getattr(op, schema_attr, None),
        ):
            return True

    # Some operations (e.g. BulkInsertOp) hold a full Table object.
    table = getattr(op, "table", None)
    if Table is not None and isinstance(table, Table):
        if _matches_identity(table_name, schema, table.name, table.schema):
            return True

    return False


def _check_unsupported(
    op: MigrateOperation,
    table_name: str,
    schema: str | None,
) -> None:
    """Raise if *op* is unsupported and may reference the table."""

    op_module = type(op).__module__
    if op_module != "alembic.operations.ops":
        # A user-defined operation may hold a reference to the table in
        # any way; reject the whole batch rather than risk a partial rename.
        raise UnsupportedTableRenameError(
            op, table_name, schema, reason="custom operation"
        )

    if _op_references_table(op, table_name, schema):
        raise UnsupportedTableRenameError(
            op,
            table_name,
            schema,
            reason=(
                "built-in operation references the table but is not "
                "supported for rename"
            ),
        )


def _iter_rename_actions(
    root: MigrateOperation,
    old_table_name: str,
    new_table_name: str,
    schema: str | None,
) -> Iterator[tuple[MigrateOperation, str, str]]:
    """Walk *root* and yield ``(op, attribute, new_value)`` actions.

    Raises :class:`UnsupportedTableRenameError` before yielding any action
    if an unsupported operation that may reference the table is found.

    """

    # Import locally to avoid a circular import at module load time.
    from .ops import MigrationScript

    if isinstance(root, MigrationScript):
        for upgrade_ops in root.upgrade_ops_list:
            yield from _iter_rename_actions(
                upgrade_ops, old_table_name, new_table_name, schema
            )
        for downgrade_ops in root.downgrade_ops_list:
            yield from _iter_rename_actions(
                downgrade_ops, old_table_name, new_table_name, schema
            )
        return

    if isinstance(root, OpContainer):
        if isinstance(root, ModifyTableOps):
            if _matches_identity(
                old_table_name, schema, root.table_name, root.schema
            ):
                yield (root, "table_name", new_table_name)
        for child in root.ops:
            yield from _iter_rename_actions(
                child, old_table_name, new_table_name, schema
            )
        return

    if isinstance(root, _TABLE_NAME_OP_TYPES):
        table_op = cast(_TableNameOpUnion, root)
        if _matches_identity(
            old_table_name, schema, table_op.table_name, table_op.schema
        ):
            yield (table_op, "table_name", new_table_name)
        return

    if isinstance(root, CreateForeignKeyOp):
        source_schema, referent_schema = _fk_schemas(root)
        if _matches_identity(
            old_table_name,
            schema,
            root.source_table,
            source_schema,
        ):
            yield (root, "source_table", new_table_name)
        if _matches_identity(
            old_table_name,
            schema,
            root.referent_table,
            referent_schema,
        ):
            yield (root, "referent_table", new_table_name)
        return

    _check_unsupported(root, old_table_name, schema)


def plan_table_rename(
    root: MigrateOperation,
    old_table_name: str,
    new_table_name: str,
    *,
    schema: str | None = None,
) -> TableRenamePlan:
    """Build a read-only plan for renaming a table across an operation tree.

    The tree is traversed and every operation that references the table
    identity ``(schema, old_table_name)`` is recorded.  The tree itself is
    not modified.

    :param root: the root :class:`.MigrateOperation` of the tree (e.g. a
        :class:`.MigrationScript`, :class:`.UpgradeOps`, or any
        :class:`.OpContainer`).
    :param old_table_name: the current logical name of the table.
    :param new_table_name: the new logical name.
    :param schema: optional schema name; together with *old_table_name* it
        forms the identity that is matched.

    :return: a read-only :class:`TableRenamePlan`.

    :raises UnsupportedTableRenameError: if an operation is encountered
        that is not supported for rename and may reference the table.  In
        this case the tree is left unchanged.

    """

    actions = list(
        _iter_rename_actions(root, old_table_name, new_table_name, schema)
    )
    operations = tuple(op for op, _, _ in actions)
    return TableRenamePlan(
        old_table_name, new_table_name, schema, operations
    )


def apply_table_rename(
    root: MigrateOperation,
    old_table_name: str,
    new_table_name: str,
    *,
    schema: str | None = None,
) -> MigrateOperation:
    """Return a deep copy of the tree with the table renamed.

    The original operation tree is left untouched.  The rename is first
    planned against the original tree so that an unsupported operation
    causes :class:`UnsupportedTableRenameError` to be raised before any
    copy is made.

    :param root: the root :class:`.MigrateOperation` of the tree.
    :param old_table_name: the current logical name of the table.
    :param new_table_name: the new logical name.
    :param schema: optional schema name; together with *old_table_name* it
        forms the identity that is matched.

    :return: a deep copy of *root* with the rename applied.

    :raises UnsupportedTableRenameError: if an operation is encountered
        that is not supported for rename and may reference the table.  In
        this case the original tree is left unchanged.

    """

    # Validate against the original tree first; this raises before any
    # copy is made if the rename cannot be applied safely.
    plan_table_rename(root, old_table_name, new_table_name, schema=schema)

    new_root = copy.deepcopy(root)

    for op, attr, value in _iter_rename_actions(
        new_root, old_table_name, new_table_name, schema
    ):
        setattr(op, attr, value)

    return new_root


def op_tree_to_str(root: MigrateOperation) -> str:
    """Return a human-readable string representation of an operation tree.

    This is a database-free debugging aid that walks the tree and prints
    each operation together with its table identity.

    """

    lines: list[str] = []
    _format_op(root, 0, lines)
    return "\n".join(lines)


def _format_op(op: MigrateOperation, indent: int, lines: list[str]) -> None:
    prefix = "  " * indent

    from .ops import MigrationScript

    if isinstance(op, MigrationScript):
        lines.append(f"{prefix}MigrationScript(rev_id={op.rev_id!r})")
        for upgrade_ops in op.upgrade_ops_list:
            _format_op(upgrade_ops, indent + 1, lines)
        for downgrade_ops in op.downgrade_ops_list:
            _format_op(downgrade_ops, indent + 1, lines)
        return

    if isinstance(op, OpContainer):
        name = type(op).__name__
        if isinstance(op, ModifyTableOps):
            lines.append(
                f"{prefix}{name}({_identity_repr(op.schema, op.table_name)})"
            )
        else:
            lines.append(f"{prefix}{name}()")
        for child in op.ops:
            _format_op(child, indent + 1, lines)
        return

    lines.append(f"{prefix}{_format_leaf(op)}")


def _format_leaf(op: MigrateOperation) -> str:
    name = type(op).__name__

    if isinstance(op, CreateForeignKeyOp):
        source_schema, referent_schema = _fk_schemas(op)
        src = _identity_repr(source_schema, op.source_table)
        ref = _identity_repr(referent_schema, op.referent_table)
        return f"{name}({src} -> {ref})"

    if isinstance(op, _TABLE_NAME_OP_TYPES) or isinstance(op, ModifyTableOps):
        table_op = cast(Union[_TableNameOpUnion, ModifyTableOps], op)
        ident = _identity_repr(table_op.schema, table_op.table_name)
        return f"{name}({ident})"

    return f"{name}()"
