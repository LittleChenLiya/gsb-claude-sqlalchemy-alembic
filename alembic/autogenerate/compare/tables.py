# mypy: allow-untyped-calls

from __future__ import annotations

from collections.abc import Iterator
from collections.abc import Mapping
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

    if autogen_context.column_renames:
        _validate_column_renames(
            autogen_context, conn_table_names, tname_to_table, inspector
        )

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

    renamed_views = None
    if autogen_context._validated_column_renames:
        renamed_views = _renamed_metadata(existing_metadata, autogen_context)

    for s, tname in sorted(existing_tables, key=lambda x: (x[0] or "", x[1])):
        s = s or None
        name = "%s.%s" % (s, tname) if s else tname
        metadata_table = tname_to_table[(s, tname)]
        conn_table = existing_metadata.tables[name]
        if renamed_views is not None:
            compare_conn_table = renamed_views.tables[name]
        else:
            compare_conn_table = conn_table

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
            ):
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


def _validate_column_renames(
    autogen_context: AutogenContext,
    conn_table_names: set[tuple[str | None, str]],
    tname_to_table: dict[tuple[str | None, str], Table],
    inspector: Inspector,
) -> None:
    """Validate the ``column_renames`` option in its entirety.

    All mappings are checked before any operations are generated; if any
    single mapping is invalid, a :class:`~alembic.util.CommandError` is
    raised naming every offending table and column, and no migration
    operations are produced.

    On success, the normalized mapping is stored on the autogen context
    as ``{(schema, table_name): {old_column_name: new_column_name}}``.
    """
    raw = autogen_context.column_renames
    default_schema = inspector.bind.dialect.default_schema_name

    errors: list[str] = []
    normalized: dict[tuple[str | None, str], dict[str, str]] = {}

    if not isinstance(raw, Mapping):
        raise util.CommandError(
            "column_renames must be a mapping of "
            "(schema, table_name, old_column_name) -> new_column_name"
        )

    for key, new_name in raw.items():
        if isinstance(key, tuple) and len(key) == 2:
            schema, tname, old_name = None, key[0], key[1]
        elif isinstance(key, tuple) and len(key) == 3:
            schema, tname, old_name = key
        else:
            errors.append(
                "column_renames key %r is not a "
                "(table_name, old_column_name) or "
                "(schema, table_name, old_column_name) tuple" % (key,)
            )
            continue
        if schema == default_schema:
            schema = None
        if not isinstance(tname, str) or not isinstance(old_name, str):
            errors.append(
                "column_renames key %r does not name a table and column"
                % (key,)
            )
            continue
        if not isinstance(new_name, str):
            errors.append(
                "column_renames target %r for column %r on table %r "
                "is not a string" % (new_name, old_name, tname)
            )
            continue
        table_key = (schema, tname)
        per_table = normalized.setdefault(table_key, {})
        if old_name in per_table:
            errors.append(
                "column %r on table %r is renamed more than once"
                % (old_name, tname)
            )
        else:
            per_table[old_name] = new_name

    for (schema, tname), per_table in normalized.items():
        for old_name, new_name in per_table.items():
            if new_name in per_table:
                errors.append(
                    "chained or circular column rename on table %r: "
                    "column %r is both a rename source and a rename "
                    "target" % (tname, new_name)
                )
        new_names = list(per_table.values())
        for dup in {n for n in new_names if new_names.count(n) > 1}:
            errors.append(
                "multiple columns on table %r are renamed to the same "
                "target column %r" % (tname, dup)
            )

    conn_col_cache: dict[tuple[str | None, str], set[str]] = {}

    for (schema, tname), per_table in normalized.items():
        metadata_table = tname_to_table.get((schema, tname))
        if metadata_table is None:
            for old_name, new_name in per_table.items():
                errors.append(
                    "cannot rename column %r to %r: table %r is not "
                    "present in the target MetaData"
                    % (old_name, new_name, tname)
                )
            continue
        if (schema, tname) not in conn_table_names:
            for old_name, new_name in per_table.items():
                errors.append(
                    "cannot rename column %r to %r: table %r does not "
                    "exist in the database" % (old_name, new_name, tname)
                )
            continue

        if (schema, tname) not in conn_col_cache:
            conn_col_cache[(schema, tname)] = {
                col["name"]
                for col in inspector.get_columns(tname, schema=schema)
            }
        conn_cols = conn_col_cache[(schema, tname)]
        metadata_cols = {c.name for c in metadata_table.c if not c.system}

        for old_name, new_name in per_table.items():
            if old_name not in conn_cols:
                errors.append(
                    "cannot rename column %r to %r on table %r: "
                    "column %r does not exist in the database"
                    % (old_name, new_name, tname, old_name)
                )
            if new_name not in metadata_cols:
                errors.append(
                    "cannot rename column %r to %r on table %r: "
                    "column %r is not present in the target MetaData"
                    % (old_name, new_name, tname, new_name)
                )
            if old_name in metadata_cols:
                errors.append(
                    "cannot rename column %r to %r on table %r: "
                    "column %r is still present in the target MetaData"
                    % (old_name, new_name, tname, old_name)
                )
            if new_name in conn_cols:
                errors.append(
                    "cannot rename column %r to %r on table %r: "
                    "column %r already exists in the database"
                    % (old_name, new_name, tname, new_name)
                )

    if errors:
        raise util.CommandError(
            "Invalid column_renames %s:\n%s"
            % (
                "mapping" if len(errors) == 1 else "mappings",
                "\n".join("  * %s" % err for err in errors),
            )
        )

    autogen_context._validated_column_renames = normalized


