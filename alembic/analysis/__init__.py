"""Offline analysis of migration operation trees.

This package provides a *pure, in-memory* analyzer that inspects a tree of
:class:`~alembic.operations.ops.MigrateOperation` objects and splits it into
ordered fragments grouped by whether they are expected to keep a single
transaction boundary.

The analyzer never executes SQL, never connects to a database and never
mutates the schema or the operations it inspects.  Operations that cannot be
classified from the objects alone are reported as ``unknown`` rather than
silently treated as transaction-safe.

.. seealso::

    :doc:`/transaction_analysis`

        Rule documentation describing how fragments are derived.

"""

from .txboundary import analyze_transaction_boundaries
from .txboundary import Fragment
from .txboundary import OperationItem
from .txboundary import TransactionAnalysis
from .txboundary import TransactionBoundaryOp

__all__ = [
    "Fragment",
    "OperationItem",
    "TransactionAnalysis",
    "TransactionBoundaryOp",
    "analyze_transaction_boundaries",
]
