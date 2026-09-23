# mypy: allow-untyped-calls

from __future__ import annotations

from collections.abc import Iterator
import contextlib
import logging
from typing import TYPE_CHECKING

from sqlalchemy import event
from sqlalchemy import schema as sa_schema
from sqlalchemy.util import OrderedSet

from .util import _InspectorConv
from ... import util
from ...operations import ops
from ...util import PriorityDispatchResult

if TYPE_CHECKING:
    from sqlalchemy.engine.reflection import Inspector
    from sqlalchemy.sql.elements import quoted_name
    from sqlalchemy.sql.schema import Table

    from ...autogenerate.api import AutogenContext
    from ...operations.ops import ModifyTableOps
    from ...operations.ops import UpgradeOps
    from ...runtime.plugins import Plugin


log = logging.getLogger(__name__)


def _autogen_for_tables(
    autogen_context: AutogenContext,
    upgrade_ops: UpgradeOps,
    schemas: set[str | None],
) -> PriorityDispatchResult:
    inspector = autogen_context.inspector

    conn_table_names: set[tuple[str | None, str]] = set()

    version_table_schema = (
        autogen_context.migration_context.version_table_schema
    )
    version_table = autogen_context.migration_context.version_table

    for schema_name in schemas:
        tables = available = set(inspector.get_table_names(schema=schema_name))
        if schema_name == version_table_schema:
            tables = tables.difference(
                [autogen_context.migration_context.version_table]
            )

        tablenames = [
            tname
            for tname in tables
            if autogen_context.run_name_filters(
                tname, "table", {"schema_name": schema_name}
            )
        ]

        conn_table_names.update((schema_name, tname) for tname in tablenames)

        inspector = autogen_context.inspector
        insp = _InspectorConv(inspector)
        insp.pre_cache_tables(schema_name, tablenames, available)

    metadata_table_names = OrderedSet(
        [(table.schema, table.name) for table in autogen_context.sorted_tables]
    ).difference([(version_table_schema, version_table)])

    _compare_tables(
        conn_table_names,
        metadata_table_names,
        inspector,
        upgrade_ops,
        autogen_context,
    )

    return PriorityDispatchResult.CONTINUE


