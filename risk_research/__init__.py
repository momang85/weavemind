"""Financial risk research features and future labelled-data admission.

These pure functions neither train a credit/fraud model nor make decisions.
"""

from .features import build
from .readiness import assess

__all__ = ["build", "assess"]
