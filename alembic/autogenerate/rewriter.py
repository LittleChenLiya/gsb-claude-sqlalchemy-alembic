from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from typing import Callable
from typing import TYPE_CHECKING

from .. import util
from ..operations import ops

if TYPE_CHECKING:
    from ..operations.ops import AddColumnOp
    from ..operations.ops import AlterColumnOp
    from ..operations.ops import CreateTableOp
    from ..operations.ops import DowngradeOps
    from ..operations.ops import MigrateOperation
    from ..operations.ops import MigrationScript
    from ..operations.ops import ModifyTableOps
    from ..operations.ops import OpContainer
    from ..operations.ops import UpgradeOps
    from ..runtime.migration import MigrationContext
    from ..script.revision import _GetRevArg

ProcessRevisionDirectiveFn = Callable[
    ["MigrationContext", "_GetRevArg", list["MigrationScript"]], None
]


class _ScopedRewriter:
    """A temporary registration handle returned by :meth:`.Rewriter.scope`.

    The object proxies :meth:`.rewrites` registrations into a private,
    branched :class:`.Dispatcher` installed on the parent :class:`.Rewriter`
    for the duration of the ``with`` block.  Registrations made through the
    scope replace any rule previously registered for the same directive
    type, including rules inherited from an enclosing scope, while rules of
    other types remain visible.

    """

    __slots__ = ("_rewriter",)

    def __init__(self, rewriter: Rewriter) -> None:
        self._rewriter = rewriter

    def rewrites(
        self,
        operator: (
            type[AddColumnOp]
            | type[MigrateOperation]
            | type[AlterColumnOp]
            | type[CreateTableOp]
            | type[ModifyTableOps]
        ),
    ) -> Callable[..., Any]:
        """Register a temporary rewrite function for a given type.

        Has the same signature as :meth:`.Rewriter.rewrites`.  Unlike a
        permanent registration, a registration of the same type is allowed
        and replaces the rule active in the current scope; it is discarded
        as soon as the scope exits.

        """
        return self._rewriter.dispatch.dispatch_for(operator, replace=True)