def _compare_tables(
    conn_table_names: set[tuple[str | None, str]],
    metadata_table_names: set[tuple[str | None, str]],
    inspector: Inspector,
    upgrade_ops: UpgradeOps,
    autogen_context: AutogenContext,
) -> None:
    default_schema = inspector.bind.dialect.default_schema_name

    # tables coming from the connection will not have "schema"
    # set if it matches default_schema_name; so we need a list
    # of table names from local metadata that also have "None" if schema
    # == default_schema_name.  Most setups will be like this anyway but
    # some are not (see #170)
    metadata_table_names_no_dflt_schema = OrderedSet(
        [
            (schema if schema != default_schema else None, tname)
            for schema, tname in metadata_table_names
        ]
    )

    # to adjust for the MetaData collection storing the tables either
    # as "schemaname.tablename" or just "tablename", create a new lookup
    # which will match the "non-default-schema" keys to the Table object.
    tname_to_table = {
        no_dflt_schema: autogen_context.table_key_to_table[
            sa_schema._get_table_key(tname, schema)
        ]
        for no_dflt_schema, (schema, tname) in zip(
            metadata_table_names_no_dflt_schema, metadata_table_names
        )
    }
    metadata_table_names = metadata_table_names_no_dflt_schema

    for s, tname in metadata_table_names.difference(conn_table_names):
        name = "%s.%s" % (s, tname) if s else tname
        metadata_table = tname_to_table[(s, tname)]
        if autogen_context.run_object_filters(
            metadata_table, tname, "table", False, None
        ):
            upgrade_ops.ops.append(
                ops.CreateTableOp.from_table(metadata_table)
            )
            log.info("Detected added table %r", name)
            modify_table_ops = ops.ModifyTableOps(tname, [], schema=s)

            autogen_context.comparators.dispatch(
                "table", qualifier=autogen_context.dialect.name
            )(
                autogen_context,
                modify_table_ops,
                s,
                tname,
                None,
                metadata_table,
            )
            if not modify_table_ops.is_empty():
                upgrade_ops.ops.append(modify_table_ops)

    removal_metadata = sa_schema.MetaData()
    for s, tname in conn_table_names.difference(metadata_table_names):
        name = sa_schema._get_table_key(tname, s)

        # a name might be present already if a previous reflection pulled
        # this table in via foreign key constraint
        exists = name in removal_metadata.tables
        t = sa_schema.Table(tname, removal_metadata, schema=s)

        if not exists:
            event.listen(
                t,
                "column_reflect",
                # fmt: off
                autogen_context.migration_context.impl.
                _compat_autogen_column_reflect
                (inspector),
                # fmt: on
            )
            _InspectorConv(inspector).reflect_table(t)
        if autogen_context.run_object_filters(t, tname, "table", True, None):
            modify_table_ops = ops.ModifyTableOps(tname, [], schema=s)

            autogen_context.comparators.dispatch(
                "table", qualifier=autogen_context.dialect.name
            )(autogen_context, modify_table_ops, s, tname, t, None)
            if not modify_table_ops.is_empty():
                upgrade_ops.ops.append(modify_table_ops)

            upgrade_ops.ops.append(ops.DropTableOp.from_table(t))
            log.info("Detected removed table %r", name)

    existing_tables = conn_table_names.intersection(metadata_table_names)

    existing_metadata = sa_schema.MetaData()
    conn_column_info = {}
    for s, tname in existing_tables:
        name = sa_schema._get_table_key(tname, s)
        exists = name in existing_metadata.tables

        # a name might be present already if a previous reflection pulled
        # this table in via foreign key constraint
        t = sa_schema.Table(tname, existing_metadata, schema=s)
        if not exists:
            event.listen(
                t,
                "column_reflect",
                # fmt: off
                autogen_context.migration_context.impl.
                _compat_autogen_column_reflect(inspector),
                # fmt: on
            )
            _InspectorConv(inspector).reflect_table(t)

        conn_column_info[(s, tname)] = t

    autogen_context._column_renames = _validate_column_renames(
        autogen_context,
        existing_tables,
        conn_column_info,
        tname_to_table,
        default_schema,
    )
    autogen_context._renamed_conn_tables = {
        key: _renamed_reflected_table(conn_column_info[key], renames)
        for key, renames in autogen_context._column_renames.items()
    }

    for s, tname in sorted(existing_tables, key=lambda x: (x[0] or "", x[1])):
        s = s or None
        name = "%s.%s" % (s, tname) if s else tname
        metadata_table = tname_to_table[(s, tname)]
        conn_table = existing_metadata.tables[name]

        if autogen_context.run_object_filters(
            metadata_table, tname, "table", False, conn_table
        ):
            modify_table_ops = ops.ModifyTableOps(tname, [], schema=s)
            with _compare_columns(
                s,
                tname,
                conn_table,
                metadata_table,
                modify_table_ops,
                autogen_context,
                inspector,
                column_renames=autogen_context._column_renames.get(
                    (s, tname), {}
                ),
            ) as compare_conn_table:
                autogen_context.comparators.dispatch(
                    "table", qualifier=autogen_context.dialect.name
                )(
                    autogen_context,
                    modify_table_ops,
                    s,
                    tname,
                    compare_conn_table,
                    metadata_table,
                )

            if not modify_table_ops.is_empty():
                upgrade_ops.ops.append(modify_table_ops)


