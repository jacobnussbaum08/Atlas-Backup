"""Parameter spaces and search methods (Phase 8B).

A :class:`ParameterSpace` lists, for one strategy, the parameters that may vary and the
discrete values each may take (ranges are expanded into explicit steps, so every trial is
an exact, reproducible point), plus cross-parameter constraints. Every point is also
validated by the strategy's own parameter model, which carries the family's invariants
(for example fast EMA < slow EMA); an invalid point is *infeasible*, never a trial.

Search methods (all deterministic for a given seed):

* **GRID** for small spaces: every feasible point. Refused when the feasible grid exceeds the
  trial budget, so an exhaustive search cannot silently explode.
* **RANDOM**: seeded draws without replacement from the feasible points.
* **BAYESIAN**: seeded random starts, then each next point maximises the expected
  improvement of a Gaussian process fitted to the scores so far (scikit-learn,
  ``random_state`` = seed). It is justified only when evaluations are expensive and the
  space has several ordered dimensions; it searches *more efficiently*, which also means it
  overfits the training windows *faster*, so its trials are counted exactly like the others.

The stored (champion) parameters are always trial 0, so every search is compared against
what is already approved.
"""

from __future__ import annotations

import itertools
import math
import warnings
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.learning.enums import SearchMethod

Value = int | str | bool | Decimal


class Dimension(BaseModel):
    """One varying parameter: explicit ``values`` or a ``low``..``high`` range in ``step``s."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    values: tuple[Value, ...] | None = None
    low: Decimal | None = None
    high: Decimal | None = None
    step: Decimal | None = Field(default=None, gt=0)
    integer: bool = False

    @model_validator(mode="after")
    def _shape(self) -> Dimension:
        ranged = (self.low, self.high, self.step)
        if self.values is not None:
            if any(v is not None for v in ranged):
                raise ValueError(f"{self.name}: give values or a range, not both")
            if not self.values or len(set(map(str, self.values))) != len(self.values):
                raise ValueError(f"{self.name}: values must be non-empty and unique")
        else:
            if any(v is None for v in ranged):
                raise ValueError(f"{self.name}: a range needs low, high and step")
            if self.high < self.low:  # type: ignore[operator]  # all three checked above
                raise ValueError(f"{self.name}: high is below low")
            if (self.high - self.low) / self.step > 200:  # type: ignore[operator]
                raise ValueError(f"{self.name}: more than 200 steps")
        return self

    def points(self) -> list[Value]:
        if self.values is not None:
            return list(self.values)
        low, high, step = self.low, self.high, self.step
        if low is None or high is None or step is None:  # validated; keeps the types exact
            return []
        out: list[Value] = []
        v = low
        while v <= high:
            out.append(int(v) if self.integer else v)
            v += step
        return out

    @property
    def ordered(self) -> bool:
        return all(isinstance(v, (int, Decimal)) and not isinstance(v, bool) for v in self.points())


class Constraint(BaseModel):
    """``left op right`` where ``right`` is another parameter's name or a constant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    left: str
    op: Literal["<", "<=", ">", ">=", "!="]
    right: str | Decimal

    def holds(self, point: Mapping[str, Any], base: Mapping[str, Any]) -> bool:
        def value(name: str) -> Any:
            return point[name] if name in point else base.get(name)

        a = value(self.left)
        b = value(self.right) if isinstance(self.right, str) else self.right
        if a is None or b is None:
            return False
        a, b = Decimal(str(a)), Decimal(str(b))
        return {
            "<": a < b,
            "<=": a <= b,
            ">": a > b,
            ">=": a >= b,
            "!=": a != b,
        }[self.op]


