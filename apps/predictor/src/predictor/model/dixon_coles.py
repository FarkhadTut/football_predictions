"""Dixon-Coles bivariate-Poisson football outcome model.

Dixon & Coles (1997) refine the independent-Poisson goal model with two
ideas we adopt verbatim:

1. **Team strengths via log-rates.** For a match where team ``h`` hosts
   team ``a``::

        log λ_home = α_h + δ_a + γ
        log λ_away = α_a + δ_h

   where ``α`` is attack strength, ``δ`` is defence weakness (higher =
   concedes more), and ``γ`` is a global home-advantage. We impose the
   identifying constraint ``mean(α) = mean(δ) = 0`` post-fit (the
   optimizer holds ``α_0 = 0`` to break the rank-1 degeneracy).

2. **Low-score correction** ``τ(x, y; λ, μ, ρ)`` that adjusts the four
   joint cells where independence is empirically wrong::

        τ(0,0) = 1 − λμρ
        τ(1,0) = 1 + μρ
        τ(0,1) = 1 + λρ
        τ(1,1) = 1 − ρ
        τ(x,y) = 1                                 (otherwise)

   The bivariate PMF becomes ``τ · Pois(x;λ) · Pois(y;μ)``. ``ρ`` is
   shared across matches; in practice ρ ∈ (-0.2, 0.0).

**Time weighting** (also from DC): each match contributes weight
``2 ** (-Δdays / half_life_days)`` to the log-likelihood, so old matches
fade gracefully. ``half_life_days=None`` disables decay (uniform weights).

**Fitting.** Negative log-likelihood is minimized via ``scipy.optimize``
L-BFGS-B with an analytical gradient (see ``_neg_log_lik_and_grad``).
The parameter vector is ``[α_1..α_{N-1}, δ_0..δ_{N-1}, γ, ρ]`` —
``α_0`` is held at 0 during optimization and the strengths are
re-centered to mean zero after convergence (``γ`` absorbs the offset
so log-rates are preserved).

**Prediction.** ``predict(home, away)`` returns a ``max_goals × max_goals``
matrix of joint score probabilities. Marginals for 1X2 / O-U / BTTS /
corners are derived downstream in ``predictor.model.markets``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import gammaln

__all__ = [
    "DixonColesModel",
    "DixonColesParams",
    "tau_correction",
]


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DixonColesParams:
    """Fitted Dixon-Coles parameters with mean-zero α / δ identifiability."""

    teams: tuple[str, ...]
    attack: np.ndarray  # shape (n_teams,), mean ≈ 0
    defence: np.ndarray  # shape (n_teams,), mean ≈ 0
    home_adv: float
    rho: float

    def attack_for(self, team: str) -> float:
        return float(self.attack[self.teams.index(team)])

    def defence_for(self, team: str) -> float:
        return float(self.defence[self.teams.index(team)])


# ---------------------------------------------------------------------------
# Core math (vectorized, used by both likelihood + prediction)
# ---------------------------------------------------------------------------


def tau_correction(
    x: np.ndarray | int,
    y: np.ndarray | int,
    lam: np.ndarray | float,
    mu: np.ndarray | float,
    rho: float,
) -> np.ndarray:
    """Dixon-Coles low-score correction τ(x, y; λ, μ, ρ).

    Broadcasts over arrays. Returns 1.0 outside the {0,1}×{0,1} block.
    """
    x_arr = np.asarray(x)
    y_arr = np.asarray(y)
    lam_arr = np.asarray(lam, dtype=float)
    mu_arr = np.asarray(mu, dtype=float)
    out = np.ones(np.broadcast_shapes(x_arr.shape, y_arr.shape, lam_arr.shape, mu_arr.shape))
    out = np.where((x_arr == 0) & (y_arr == 0), 1.0 - lam_arr * mu_arr * rho, out)
    out = np.where((x_arr == 1) & (y_arr == 0), 1.0 + mu_arr * rho, out)
    out = np.where((x_arr == 0) & (y_arr == 1), 1.0 + lam_arr * rho, out)
    out = np.where((x_arr == 1) & (y_arr == 1), 1.0 - rho, out)
    return out


def _log_poisson_pmf(k: np.ndarray, lam: np.ndarray) -> np.ndarray:
    """log P(K = k | λ) = k log λ − λ − log k! ."""
    return cast(np.ndarray, k * np.log(lam) - lam - gammaln(k + 1.0))


# ---------------------------------------------------------------------------
# Internal: parameter vector packing / unpacking
# ---------------------------------------------------------------------------


def _unpack(vec: np.ndarray, n_teams: int) -> tuple[np.ndarray, np.ndarray, float, float]:
    """Unpack the L-BFGS-B parameter vector. α_0 is held at 0."""
    n_alpha_free = n_teams - 1
    attack = np.empty(n_teams)
    attack[0] = 0.0
    attack[1:] = vec[:n_alpha_free]
    defence = vec[n_alpha_free : n_alpha_free + n_teams]
    home_adv = float(vec[-2])
    rho = float(vec[-1])
    return attack, defence, home_adv, rho


def _pack(attack: np.ndarray, defence: np.ndarray, home_adv: float, rho: float) -> np.ndarray:
    """Inverse of ``_unpack``: drop ``α_0`` and concatenate."""
    return np.concatenate([attack[1:], defence, [home_adv, rho]])


# ---------------------------------------------------------------------------
# Internal: negative log-likelihood + analytical gradient
# ---------------------------------------------------------------------------


def _neg_log_lik_and_grad(
    vec: np.ndarray,
    *,
    home_idx: np.ndarray,
    away_idx: np.ndarray,
    home_goals: np.ndarray,
    away_goals: np.ndarray,
    weights: np.ndarray,
    n_teams: int,
    ridge: float = 0.0,
) -> tuple[float, np.ndarray]:
    """Negative weighted log-likelihood and its gradient w.r.t. ``vec``.

    ``ridge`` adds an L2 penalty ``0.5·ridge·(Σα² + Σδ²)`` on the team
    attack/defence strengths (not γ or ρ), shrinking them toward the
    league-average team. This regularises the fit on the small per-team
    samples typical of international tournaments, where unpenalised MLE
    is badly overconfident.

    Derivation
    ----------
    For each match i:

        log λ_h,i = α_{h_i} + δ_{a_i} + γ
        log λ_a,i = α_{a_i} + δ_{h_i}
        log f_i   = log τ_i + log Pois(x_i; λ_h,i) + log Pois(y_i; λ_a,i)

    The Poisson term's derivative w.r.t. any parameter ``θ`` that
    enters only through ``λ`` is ``(x/λ − 1) · ∂λ/∂θ``. Since
    ``∂λ/∂α_k = λ`` when ``k`` indexes that match's home (or away)
    team, the contribution simplifies to ``(x − λ)`` per match.

    ``τ`` only depends on parameters via λ and μ for the four low-score
    cells; the ``∂log τ/∂λ`` and ``∂log τ/∂μ`` factors are precomputed
    once per match and multiplied through.
    """
    attack, defence, home_adv, rho = _unpack(vec, n_teams)

    lam = np.exp(attack[home_idx] + defence[away_idx] + home_adv)
    mu = np.exp(attack[away_idx] + defence[home_idx])

    log_pois_h = _log_poisson_pmf(home_goals, lam)
    log_pois_a = _log_poisson_pmf(away_goals, mu)

    tau = tau_correction(home_goals, away_goals, lam, mu, rho)
    if np.any(tau <= 0):
        # Optimizer wandered into infeasible region; return +inf + zero
        # gradient so L-BFGS-B backs off.
        return float("inf"), np.zeros_like(vec)

    log_lik_per = weights * (np.log(tau) + log_pois_h + log_pois_a)
    neg_log_lik = -float(np.sum(log_lik_per))

    # L2 (ridge) penalty on team strengths. α_0 is held at 0, so it is
    # excluded automatically; δ is penalised in full.
    if ridge > 0.0:
        neg_log_lik += 0.5 * ridge * float(np.sum(attack**2) + np.sum(defence**2))

    # --- Gradient ---
    # Per-match partials of Poisson piece w.r.t. λ and μ (× λ or μ since
    # log-rate is the actual parameter): ∂/∂(logλ) log Pois(x;λ) = x − λ.
    d_pois_d_logl = home_goals - lam  # shape (n_matches,)
    d_pois_d_logm = away_goals - mu

    # Per-match partials of log τ w.r.t. λ and μ. Zero outside low-score block.
    d_logtau_d_logl = np.zeros_like(lam)
    d_logtau_d_logm = np.zeros_like(mu)
    d_logtau_d_rho = np.zeros_like(lam)

    m00 = (home_goals == 0) & (away_goals == 0)
    m10 = (home_goals == 1) & (away_goals == 0)
    m01 = (home_goals == 0) & (away_goals == 1)
    m11 = (home_goals == 1) & (away_goals == 1)

    # τ(0,0) = 1 - λμρ. ∂log τ/∂(logλ) = -λμρ / (1-λμρ); same for logμ.
    denom_00 = 1.0 - lam * mu * rho
    d_logtau_d_logl[m00] = (-lam[m00] * mu[m00] * rho) / denom_00[m00]
    d_logtau_d_logm[m00] = (-lam[m00] * mu[m00] * rho) / denom_00[m00]
    d_logtau_d_rho[m00] = (-lam[m00] * mu[m00]) / denom_00[m00]

    # τ(1,0) = 1 + μρ. ∂log τ/∂(logμ) = μρ/(1+μρ); ∂/∂(logλ) = 0.
    denom_10 = 1.0 + mu * rho
    d_logtau_d_logm[m10] = (mu[m10] * rho) / denom_10[m10]
    d_logtau_d_rho[m10] = mu[m10] / denom_10[m10]

    # τ(0,1) = 1 + λρ.
    denom_01 = 1.0 + lam * rho
    d_logtau_d_logl[m01] = (lam[m01] * rho) / denom_01[m01]
    d_logtau_d_rho[m01] = lam[m01] / denom_01[m01]

    # τ(1,1) = 1 - ρ. Independent of λ, μ.
    d_logtau_d_rho[m11] = -1.0 / (1.0 - rho)

    # Combined per-match partials w.r.t. log λ_h and log λ_a:
    g_logl = weights * (d_pois_d_logl + d_logtau_d_logl)
    g_logm = weights * (d_pois_d_logm + d_logtau_d_logm)
    g_rho = float(np.sum(weights * d_logtau_d_rho))

    # Now propagate to (α, δ, γ).
    #   log λ_h depends on α_{h_i}, δ_{a_i}, γ
    #   log λ_a depends on α_{a_i}, δ_{h_i}
    grad_attack = np.zeros(n_teams)
    grad_defence = np.zeros(n_teams)
    np.add.at(grad_attack, home_idx, g_logl)
    np.add.at(grad_attack, away_idx, g_logm)
    np.add.at(grad_defence, away_idx, g_logl)
    np.add.at(grad_defence, home_idx, g_logm)
    grad_home_adv = float(np.sum(g_logl))

    # Negate (we minimize -log L) and drop α_0 (held fixed).
    grad_attack_vec = -grad_attack[1:]
    grad_defence_vec = -grad_defence
    if ridge > 0.0:
        # ∂/∂θ [0.5·ridge·θ²] = ridge·θ, added to the (negated) NLL gradient.
        grad_attack_vec = grad_attack_vec + ridge * attack[1:]
        grad_defence_vec = grad_defence_vec + ridge * defence
    grad_vec = np.concatenate([grad_attack_vec, grad_defence_vec, [-grad_home_adv, -g_rho]])
    return neg_log_lik, grad_vec


# ---------------------------------------------------------------------------
# Public model
# ---------------------------------------------------------------------------


REQUIRED_COLUMNS = ("home_team", "away_team", "home_goals", "away_goals", "kickoff_utc")


class DixonColesModel:
    """Fittable Dixon-Coles model.

    Parameters
    ----------
    half_life_days : float or None
        Exponential decay half-life for time weighting (``None`` →
        uniform weights). Dixon & Coles report ~half-season half-lives
        for in-season prediction; we leave the choice to the caller.
    rho_bounds : tuple[float, float]
        Box constraints for ρ. Defaults to (-0.2, 0.2) which is wide
        enough for any plausible football dataset and keeps τ > 0 even
        when λμ ≈ 5.
    neutral_venue : bool
        When ``True``, the home-advantage term γ is fixed at 0 and not
        estimated, so ``λ_home = exp(α_home + δ_away)`` is symmetric with
        ``λ_away``. Use for international tournaments, which are played at
        neutral venues — applying a club-style home edge to an arbitrary
        "home" designation is a systematic miscalibration.
    """

    def __init__(
        self,
        *,
        half_life_days: float | None = None,
        rho_bounds: tuple[float, float] = (-0.2, 0.2),
        neutral_venue: bool = False,
    ) -> None:
        self.half_life_days = half_life_days
        self.rho_bounds = rho_bounds
        self.neutral_venue = neutral_venue
        self.params: DixonColesParams | None = None
        self._team_idx: dict[str, int] = {}
        self._opt_result: object | None = None

    # ---- public API ----

    def fit(
        self,
        matches: pd.DataFrame,
        *,
        as_of: datetime | None = None,
        max_iter: int = 500,
        tol: float = 1e-8,
        ridge: float = 0.0,
    ) -> DixonColesModel:
        """Fit the model on a match dataframe.

        ``matches`` must contain ``home_team, away_team, home_goals,
        away_goals, kickoff_utc``. Time weighting is computed relative
        to ``as_of`` (defaults to the latest kickoff in the dataset).
        ``ridge`` is the L2 shrinkage strength on team strengths (0 = MLE).
        """
        missing = [c for c in REQUIRED_COLUMNS if c not in matches.columns]
        if missing:
            raise ValueError(f"matches missing required columns: {missing}")
        if len(matches) == 0:
            raise ValueError("matches is empty")

        teams = tuple(sorted(set(matches["home_team"]) | set(matches["away_team"])))
        n_teams = len(teams)
        if n_teams < 2:
            raise ValueError("need at least 2 distinct teams to fit")
        team_idx = {t: i for i, t in enumerate(teams)}

        home_idx = matches["home_team"].map(team_idx).to_numpy(dtype=np.int64)
        away_idx = matches["away_team"].map(team_idx).to_numpy(dtype=np.int64)
        home_goals = matches["home_goals"].to_numpy(dtype=np.float64)
        away_goals = matches["away_goals"].to_numpy(dtype=np.float64)

        weights = self._compute_weights(matches["kickoff_utc"], as_of)

        # Starting point: zero strengths, modest home advantage, no rho.
        # At a neutral venue γ is fixed at 0 (frozen via equal bounds) so the
        # nominal home/away labels carry no advantage.
        x0 = np.zeros(2 * n_teams + 1)
        x0[-2] = 0.0 if self.neutral_venue else 0.25  # γ ≈ log(1.28) home edge
        x0[-1] = 0.0

        home_adv_bound: tuple[float | None, float | None] = (
            (0.0, 0.0) if self.neutral_venue else (-2.0, 2.0)
        )
        bounds: list[tuple[float | None, float | None]] = [(None, None)] * (2 * n_teams - 1) + [
            home_adv_bound,
            self.rho_bounds,
        ]

        # scipy.minimize doesn't take kwargs=; capture the per-match arrays
        # in a closure so the optimizer sees a single positional argument.
        def objective(v: np.ndarray) -> tuple[float, np.ndarray]:
            return _neg_log_lik_and_grad(
                v,
                home_idx=home_idx,
                away_idx=away_idx,
                home_goals=home_goals,
                away_goals=away_goals,
                weights=weights,
                n_teams=n_teams,
                ridge=ridge,
            )

        result = minimize(
            objective,
            x0,
            jac=True,
            method="L-BFGS-B",
            bounds=bounds,
            options={"maxiter": max_iter, "ftol": tol, "gtol": tol},
        )
        self._opt_result = result
        attack, defence, home_adv, rho = _unpack(result.x, n_teams)

        if self.neutral_venue:
            # No γ to absorb a shift into; use the gauge (α → α − c, δ → δ + c),
            # which leaves every λ = α_i + δ_j invariant, to set mean(α) = 0.
            # γ stays exactly 0.
            c = float(np.mean(attack))
            attack = attack - c
            defence = defence + c
            home_adv = 0.0
        else:
            # Re-center to mean(α) = mean(δ) = 0, absorbing the shift into γ.
            # log λ is invariant under (α → α + c_α, δ → δ + c_δ, γ → γ − c_α − c_δ).
            c_alpha = float(np.mean(attack))
            c_delta = float(np.mean(defence))
            attack = attack - c_alpha
            defence = defence - c_delta
            home_adv = home_adv + c_alpha + c_delta

        self.params = DixonColesParams(
            teams=teams,
            attack=attack,
            defence=defence,
            home_adv=home_adv,
            rho=rho,
        )
        self._team_idx = team_idx
        return self

    def predict_lambdas(self, home: str, away: str) -> tuple[float, float]:
        """Return ``(λ_home, λ_away)`` for a fitted model."""
        p = self._require_params()
        ih, ia = p.teams.index(home), p.teams.index(away)
        lam = float(np.exp(p.attack[ih] + p.defence[ia] + p.home_adv))
        mu = float(np.exp(p.attack[ia] + p.defence[ih]))
        return lam, mu

    def predict(self, home: str, away: str, max_goals: int = 10) -> np.ndarray:
        """Joint score-probability matrix of shape ``(max_goals, max_goals)``.

        Cell ``[x, y]`` is ``P(home_goals=x, away_goals=y)`` under the
        fitted DC model. The matrix is truncated to ``max_goals`` per
        side, so it does not exactly sum to 1.0 (tail mass is dropped);
        callers that need a normalized matrix should renormalize.
        """
        p = self._require_params()
        lam, mu = self.predict_lambdas(home, away)
        return self._score_matrix(lam, mu, p.rho, max_goals)

    # ---- helpers (also used by tests for fine-grained checks) ----

    @staticmethod
    def _score_matrix(lam: float, mu: float, rho: float, max_goals: int) -> np.ndarray:
        x = np.arange(max_goals)
        pois_h = np.exp(_log_poisson_pmf(x.astype(float), np.full_like(x, lam, dtype=float)))
        pois_a = np.exp(_log_poisson_pmf(x.astype(float), np.full_like(x, mu, dtype=float)))
        m = np.outer(pois_h, pois_a)
        # Apply τ to the four low-score cells.
        if max_goals >= 1:
            m[0, 0] *= 1.0 - lam * mu * rho
        if max_goals >= 2:
            m[1, 0] *= 1.0 + mu * rho
            m[0, 1] *= 1.0 + lam * rho
            m[1, 1] *= 1.0 - rho
        return m

    def _compute_weights(self, kickoffs: pd.Series, as_of: datetime | None) -> np.ndarray:
        if self.half_life_days is None:
            return np.ones(len(kickoffs))
        ts = pd.to_datetime(kickoffs)
        ref = pd.Timestamp(as_of) if as_of is not None else ts.max()
        days_ago = (ref - ts).dt.total_seconds().to_numpy() / 86400.0
        return cast(np.ndarray, np.power(2.0, -days_ago / self.half_life_days))

    def _require_params(self) -> DixonColesParams:
        if self.params is None:
            raise RuntimeError("model has not been fitted; call .fit(matches) first")
        return self.params
