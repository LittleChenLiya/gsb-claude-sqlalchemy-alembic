# mypy: allow-untyped-defs, allow-incomplete-defs, allow-untyped-calls
# mypy: no-warn-return-any, allow-any-generics
from __future__ import annotations

from collections.abc import Collection
from collections.abc import Mapping
from typing import Any
from typing import TYPE_CHECKING

from sqlalchemy import schema as sa_schema
from sqlalchemy.sql.elements import conv

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.engine import Inspector
    from sqlalchemy.engine.interfaces import ReflectedCheckConstraint
    from sqlalchemy.engine.interfaces import ReflectedForeignKeyConstraint
    from sqlalchemy.engine.interfaces import ReflectedIndex
    from sqlalchemy.engine.interfaces import ReflectedUniqueConstraint
    from sqlalchemy.engine.reflection import _ReflectionInfo
    from sqlalchemy.sql.schema import CheckConstraint

    from ...ddl.impl import DefaultImpl

_INSP_KEYS = (
    "columns",
    "pk_constraint",
    "foreign_keys",
    "indexes",
    "unique_constraints",
    "table_comment",
    "check_constraints",
    "table_options",
)
_CONSTRAINT_INSP_KEYS = (
    "pk_constraint",
    "foreign_keys",
    "indexes",
    "unique_constraints",
    "check_constraints",
)


def _tokenize_ck_expression(text: str) -> list[tuple[str, str]] | None:
    """Tokenize a CHECK constraint expression for normalization.

    Returns a list of ``(kind, text)`` tokens where ``kind`` is one of
    ``"literal"`` (string literals and quoted identifiers, kept verbatim),
    ``"word"`` (keywords, identifiers, numbers) or ``"punct"`` (individual
    punctuation / operator characters).  Whitespace is discarded.

    Returns ``None`` if the text cannot be tokenized, e.g. it contains an
    unterminated string literal or quoted identifier.  In that case the
    caller should assume the expression cannot be normalized.
    """
    tokens: list[tuple[str, str]] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ("'", '"', "`"):
            # string literal or quoted identifier; the quote char may be
            # escaped by doubling it
            j = i + 1
            while True:
                j = text.find(ch, j)
                if j == -1:
                    return None
                if j + 1 < n and text[j + 1] == ch:
                    j += 2
                    continue
                break
            tokens.append(("literal", text[i : j + 1]))
            i = j + 1
        elif ch == "[":
            # bracket-quoted identifier (SQL Server style)
            j = text.find("]", i + 1)
            if j == -1:
                return None
            tokens.append(("literal", text[i : j + 1]))
            i = j + 1
        elif ch.isspace():
            i += 1
        elif ch.isalnum() or ch in ("_", "$"):
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in ("_", "$")):
                j += 1
            tokens.append(("word", text[i:j]))
            i = j
        else:
            tokens.append(("punct", ch))
            i += 1
    return tokens


def _ck_tokens_paren_balance(tokens: list[tuple[str, str]]) -> int:
    depth = 0
    for kind, value in tokens:
        if kind == "punct":
            if value == "(":
                depth += 1
            elif value == ")":
                depth -= 1
    return depth


def _strip_unbalanced_trailing_parens(text: str) -> str:
    """Remove trailing ``)`` characters that unbalance the expression.

    Some reflection paths (e.g. older SQLite dialect parsing) include the
    closing parenthesis of the surrounding ``CHECK (...)`` clause - and
    even that of the CREATE TABLE statement itself - in the reflected
    expression text.  A trailing ``)`` that makes the parenthesis balance
    negative cannot be part of a valid standalone expression, so it is
    safe to drop.  All other characters, including whitespace, string
    literals and quoted identifiers, are kept exactly as written; if the
    text cannot be tokenized it is returned unchanged.
    """
    tokens = _tokenize_ck_expression(text)
    if tokens is None:
        return text
    pop = 0
    balance = _ck_tokens_paren_balance(tokens)
    while (
        pop < len(tokens)
        and tokens[-1 - pop] == ("punct", ")")
        and balance < 0
    ):
        pop += 1
        balance += 1
    if not pop:
        return text
    # drop the last ``pop`` ")" characters from the original text; only
    # whitespace may follow them, so the remaining text is the original
    # expression verbatim
    out = []
    remaining = pop
    for ch in reversed(text):
        if remaining and ch == ")":
            remaining -= 1
            continue
        out.append(ch)
    return "".join(reversed(out)).rstrip()