@contextlib.contextmanager
def _compare_columns(
    schema: str | None,
    tname: quoted_name | str,
    conn_table: Table,
    metadata_table: Table,
    modify_table_ops: ModifyTableOps,
    autogen_context: AutogenContext,
    inspector: Inspector,
    column_renames: dict[str, str] | None = None,
) -> Iterator[Table]:
    name = "%s.%s" % (schema, tname) if schema else tname
    metadata_col_names = OrderedSet(
        c.name for c in metadata_table.c if not c.system
    )
    metadata_cols_by_name = {
        c.name: c for c in metadata_table.c if not c.system
    }

    conn_col_names = {
        c.name: c
        for c in conn_table.c
        if autogen_context.run_name_filters(
            c.name, "column", {"table_name": tname, "schema_name": schema}
        )
    }

    # apply the object filters to the declared renames up front, so that
    # a filtered-out rename falls back to the regular add/drop comparison
    renames: dict[str, str] = {}
    if column_renames:
        for old_name, new_name in column_renames.items():
            metadata_col = metadata_cols_by_name[new_name]
            conn_col = conn_table.c[old_name]
            if autogen_context.run_object_filters(
                metadata_col, new_name, "column", False, conn_col
            ):
                renames[old_name] = new_name

    renamed_new_names = set(renames.values())

    for cname in metadata_col_names.difference(conn_col_names):
        if cname in renamed_new_names:
            continue
        if autogen_context.run_object_filters(
            metadata_cols_by_name[cname], cname, "column", False, None
        ):
            modify_table_ops.ops.append(
                ops.AddColumnOp.from_column_and_tablename(
                    schema, tname, metadata_cols_by_name[cname]
                )
            )
            log.info("Detected added column '%s.%s'", name, cname)

    for colname in metadata_col_names.intersection(conn_col_names):
        metadata_col = metadata_cols_by_name[colname]
        conn_col = conn_table.c[colname]
        if not autogen_context.run_object_filters(
            metadata_col, colname, "column", False, conn_col
        ):
            continue
        alter_column_op = ops.AlterColumnOp(tname, colname, schema=schema)

        autogen_context.comparators.dispatch(
            "column", qualifier=autogen_context.dialect.name
        )(
            autogen_context,
            alter_column_op,
            schema,
            tname,
            colname,
            conn_col,
            metadata_col,
        )

        if alter_column_op.has_changes():
            modify_table_ops.ops.append(alter_column_op)

    for old_name, new_name in renames.items():
        metadata_col = metadata_cols_by_name[new_name]
        conn_col = conn_table.c[old_name]
        alter_column_op = ops.AlterColumnOp(
            tname, old_name, schema=schema, modify_name=new_name
        )

        autogen_context.comparators.dispatch(
            "column", qualifier=autogen_context.dialect.name
        )(
            autogen_context,
            alter_column_op,
            schema,
            tname,
            new_name,
            conn_col,
            metadata_col,
        )

        modify_table_ops.ops.append(alter_column_op)
        log.info(
            "Detected renamed column '%s.%s' to '%s.%s'",
            name,
            old_name,
            name,
            new_name,
        )

    # constraint, index and foreign key comparison runs against a copy
    # of the reflected table where renamed columns carry their new
    # (metadata-side) names, so that columns are matched by identity
    if renames:
        yield _renamed_reflected_table(conn_table, renames)
    else:
        yield conn_table

    for cname in set(conn_col_names).difference(metadata_col_names):
        if cname in renames:
            continue
        if autogen_context.run_object_filters(
            conn_table.c[cname], cname, "column", True, None
        ):
            modify_table_ops.ops.append(
                ops.DropColumnOp.from_column_and_tablename(
                    schema, tname, conn_table.c[cname]
                )
            )
            log.info("Detected removed column '%s.%s'", name, cname)


def _renamed_reflected_table(
    conn_table: Table, renames: dict[str, str]
) -> Table:
    """Return a lightweight copy of a reflected table with the renamed
    columns carrying their new (metadata-side) names.

    This is used so that constraint, index and foreign key comparison
    matches columns by their renamed identity.  Only the elements the
    table-level comparators rely upon are copied; constraints and
    indexes are not copied since the comparators rebuild them from
    reflected inspector data.

    """
    new_table = sa_schema.Table(
        conn_table.name,
        sa_schema.MetaData(),
        schema=conn_table.schema,
        comment=conn_table.comment,
    )
    for col in conn_table.c:
        new_table.append_column(
            sa_schema.Column(
                renames.get(col.name, col.name),
                col.type,
                nullable=col.nullable,
                default=col.default,
                server_default=col.server_default,
                comment=col.comment,
            )
        )
    return new_table


