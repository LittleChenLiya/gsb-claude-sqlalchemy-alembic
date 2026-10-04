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
        self._dispatch_stack: list[util.Dispatcher] = []
        self._mutation_token: object | None = None

    @contextmanager
    def scope(self) -> Iterator[Rewriter]:
        """Context manager providing a temporary registration scope.

        A :class:`.Rewriter` may be placed into a temporary scope, within
        which rewrite rules may be registered that override those registered
        normally.  When the scope is exited, the original rules are restored.
        Nested scopes are restored in LIFO order, and a scope is restored
        even if an exception is raised within it.

        E.g.::

            writer = autogenerate.Rewriter()

            @writer.rewrites(ops.AddColumnOp)
            def mark_column(context, revision, op):
                op.column.name = "A"
                return op

            with writer.scope():
                @writer.rewrites(ops.AddColumnOp)
                def mark_column_temporarily(context, revision, op):
                    op.column.name = "B"
                    return op

                # within the scope, "B" is applied
                writer(context, revision, directives)

            # outside the scope, "A" is applied again
            writer(context, revision, directives)

        The scope is local to this :class:`.Rewriter` instance; rewrite
        rules registered within it are not visible to other
        :class:`.Rewriter` instances, including those that have already
        been created.

        .. seealso::

            :ref:`autogen_rewriter` - usage example

        """
        self._dispatch_stack.append(util.Dispatcher())
        try:
            yield self
        finally:
            self._dispatch_stack.pop()

    @property
    def _active_dispatch(self) -> util.Dispatcher:
        """Return the dispatcher that is currently active.

        This is the dispatcher at the top of the temporary scope stack,
        or the base dispatcher if no scope is active.

        """
        if self._dispatch_stack:
            return self._dispatch_stack[-1]
        return self.dispatch

    def _all_dispatchers(self) -> Iterator[util.Dispatcher]:
        """Iterate over all dispatchers, from the active scope down to
        the base dispatcher."""

        yield from reversed(self._dispatch_stack)
        yield self.dispatch

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
        return self._active_dispatch.dispatch_for(operator)

    def _rewrite(
        self,
        context: MigrationContext,
        revision: _GetRevArg,
        directive: MigrateOperation,
    ) -> Iterator[MigrateOperation]:
        _rewriter: Any = None
        for dispatcher in self._all_dispatchers():
            try:
                _rewriter = dispatcher.dispatch(directive)
                break
            except ValueError:
                continue

        if _rewriter is None:
            yield directive
        elif self._mutation_token in directive._mutations:
            yield directive
        else:
            for r_directive in util.to_list(
                _rewriter(context, revision, directive), []
            ):
                if self._mutation_token is not None:
                    r_directive._mutations = r_directive._mutations.union(
                        [self._mutation_token]
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
        self._mutation_token = object()
        try:
            self._traverse_list(context, revision, directives)
        finally:
            self._mutation_token = None