class ParameterSpace(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dimensions: tuple[Dimension, ...] = Field(min_length=1, max_length=8)
    constraints: tuple[Constraint, ...] = ()

    @model_validator(mode="after")
    def _names(self) -> ParameterSpace:
        names = [d.name for d in self.dimensions]
        if len(names) != len(set(names)):
            raise ValueError("each parameter may appear once")
        return self

    def grid_size(self) -> int:
        return math.prod(len(d.points()) for d in self.dimensions)

    def grid(self) -> list[dict[str, Value]]:
        names = [d.name for d in self.dimensions]
        return [
            dict(zip(names, combo, strict=True))
            for combo in itertools.product(*(d.points() for d in self.dimensions))
        ]


Validator = Callable[[dict[str, Any]], dict[str, Any]]


def feasible_points(
    space: ParameterSpace, base: Mapping[str, Any], validate: Validator
) -> tuple[list[dict[str, Any]], int]:
    """(feasible points as full effective parameter sets, infeasible count). ``validate``
    returns the strategy's effective parameters as JSON or raises ``ValueError``."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    infeasible = 0
    if space.grid_size() > 50_000:
        raise ValueError(f"the space has {space.grid_size()} points; at most 50,000 are allowed")
    for point in space.grid():
        if not all(c.holds(point, base) for c in space.constraints):
            infeasible += 1
            continue
        try:
            effective = validate({**base, **point})
        except (ValueError, ValidationError):
            infeasible += 1
            continue
        key = repr(sorted(effective.items()))
        if key not in seen:
            seen.add(key)
            out.append(effective)
    return out, infeasible


def _encode(space: ParameterSpace, params: Mapping[str, Any]) -> list[float]:
    """Position of each dimension's value in its ordered point list, scaled to [0, 1]."""
    out: list[float] = []
    for d in space.dimensions:
        pts = [str(p) for p in d.points()]
        value = str(params.get(d.name))
        i = pts.index(value) if value in pts else 0
        out.append(i / max(len(pts) - 1, 1))
    return out


class Searcher:
    """Proposes trials one at a time; the caller scores each and reports it back."""

    def __init__(
        self,
        method: SearchMethod,
        space: ParameterSpace,
        points: list[dict[str, Any]],
        *,
        budget: int,
        seed: int,
        initial_random: int = 5,
    ) -> None:
        if method is SearchMethod.GRID and len(points) > budget:
            raise ValueError(
                f"GRID search would run {len(points)} trials, above the budget of {budget}; "
                "use RANDOM or BAYESIAN search, or a smaller space"
            )
        self.method = method
        self.space = space
        self.points = points
        self.budget = min(budget, len(points))
        self.rng = np.random.default_rng(seed)
        self.seed = seed
        self.initial_random = initial_random
        self.order: list[int] = (
            list(range(len(points)))
            if method is SearchMethod.GRID
            else [int(i) for i in self.rng.permutation(len(points))]
        )
        self.tried: list[int] = []
        self.scores: list[float] = []

    def next(self) -> dict[str, Any] | None:
        if len(self.tried) >= self.budget:
            return None
        if self.method is SearchMethod.BAYESIAN and len(self.tried) >= self.initial_random:
            index = self._expected_improvement()
        else:
            index = next(i for i in self.order if i not in self.tried)
        self.tried.append(index)
        return self.points[index]

    def report(self, score: float | None) -> None:
        # Failed or undefined trials score as the worst seen (they are still counted).
        if score is None or not math.isfinite(score):
            score = min(self.scores, default=0.0) - 1.0
        self.scores.append(score)

    def _expected_improvement(self) -> int:
        from scipy.stats import norm
        from sklearn.exceptions import ConvergenceWarning
        from sklearn.gaussian_process import GaussianProcessRegressor
        from sklearn.gaussian_process.kernels import ConstantKernel, Matern, WhiteKernel

        x = np.array([_encode(self.space, self.points[i]) for i in self.tried])
        y = np.array(self.scores[: len(self.tried)])
        kernel = ConstantKernel(1.0) * Matern(nu=2.5) + WhiteKernel(1e-3)
        gp = GaussianProcessRegressor(kernel=kernel, normalize_y=True, random_state=self.seed)
        with warnings.catch_warnings():
            # Hyperparameter bounds reached on a handful of points are expected; the fit is
            # still deterministic for the seed.
            warnings.simplefilter("ignore", ConvergenceWarning)
            gp.fit(x, y)
        remaining = [i for i in self.order if i not in self.tried]
        pool = np.array([_encode(self.space, self.points[i]) for i in remaining])
        mu, sd = gp.predict(pool, return_std=True)
        best = y.max()
        with np.errstate(divide="ignore", invalid="ignore"):
            z = np.where(sd > 0, (mu - best) / sd, 0.0)
            ei = np.where(sd > 0, (mu - best) * norm.cdf(z) + sd * norm.pdf(z), 0.0)
        return remaining[int(np.argmax(ei))]  # ties: the earliest in the seeded order


def neighbors(
    space: ParameterSpace, selected: Mapping[str, Any], points: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Feasible points one step away from ``selected`` along one ordered dimension."""
    names = [d.name for d in space.dimensions]
    out: list[dict[str, Any]] = []

    def same(p: Mapping[str, Any], wanted: Mapping[str, Any]) -> bool:
        # Effective parameters are JSON (decimals as strings): compare as strings.
        return all(str(p.get(n)) == str(v) for n, v in wanted.items())

    for d in space.dimensions:
        pts = d.points()
        labels = [str(p) for p in pts]
        current = str(selected.get(d.name))
        if current not in labels:
            continue
        i = labels.index(current)
        for j in (i - 1, i + 1):
            if 0 <= j < len(pts):
                wanted = {n: selected.get(n) for n in names} | {d.name: pts[j]}
                match = next((p for p in points if same(p, wanted)), None)
                if match is not None and dict(match) not in out:
                    out.append(dict(match))
    return out


# ---------------------------------------------------------------------- default spaces
def _d(name: str, *values: Value) -> Dimension:
    return Dimension(name=name, values=tuple(values))


DEFAULT_SPACES: dict[str, ParameterSpace] = {
    "momentum": ParameterSpace(
        dimensions=(
            _d("lookback_periods", 10, 20, 40),
            _d("stop_atr_multiple", Decimal("1.5"), Decimal("2.0"), Decimal("3.0")),
            _d("holding_horizon_periods", 5, 10),
        )
    ),
    "trend_following": ParameterSpace(
        dimensions=(
            _d("fast_ema_length", 10, 20, 30),
            _d("slow_ema_length", 50, 100),
            _d("min_adx", Decimal("15"), Decimal("20"), Decimal("25")),
        ),
        constraints=(Constraint(left="fast_ema_length", op="<", right="slow_ema_length"),),
    ),
    "mean_reversion": ParameterSpace(
        dimensions=(
            _d("rsi_oversold", Decimal("25"), Decimal("30"), Decimal("35")),
            _d("bollinger_k", Decimal("1.5"), Decimal("2.0"), Decimal("2.5")),
            _d("stop_atr_multiple", Decimal("1.0"), Decimal("1.5"), Decimal("2.0")),
        )
    ),
    "breakout": ParameterSpace(
        dimensions=(
            _d("channel_length", 10, 20, 40),
            _d("confirmation_buffer_atr", Decimal("0"), Decimal("0.25"), Decimal("0.5")),
            _d("holding_horizon_periods", 5, 10),
        )
    ),
    "volatility": ParameterSpace(
        dimensions=(
            _d("expansion_ratio", Decimal("1.3"), Decimal("1.5"), Decimal("1.8")),
            _d("stop_atr_multiple", Decimal("1.5"), Decimal("2.0"), Decimal("3.0")),
            _d("target_atr_multiple", Decimal("2.0"), Decimal("3.0"), Decimal("4.0")),
        )
    ),
    "event_driven": ParameterSpace(
        dimensions=(
            _d("min_relevance", Decimal("0.5"), Decimal("0.7")),
            _d("min_events", 1, 2),
            _d("stop_atr_multiple", Decimal("1.0"), Decimal("1.5"), Decimal("2.0")),
        )
    ),
}


__all__ = [
    "DEFAULT_SPACES",
    "Constraint",
    "Dimension",
    "ParameterSpace",
    "Searcher",
    "feasible_points",
    "neighbors",
]