def _normalize_check_expression(text: str) -> str | None:
    """Normalize a CHECK constraint expression for comparison.

    The normalization is deliberately conservative: it removes
    insignificant whitespace and redundant outer parentheses only.
    String literals, quoted (case-sensitive) identifiers and the order of
    tokens - and therefore of operators and operands - are preserved
    exactly as written.

    Returns ``None`` if the expression cannot be tokenized or has
    unbalanced parentheses; callers should treat this as "cannot be
    compared" and conservatively report a difference.
    """
    tokens = _tokenize_ck_expression(text)
    if tokens is None:
        return None

    # strip pairs of parentheses that enclose the whole expression
    while (
        len(tokens) >= 2
        and tokens[0] == ("punct", "(")
        and tokens[-1] == ("punct", ")")
    ):
        depth = 0
        encloses_all = True
        for idx, (kind, value) in enumerate(tokens):
            if kind == "punct":
                if value == "(":
                    depth += 1
                elif value == ")":
                    depth -= 1
                    if depth < 0:
                        return None
                    if depth == 0 and idx != len(tokens) - 1:
                        encloses_all = False
                        break
        if depth != 0:
            return None
        if not encloses_all:
            break
        tokens = tokens[1:-1]

    # remaining parentheses must be balanced
    depth = 0
    for kind, value in tokens:
        if kind == "punct":
            if value == "(":
                depth += 1
            elif value == ")":
                depth -= 1
                if depth < 0:
                    return None
    if depth != 0:
        return None

    # join tokens; a single space is kept only between two adjacent
    # word-like tokens so that e.g. "a b" cannot become "ab"
    out: list[str] = []
    prev_kind: str | None = None
    for kind, value in tokens:
        if (
            out
            and prev_kind in ("word", "literal")
            and kind in ("word", "literal")
        ):
            out.append(" ")
        out.append(value)
        prev_kind = kind
    return "".join(out)


def _make_check_constraint(
    impl: DefaultImpl,
    params: ReflectedCheckConstraint,
    conn_table: Table,
) -> CheckConstraint:
    sqltext = params["sqltext"]
    if isinstance(sqltext, str):
        sqltext = _strip_unbalanced_trailing_parens(sqltext)
    const = sa_schema.CheckConstraint(
        sqltext,
        name=params["name"],
        table=conn_table,
        **impl.adjust_reflected_dialect_options(params, "check_constraint"),
    )
    return const


def _managed_ck_names_for_table(
    managed: Mapping[str, Collection[str]] | None,
    schema: str | None,
    tname: str,
) -> frozenset[str]:
    """Return the set of managed CHECK constraint names for a table.

    The ``managed`` mapping is keyed by table name, optionally
    schema-qualified as ``"schemaname.tablename"``.  A schema-qualified
    entry takes precedence over an unqualified one for the same table.
    """
    if not managed:
        return frozenset()
    names: Collection[str] | None = None
    if schema:
        names = managed.get("%s.%s" % (schema, tname))
    if names is None:
        names = managed.get(tname)
    if not names:
        return frozenset()
    return frozenset(str(name) for name in names)


