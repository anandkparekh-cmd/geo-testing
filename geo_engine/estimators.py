"""Counterfactual estimators: predict what the test geos would have done without the test.

Each estimator is fitted on the pre-period only (test geos' total vs control geos) and
then predicts the test geos' total during the test from the controls. The same code is
used by simulate_power (on historical windows with no test) and by estimate_lift (on
the real test), so the power analysis measures the estimator that will be used.

Methods
- "synthetic_control": non-negative weights on each control geo plus an intercept,
  with light ridge shrinkage so the weights spread across similar geos instead of
  over-fitting a few.
- "tbr": time-based regression (Kerman et al., Google, 2017): test total regressed on the
  control total, y = a + b * x. Fewer moving parts, a useful cross-check.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import nnls

METHODS = ("synthetic_control", "tbr")


@dataclass
class Fit:
    method: str
    intercept: float
    weights: np.ndarray            # one per control column (tbr: the same slope for every column)
    pre_fitted: np.ndarray         # in-sample prediction over the pre-period

    def predict(self, X: np.ndarray) -> np.ndarray:
        return self.intercept + X @ self.weights


def fit(method: str, y_pre: np.ndarray, X_pre: np.ndarray, ridge: float = 0.05) -> Fit:
    """y_pre: test-geo total per period (n,). X_pre: control geos (n, k)."""
    if method == "tbr":
        x = X_pre.sum(axis=1)
        b, a = np.polyfit(x, y_pre, 1)
        w = np.full(X_pre.shape[1], b)
        return Fit(method, float(a), w, a + b * x)
    if method == "synthetic_control":
        ym, Xm = y_pre.mean(), X_pre.mean(axis=0)
        yc, Xc = y_pre - ym, X_pre - Xm
        # ridge via augmented rows, scaled to the data so `ridge` is unit-free
        lam = ridge * float(np.mean(np.sum(Xc**2, axis=0)))
        A = np.vstack([Xc, np.sqrt(lam) * np.eye(X_pre.shape[1])])
        bvec = np.concatenate([yc, np.zeros(X_pre.shape[1])])
        w, _ = nnls(A, bvec, maxiter=50 * X_pre.shape[1])
        a = ym - Xm @ w
        return Fit(method, float(a), w, a + X_pre @ w)
    raise ValueError(f"method must be one of {METHODS}")


def lift(y_post: np.ndarray, counterfactual: np.ndarray) -> float:
    """Relative lift of actual over counterfactual across the whole test window."""
    return float(y_post.sum() / counterfactual.sum() - 1)