def _validate_column_renames(
    autogen_context: AutogenContext,
    existing_tables: set[tuple[str | None, str]],
    conn_column_info: dict[tuple[str | None, str], Table],
    tname_to_table: dict[tuple[str | None, str], Table],
    default_schema: str | None,
) -> dict[tuple[str | None, str], dict[str, str]]:
    """Validate the ``column_rename_map`` option in full.

    Every entry is checked before any operations are generated; if any
    entry is invalid a :class:`~alembic.util.CommandError` is raised
    listing all of the problems, so that no partial migration is
    produced.  Returns the validated renames grouped by
    ``(schema, table_name)``.

    """
    rename_map = autogen_context.column_rename_map
    per_table: dict[tuple[str | None, str], dict[str, str]] = {}
    if not rename_map:
        return per_table

    errors: list[str] = []

    for (schema, tname, old_name), new_name in rename_map.items():
        if schema == default_schema:
            schema = None
        per_table.setdefault((schema, tname), {})[old_name] = new_name

    def _tlabel(schema: str | None, tname: str) -> str:
        return "%s.%s" % (schema, tname) if schema else tname

    # structural checks: strictly one-to-one, no chains, no cycles
    for (schema, tname), renames in per_table.items():
        tlabel = _tlabel(schema, tname)
        new_names = list(renames.values())
        for old_name, new_name in renames.items():
            if old_name == new_name:
                errors.append(
                    "table %r: column rename %r -> %r is not a rename; "
                    "source and target names are identical"
                    % (tlabel, old_name, new_name)
                )
            elif new_name in renames:
                errors.append(
                    "table %r: column rename %r -> %r is part of a chain "
                    "or cycle; %r is itself renamed to %r"
                    % (
                        tlabel,
                        old_name,
                        new_name,
                        new_name,
                        renames[new_name],
                    )
                )
        for new_name in set(new_names):
            if new_names.count(new_name) > 1:
                old_names = sorted(
                    old for old, new in renames.items() if new == new_name
                )
                errors.append(
                    "table %r: columns %s all rename to the same target "
                    "%r; renames must be one-to-one"
                    % (
                        tlabel,
                        ", ".join(repr(old) for old in old_names),
                        new_name,
                    )
                )

    # existence checks against the reflected database and the metadata
    for (schema, tname), renames in per_table.items():
        tlabel = _tlabel(schema, tname)
        if (schema, tname) not in existing_tables:
            errors.append(
                "table %r: column renames require the table to exist "
                "both in the database and in the target MetaData" % (tlabel,)
            )
            continue
        conn_table = conn_column_info[(schema, tname)]
        metadata_table = tname_to_table[(schema, tname)]
        for old_name, new_name in renames.items():
            if old_name not in conn_table.c:
                errors.append(
                    "table %r: column %r does not exist in the database"
                    % (tlabel, old_name)
                )
            if new_name in conn_table.c:
                errors.append(
                    "table %r: column %r already exists in the database"
                    % (tlabel, new_name)
                )
            if new_name not in metadata_table.c:
                errors.append(
                    "table %r: column %r does not exist in the target "
                    "MetaData" % (tlabel, new_name)
                )
            if old_name in metadata_table.c:
                errors.append(
                    "table %r: column %r is still present in the target "
                    "MetaData; a rename requires the old column to be "
                    "removed from the MetaData" % (tlabel, old_name)
                )

    if errors:
        raise util.CommandError(
            "Invalid column_rename_map; no operations were generated. "
            "Problems:\n  * " + "\n  * ".join(errors)
        )
    return per_table


def setup(plugin: Plugin) -> None:

    plugin.add_autogenerate_comparator(
        _autogen_for_tables,
        "schema",
        "tables",
    )
