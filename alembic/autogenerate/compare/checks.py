# mypy: allow-untyped-defs, allow-untyped-calls, allow-incomplete-defs

"""Compare explicitly "managed" CHECK constraints.

This module implements the
:paramref:`.EnvironmentContext.configure.managed_check_constraints`
feature, where the caller names specific CHECK constraints, per table,
that autogenerate should track.   Only the constraints named in the
option are examined; all other CHECK constraints are left to whatever
handling (if any) is otherwise in effect.

"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import TYPE_CHECKING

from sqlalchemy import schema as sa_schema

from .util import _InspectorConv
from ... import util
from ...operations import ops
from ...util import PriorityDispatchResult
from ...util import sqla_compat

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import ReflectedCheckConstraint
    from sqlalchemy.sql.elements import quoted_name
    from sqlalchemy.sql.schema import CheckConstraint
    from sqlalchemy.sql.schema import Table

    from ...autogenerate.api import AutogenContext
    from ...ddl.impl import DefaultImpl
    from ...operations.ops import ModifyTableOps
    from ...runtime.plugins import Plugin


log = logging.getLogger(__name__)


def managed_check_constraint_names(
    autogen_context: AutogenContext,
    schema: str | None,
    tname: quoted_name | str,
) -> frozenset[str]:
    """Return the names of CHECK constraints that are explicitly managed
    for the given table, per the ``managed_check_constraints`` option.

    The option is a mapping of table keys to collections of constraint
    names.  Table keys may be plain table names, ``"schema.table"``
    strings, or ``(schema, table)`` tuples.

    """
    managed = autogen_context.opts.get("managed_check_constraints")
    if not managed:
        return frozenset()
    if not isinstance(managed, Mapping):
        raise util.CommandError(
            "managed_check_constraints must be a mapping of "
            "table name to a collection of constraint names; "
            f"got {managed!r}"
        )

    names: set[str] = set()
    str_tname = str(tname)
    for key, value in managed.items():
        if isinstance(key, tuple):
            key_schema, key_table = key
        elif "." in key:
            key_schema, key_table = key.split(".", 1)
        else:
            key_schema, key_table = None, key
        if str(key_table) != str_tname:
            continue
        if key_schema is not None and str(key_schema) != str(schema or ""):
            continue
        if isinstance(value, str) or not hasattr(value, "__iter__"):
            raise util.CommandError(
                "managed_check_constraints values must be collections "
                f"of constraint names; got {value!r} for table key "
                f"{key!r}"
            )
        names.update(value)
    return frozenset(names)


def _tokenize_check_expression(
    sqltext: str,
) -> list[tuple[str, str]] | None:
    """Tokenize a CHECK constraint expression.

    Returns a list of ``(kind, text)`` tokens where ``kind`` is one of
    ``"word"`` (identifiers, keywords, numbers), ``"literal"`` (quoted
    string literals, kept verbatim), ``"qident"`` (quoted identifiers,
    kept verbatim) or ``"punct"`` (single punctuation/operator
    characters).  SQL comments are treated as whitespace.

    Returns ``None`` if the text cannot be tokenized safely, e.g.
    unterminated string literals or quoted identifiers; callers must
    treat this as "cannot be normalized".

    """
    tokens: list[tuple[str, str]] = []
    i = 0
    n = len(sqltext)
    while i < n:
        ch = sqltext[i]
        if ch.isspace():
            i += 1
            continue
        elif ch == "'":
            # string literal; '' is an escaped quote
            j = i + 1
            while j < n:
                if sqltext[j] == "'":
                    if j + 1 < n and sqltext[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                return None
            tokens.append(("literal", sqltext[i : j + 1]))
            i = j + 1
        elif ch == '"' or ch == "`":
            # quoted identifier; doubled quote char is an escaped quote
            j = i + 1
            while j < n:
                if sqltext[j] == ch:
                    if j + 1 < n and sqltext[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                return None
            tokens.append(("qident", sqltext[i : j + 1]))
            i = j + 1
        elif ch == "[":
            # bracket-quoted identifier (SQL Server style)
            j = sqltext.find("]", i + 1)
            if j == -1:
                return None
            tokens.append(("qident", sqltext[i : j + 1]))
            i = j + 1
        elif ch == "-" and sqltext[i : i + 2] == "--":
            # line comment; treat as whitespace
            j = sqltext.find("\n", i)
            i = n if j == -1 else j
        elif ch == "/" and sqltext[i : i + 2] == "/*":
            # block comment; treat as whitespace
            j = sqltext.find("*/", i + 2)
            if j == -1:
                return None
            i = j + 2
        elif ch.isalnum() or ch in "_$" or ord(ch) > 127:
            j = i + 1
            while j < n and (
                sqltext[j].isalnum()
                or sqltext[j] in "_$"
                or ord(sqltext[j]) > 127
            ):
                j += 1
            tokens.append(("word", sqltext[i:j]))
            i = j
        else:
            tokens.append(("punct", ch))
            i += 1
    return tokens


def _strip_redundant_parens(tokens: list[tuple[str, str]]) -> None:
    """Remove, in place, parenthesis pairs that wrap the whole
    expression, as well as trailing close parens that match nothing.

    Trailing unmatched close parens are an artifact of some reflection
    backends (e.g. SQLite's ``get_check_constraints()`` on some
    SQLAlchemy versions includes the ``CHECK (...)`` wrapper's own
    closing paren in the reflected text); they are meaningless for
    comparison purposes.

    """
    # drop trailing unmatched close parens
    while tokens and tokens[-1] == ("punct", ")"):
        depth = 0
        unmatched = True
        for kind, tok in tokens[:-1]:
            if tok == "(":
                depth += 1
            elif tok == ")":
                depth -= 1
            if depth < 0:
                unmatched = False
                break
        if depth != 0:
            unmatched = False
        if not unmatched:
            break
        tokens.pop()

    # drop outer pairs that enclose the entire expression
    while (
        len(tokens) >= 2
        and tokens[0] == ("punct", "(")
        and tokens[-1] == ("punct", ")")
    ):
        depth = 0
        encloses = True
        for kind, tok in tokens[:-1]:
            if tok == "(":
                depth += 1
            elif tok == ")":
                depth -= 1
            if depth <= 0:
                encloses = False
                break
        if not encloses:
            break
        del tokens[-1]
        del tokens[0]


def normalize_check_expression(sqltext: str) -> str | None:
    """Return a normalized form of a CHECK constraint expression,
    suitable for comparison, or ``None`` if the expression cannot be
    normalized safely.

    Normalization is deliberately conservative:

    * runs of whitespace are collapsed; whitespace that only serves as
      a token separator is removed
    * redundant outer parentheses, including unmatched trailing close
      parens produced by some reflection backends, are removed
    * string literals and quoted identifiers are preserved exactly,
      including case
    * tokens are never reordered, and operator/identifier case is
      never changed

    If the text cannot be tokenized or the parentheses are not
    balanced, ``None`` is returned, in which case callers should
    conservatively report the expressions as different.

    """
    tokens = _tokenize_check_expression(sqltext)
    if tokens is None:
        return None

    _strip_redundant_parens(tokens)

    if not tokens:
        return None

    # validate balance
    depth = 0
    for kind, tok in tokens:
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        if depth < 0:
            return None
    if depth != 0:
        return None

    # join; a space is only required between two adjacent word-like
    # tokens so they don't merge into one
    word_like = ("word", "literal", "qident")
    parts: list[tuple[str, str]] = []
    for kind, tok in tokens:
        if parts and kind in word_like and parts[-1][0] in word_like:
            parts.append(("space", " "))
        parts.append((kind, tok))
    return "".join(tok for kind, tok in parts)


def _check_expressions_equal(
    metadata_sqltext: str, reflected_sqltext: str
) -> bool:
    """Compare two CHECK constraint expressions for equivalence.

    Returns ``False`` conservatively whenever the two expressions
    cannot be normalized.

    """
    if metadata_sqltext.strip() == reflected_sqltext.strip():
        return True
    meta_norm = normalize_check_expression(metadata_sqltext)
    conn_norm = normalize_check_expression(reflected_sqltext)
    if meta_norm is None or conn_norm is None:
        # cannot normalize; conservatively report a difference
        return False
    return meta_norm == conn_norm


def _sanitize_reflected_sqltext(sqltext: str) -> str:
    """Remove reflection artifacts from reflected CHECK constraint text,
    leaving the original expression otherwise intact.

    This only strips trailing close parens that match nothing, such as
    the ``CHECK (...)`` wrapper's own closing paren that some reflection
    backends include in the reflected text.  Unlike
    :func:`normalize_check_expression`, the text is otherwise preserved
    exactly as reflected, so that operations which re-create the
    constraint (e.g. downgrades) use the original expression.

    """
    text = sqltext
    while True:
        stripped = text.rstrip()
        if not stripped.endswith(")"):
            return text
        tokens = _tokenize_check_expression(stripped)
        if tokens is None:
            return text
        depth = 0
        unmatched = True
        for kind, tok in tokens[:-1]:
            if tok == "(":
                depth += 1
            elif tok == ")":
                depth -= 1
            if depth < 0:
                unmatched = False
                break
        if depth != 0:
            unmatched = False
        if not unmatched:
            return text
        text = stripped[:-1]


def _make_check_constraint(
    impl: DefaultImpl,
    params: ReflectedCheckConstraint,
    conn_table: Table,
) -> CheckConstraint:
    const = sa_schema.CheckConstraint(
        _sanitize_reflected_sqltext(params["sqltext"]),
        name=params["name"],
        table=conn_table,
        **impl.adjust_reflected_dialect_options(params, "check_constraint"),
    )
    return const


def _compare_managed_check_constraints(
    autogen_context: AutogenContext,
    modify_table_ops: ModifyTableOps,
    schema: str | None,
    tname: quoted_name | str,
    conn_table: Table | None,
    metadata_table: Table | None,
) -> PriorityDispatchResult:
    if conn_table is None or metadata_table is None:
        # for CREATE TABLE / DROP TABLE, constraints are handled
        # inline with the table itself
        return PriorityDispatchResult.CONTINUE

    managed_names = managed_check_constraint_names(
        autogen_context, schema, tname
    )
    if not managed_names:
        return PriorityDispatchResult.CONTINUE

    inspector = autogen_context.inspector
    impl = autogen_context.migration_context.impl
    dialect = autogen_context.dialect

    try:
        conn_ck_list = _InspectorConv(inspector).get_check_constraints(
            tname, schema=schema
        )
    except NotImplementedError:
        util.warn(
            "Dialect does not support reflection of CHECK constraints; "
            "skipping managed CHECK constraint comparison for table "
            f"{tname!r}"
        )
        return PriorityDispatchResult.CONTINUE

    conn_ck_by_name: dict[str, ReflectedCheckConstraint] = {}
    for ck_def in conn_ck_list:
        name = ck_def.get("name")
        if name is None or name not in managed_names:
            continue
        if not autogen_context.run_name_filters(
            name,
            "check_constraint",
            {"table_name": tname, "schema_name": schema},
        ):
            continue
        conn_ck_by_name[name] = ck_def

    metadata_ck_by_name: dict[str, CheckConstraint] = {}
    for ck in sqla_compat.all_table_check_constraints(metadata_table):
        final_name = sqla_compat._get_constraint_final_name(ck, dialect)
        if final_name is None or final_name not in managed_names:
            continue
        metadata_ck_by_name[final_name] = ck

    emitted = False
    for name in sorted(managed_names):
        metadata_ck = metadata_ck_by_name.get(name)
        conn_ck_def = conn_ck_by_name.get(name)

        if metadata_ck is not None and conn_ck_def is not None:
            conn_ck = _make_check_constraint(impl, conn_ck_def, conn_table)
            if not _check_expressions_equal(
                str(metadata_ck.sqltext), str(conn_ck.sqltext)
            ):
                if autogen_context.run_object_filters(
                    metadata_ck, name, "check_constraint", False, conn_ck
                ):
                    log.info(
                        "Detected changed managed check constraint %r "
                        "on table %r",
                        name,
                        tname,
                    )
                    # an expression change is an explicit drop + create
                    # pair; the two operations are always emitted
                    # together so that a failed comparison never
                    # produces a half-complete change
                    modify_table_ops.ops.append(
                        ops.DropConstraintOp.from_constraint(conn_ck)
                    )
                    modify_table_ops.ops.append(
                        ops.AddConstraintOp.from_constraint(metadata_ck)
                    )
                    emitted = True
        elif metadata_ck is not None:
            if autogen_context.run_object_filters(
                metadata_ck, name, "check_constraint", False, None
            ):
                log.info(
                    "Detected added managed check constraint %r on table %r",
                    name,
                    tname,
                )
                modify_table_ops.ops.append(
                    ops.AddConstraintOp.from_constraint(metadata_ck)
                )
                emitted = True
        elif conn_ck_def is not None:
            conn_ck = _make_check_constraint(impl, conn_ck_def, conn_table)
            if autogen_context.run_object_filters(
                conn_ck, name, "check_constraint", True, None
            ):
                log.info(
                    "Detected removed managed check constraint %r "
                    "on table %r",
                    name,
                    tname,
                )
                modify_table_ops.ops.append(
                    ops.DropConstraintOp.from_constraint(conn_ck)
                )
                emitted = True
        else:
            util.warn(
                f"Managed check constraint {name!r} on table {tname!r} "
                "was not found in either the database or the metadata; "
                "no operations will be generated for it."
            )

    if emitted and dialect.name == "sqlite":
        # SQLite has no ALTER TABLE ... ADD/DROP CONSTRAINT; constraint
        # changes must go through the batch "recreate the table" path
        modify_table_ops.render_as_batch = True

    return PriorityDispatchResult.CONTINUE


def setup(plugin: Plugin) -> None:
    plugin.add_autogenerate_comparator(
        _compare_managed_check_constraints,
        "table",
        "managed_check_constraints",
    )