class Rewriter:
    """A helper object that allows easy 'rewriting' of ops streams.

    The :class:`.Rewriter` object is intended to be passed along
    to the
    :paramref:`.EnvironmentContext.configure.process_revision_directives`
    parameter in an ``env.py`` script.    Once constructed, any number
    of "rewrites" functions can be associated with it, which will be given
    the opportunity to modify the structure without having to have explicit
    knowledge of the overall structure.

    The function is passed the :class:`.MigrationContext` object and
    ``revision`` tuple that are passed to the  :paramref:`.Environment
    Context.configure.process_revision_directives` function normally,
    and the third argument is an individual directive of the type
    noted in the decorator.  The function has the choice of  returning
    a single op directive, which normally can be the directive that
    was actually passed, or a new directive to replace it, or a list
    of zero or more directives to replace it.

    .. seealso::

        :ref:`autogen_rewriter` - usage example

    """

    _traverse = util.Dispatcher()

    _chained: tuple[ProcessRevisionDirectiveFn | Rewriter, ...] = ()

    def __init__(self) -> None:
        self.dispatch = util.Dispatcher()
        self._scope_stack: list[util.Dispatcher] = []
        self._active_traversal: list[tuple[MigrateOperation, Any]] | None = (
            None
        )

    def chain(
        self,
        other: ProcessRevisionDirectiveFn | Rewriter,
    ) -> Rewriter:
        """Produce a "chain" of this :class:`.Rewriter` to another.

        This allows two or more rewriters to operate serially on a stream,
        e.g.::

            writer1 = autogenerate.Rewriter()
            writer2 = autogenerate.Rewriter()


            @writer1.rewrites(ops.AddColumnOp)
            def add_column_nullable(context, revision, op):
                op.column.nullable = True
                return op


            @writer2.rewrites(ops.AddColumnOp)
            def add_column_idx(context, revision, op):
                idx_op = ops.CreateIndexOp(
                    "ixc", op.table_name, [op.column.name]
                )
                return [op, idx_op]

            writer = writer1.chain(writer2)

        :param other: a :class:`.Rewriter` instance
        :return: a new :class:`.Rewriter` that will run the operations
         of this writer, then the "other" writer, in succession.

        """
        wr = self.__class__.__new__(self.__class__)
        wr.__dict__.update(self.__dict__)
        # an active scope on the chained view must not push/pop dispatchers
        # on the rewriter it was chained from
        wr._scope_stack = list(self._scope_stack)
        wr._chained += (other,)
        return wr

    def rewrites(
        self,
        operator: (
            type[AddColumnOp]
            | type[MigrateOperation]
            | type[AlterColumnOp]
            | type[CreateTableOp]
            | type[ModifyTableOps]
        ),
    ) -> Callable[..., Any]:
        """Register a function as rewriter for a given type.

        The function should receive three arguments, which are
        the :class:`.MigrationContext`, a ``revision`` tuple, and
        an op directive of the type indicated.  E.g.::

            @writer1.rewrites(ops.AddColumnOp)
            def add_column_nullable(context, revision, op):
                op.column.nullable = True
                return op

        """
        return self.dispatch.dispatch_for(operator)

    @contextmanager
    def scope(self) -> Iterator[_ScopedRewriter]:
        """Open a local registration scope for temporary rewrite rules.

        This is useful for one-off migration tooling that needs to install
        rewrite rules just for a single, scoped section of code without
        modifying the permanently registered rules of this
        :class:`.Rewriter`::

            writer = autogenerate.Rewriter()

            @writer.rewrites(ops.AddColumnOp)
            def add_column_default(context, revision, op):
                ...

            with writer.scope() as temporary:

                @temporary.rewrites(ops.AddColumnOp)
                def add_column_for_one_off_tool(context, revision, op):
                    ...

                # this traversal uses add_column_for_one_off_tool
                writer(context, revision, directives)

            # exiting the block restores add_column_default

        Scopes can be nested.  The dispatcher of an inner scope is branched
        from the one of the immediately enclosing scope, and on exit scopes
        are restored in last-in/first-out order.  A temporary rule
        registered for a directive type overrides, only for the duration of
        that scope, whatever rule (permanent or temporary) was previously
        active for the same type; rules registered for other types are
        unaffected.  The previous rules are restored even if the block
        raises an exception.

        Temporary registrations are per-:class:`.Rewriter` object; they are
        only visible to the object (and to a :meth:`.chain` derived while
        the scope is active) on which the scope was opened, never to other
        independent :class:`.Rewriter` instances.

        """
        branched = self.dispatch.branch()
        self._scope_stack.append(self.dispatch)
        self.dispatch = branched
        try:
            yield _ScopedRewriter(self)
        finally:
            self.dispatch = self._scope_stack.pop()

    def _rewrite(
        self,
        context: MigrationContext,
        revision: _GetRevArg,
        directive: MigrateOperation,
    ) -> Iterator[MigrateOperation]:
        try:
            _rewriter = self.dispatch.dispatch(directive)
        except ValueError:
            _rewriter = None
            yield directive
        else:
            if self in directive._mutations:
                yield directive
            else:
                for r_directive in util.to_list(
                    _rewriter(context, revision, directive), []
                ):
                    active_traversal = self._active_traversal
                    if (
                        active_traversal is not None
                        and self not in r_directive._mutations
                    ):
                        # remember the previous state so that markers added
                        # by this traversal alone can be removed afterwards
                        active_traversal.append(
                            (r_directive, r_directive._mutations)
                        )
                    r_directive._mutations = r_directive._mutations.union(
                        [self]
                    )
                    yield r_directive

    def __call__(
        self,
        context: MigrationContext,
        revision: _GetRevArg,
        directives: list[MigrationScript],
    ) -> None:
        self.process_revision_directives(context, revision, directives)
        for process_revision_directives in self._chained:
            process_revision_directives(context, revision, directives)

    @_traverse.dispatch_for(ops.MigrationScript)
    def _traverse_script(
        self,
        context: MigrationContext,
        revision: _GetRevArg,
        directive: MigrationScript,
    ) -> None:
        upgrade_ops_list: list[UpgradeOps] = []
        for upgrade_ops in directive.upgrade_ops_list:
            ret = self._traverse_for(context, revision, upgrade_ops)
            if len(ret) != 1:
                raise ValueError(
                    "Can only return single object for UpgradeOps traverse"
                )
            upgrade_ops_list.append(ret[0])

        directive.upgrade_ops = upgrade_ops_list

        downgrade_ops_list: list[DowngradeOps] = []
        for downgrade_ops in directive.downgrade_ops_list:
            ret = self._traverse_for(context, revision, downgrade_ops)
            if len(ret) != 1:
                raise ValueError(
                    "Can only return single object for DowngradeOps traverse"
                )
            downgrade_ops_list.append(ret[0])
        directive.downgrade_ops = downgrade_ops_list

    @_traverse.dispatch_for(ops.OpContainer)
    def _traverse_op_container(
        self,
        context: MigrationContext,
        revision: _GetRevArg,
        directive: OpContainer,
    ) -> None:
        self._traverse_list(context, revision, directive.ops)

    @_traverse.dispatch_for(ops.MigrateOperation)
    def _traverse_any_directive(
        self,
        context: MigrationContext,
        revision: _GetRevArg,
        directive: MigrateOperation,
    ) -> None:
        pass

    def _traverse_for(
        self,
        context: MigrationContext,
        revision: _GetRevArg,
        directive: MigrateOperation,
    ) -> Any:
        directives = list(self._rewrite(context, revision, directive))
        for directive in directives:
            traverser = self._traverse.dispatch(directive)
            traverser(self, context, revision, directive)
        return directives

    def _traverse_list(
        self,
        context: MigrationContext,
        revision: _GetRevArg,
        directives: Any,
    ) -> None:
        dest = []
        for directive in directives:
            dest.extend(self._traverse_for(context, revision, directive))

        directives[:] = dest

    def process_revision_directives(
        self,
        context: MigrationContext,
        revision: _GetRevArg,
        directives: list[MigrationScript],
    ) -> None:
        # The "_mutations" markers recorded on directives prevent the same
        # rewriter from processing an emitted directive again within one
        # traversal.  They are scoped to this traversal only: snapshot every
        # directive this traversal marks and restore the previous state on
        # exit, including when an exception is raised, so that a subsequent
        # traversal (or another :class:`.Rewriter` sharing the directive
        # tree) does not inherit stale "already processed" markers.
        previous_traversal = self._active_traversal
        active_traversal: list[tuple[MigrateOperation, Any]] = []
        self._active_traversal = active_traversal
        try:
            self._traverse_list(context, revision, directives)
        finally:
            for directive, previous_mutations in reversed(active_traversal):
                directive._mutations = previous_mutations
            self._active_traversal = previous_traversal