def _renamed_metadata(
    existing_metadata: sa_schema.MetaData, autogen_context: AutogenContext
) -> sa_schema.MetaData:
    """Return a lightweight copy of the reflected MetaData in which
    renamed columns carry their new (target) names.

    All reflected tables are copied into a single new MetaData so that
    foreign key references between compared tables resolve against the
    renamed column identities.  This lets constraint and index
    comparison match database-side objects to MetaData-side objects
    using the renamed column identity, so that constraints and indexes
    spanning a renamed column are not spuriously dropped and re-created.
    """
    renamed = sa_schema.MetaData()
    for table in existing_metadata.tables.values():
        renames = autogen_context._column_renames_for_table(
            table.schema, table.name
        )
        new_table = sa_schema.Table(
            table.name,
            renamed,
            schema=table.schema,
            comment=table.comment,
        )
        for col in table.c:
            new_table.append_column(
                sa_schema.Column(
                    renames.get(col.name, col.name),
                    col.type,
                    nullable=col.nullable,
                    server_default=col.server_default,
                    comment=col.comment,
                )
            )
    return renamed


@contextlib.contextmanager
def _compare_columns(
    schema: str | None,
    tname: quoted_name | str,
    conn_table: Table,
    metadata_table: Table,
    modify_table_ops: ModifyTableOps,
    autogen_context: AutogenContext,
    inspector: Inspector,
) -> Iterator[None]:
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

    # apply validated column renames: connection-side columns are
    # considered under their new (target) names, so that a renamed
    # column is compared against its MetaData counterpart instead of
    # producing a drop/add pair.
    renames = autogen_context._column_renames_for_table(schema, tname)
    renamed_old_names: dict[str, str] = {}
    for old_name, new_name in renames.items():
        if old_name in conn_col_names:
            renamed_old_names[new_name] = old_name
            conn_col_names[new_name] = conn_col_names.pop(old_name)

    for cname in metadata_col_names.difference(conn_col_names):
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
        conn_col = conn_col_names[colname]
        if not autogen_context.run_object_filters(
            metadata_col, colname, "column", False, conn_col
        ):
            continue
        if colname in renamed_old_names:
            alter_column_op = ops.AlterColumnOp(
                tname,
                renamed_old_names[colname],
                schema=schema,
                modify_name=colname,
            )
            log.info(
                "Detected renamed column '%s.%s' to '%s'",
                name,
                renamed_old_names[colname],
                colname,
            )
        else:
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

    yield

    for cname in set(conn_col_names).difference(metadata_col_names):
        if autogen_context.run_object_filters(
            conn_table.c[cname], cname, "column", True, None
        ):
            modify_table_ops.ops.append(
                ops.DropColumnOp.from_column_and_tablename(
                    schema, tname, conn_table.c[cname]
                )
            )
            log.info("Detected removed column '%s.%s'", name, cname)


def setup(plugin: Plugin) -> None:

    plugin.add_autogenerate_comparator(
        _autogen_for_tables,
        "schema",
        "tables",
    )