class _InspectorConv:
    __slots__ = ("inspector",)

    def __init__(self, inspector: Inspector):
        self.inspector = inspector

    def _pre_cache(
        self,
        schema: str | None,
        tablenames: list[str],
        all_available_tablenames: Collection[str],
        info_key: str,
        inspector_method: Any,
    ) -> None:

        if info_key in self.inspector.info_cache:
            return

        # heuristic vendored from SQLAlchemy 2.0
        # if more than 50% of the tables in the db are in filter_names load all
        # the tables, since it's most likely faster to avoid a filter on that
        # many tables. also if a dialect doesnt have a "multi" method then
        # return the filter names
        if tablenames and all_available_tablenames and len(tablenames) > 100:
            fraction = len(tablenames) / len(all_available_tablenames)
        else:
            fraction = None

        if (
            fraction is None
            or fraction <= 0.5
            or not self.inspector.dialect._overrides_default(
                inspector_method.__name__
            )
        ):
            optimized_filter_names = tablenames
        else:
            optimized_filter_names = None

        try:
            elements = inspector_method(
                schema=schema, filter_names=optimized_filter_names
            )
        except NotImplementedError:
            self.inspector.info_cache[info_key] = NotImplementedError
        else:
            self.inspector.info_cache[info_key] = elements

    def _return_from_cache(
        self,
        tname: str,
        schema: str | None,
        info_key: str,
        inspector_method: Any,
        apply_constraint_conv: bool = False,
        optional=True,
    ) -> Any:
        not_in_cache = object()

        if info_key in self.inspector.info_cache:
            cache = self.inspector.info_cache[info_key]
            if cache is NotImplementedError:
                if optional:
                    return {}
                else:
                    # maintain NotImplementedError as alembic compare
                    # uses these to determine classes of construct that it
                    # should not compare to DB elements
                    raise NotImplementedError()

            individual = cache.get((schema, tname), not_in_cache)

            if individual is not not_in_cache:
                if apply_constraint_conv and individual is not None:
                    return self._apply_reflectinfo_conv(individual)
                else:
                    return individual

        try:
            data = inspector_method(tname, schema=schema)
        except NotImplementedError:
            if optional:
                return {}
            else:
                raise

        if apply_constraint_conv:
            return self._apply_reflectinfo_conv(data)
        else:
            return data

    def get_unique_constraints(
        self, tname: str, schema: str | None
    ) -> list[ReflectedUniqueConstraint]:
        return self._return_from_cache(
            tname,
            schema,
            "alembic_unique_constraints",
            self.inspector.get_unique_constraints,
            apply_constraint_conv=True,
            optional=False,
        )

    def get_indexes(
        self, tname: str, schema: str | None
    ) -> list[ReflectedIndex]:
        return self._return_from_cache(
            tname,
            schema,
            "alembic_indexes",
            self.inspector.get_indexes,
            apply_constraint_conv=True,
            optional=False,
        )

    def get_foreign_keys(
        self, tname: str, schema: str | None
    ) -> list[ReflectedForeignKeyConstraint]:
        return self._return_from_cache(
            tname,
            schema,
            "alembic_foreign_keys",
            self.inspector.get_foreign_keys,
            apply_constraint_conv=True,
        )

    def get_check_constraints(
        self, tname: str, schema: str | None
    ) -> list[ReflectedCheckConstraint]:
        return self._return_from_cache(
            tname,
            schema,
            "alembic_check_constraints",
            self.inspector.get_check_constraints,
            apply_constraint_conv=True,
            optional=False,
        )

    def _apply_reflectinfo_conv(self, consts):
        if not consts:
            return consts
        for const in consts if not isinstance(consts, dict) else [consts]:
            if const["name"] is not None and not isinstance(
                const["name"], conv
            ):
                const["name"] = conv(const["name"])
        return consts

    def pre_cache_tables(
        self,
        schema: str | None,
        tablenames: list[str],
        all_available_tablenames: Collection[str],
    ) -> None:
        for key in _INSP_KEYS:
            keyname = f"alembic_{key}"
            meth = getattr(self.inspector, f"get_multi_{key}")

            self._pre_cache(
                schema,
                tablenames,
                all_available_tablenames,
                keyname,
                meth,
            )

    def _make_reflection_info(
        self, tname: str, schema: str | None
    ) -> _ReflectionInfo:
        from sqlalchemy.engine.reflection import _ReflectionInfo

        table_key = (schema, tname)

        return _ReflectionInfo(
            unreflectable={},
            **{
                key: {
                    table_key: self._return_from_cache(
                        tname,
                        schema,
                        f"alembic_{key}",
                        getattr(self.inspector, f"get_{key}"),
                        apply_constraint_conv=(key in _CONSTRAINT_INSP_KEYS),
                    )
                }
                for key in _INSP_KEYS
            },
        )

    def reflect_table(self, table: Table) -> None:
        ri = self._make_reflection_info(table.name, table.schema)

        self.inspector.reflect_table(
            table,
            include_columns=None,
            resolve_fks=False,
            _reflect_info=ri,
        )
