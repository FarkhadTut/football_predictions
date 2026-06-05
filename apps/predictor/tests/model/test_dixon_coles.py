"""Unit tests for the Dixon-Coles fitter and low-score correction.

Three checks (TEST-001/002/003 in the tech decomp):

* **TEST-001 gradient check**: the analytical ∂/∂vec of the negative
  log-likelihood matches a centred finite-difference reference to
  ≤ 1e-4 on 200 synthetic matches.
* **TEST-002 parameter recovery**: fitting 5000 matches drawn from the
  true generative DC model (20 teams, fixed strengths, seed=42)
  recovers α / δ / γ within tight tolerances.
* **TEST-003 ρ correction**: the four low-score cells move the right
  direction when ρ < 0 and the joint matrix continues to sum to 1.
"""

from __future__ import annotations

from typing import cast

import numpy as np
import pandas as pd
import pytest

from predictor.model.dixon_coles import (
    DixonColesModel,
    _neg_log_lik_and_grad,
    _pack,
    tau_correction,
)

# ---------------------------------------------------------------------------
# Synthetic DC sampler — used by all three tests
# ---------------------------------------------------------------------------


def _score_matrix(lam: float, mu: float, rho: float, max_goals: int) -> np.ndarray:
    """Build a normalized DC joint score-probability matrix.

    Mirrors ``DixonColesModel._score_matrix`` but renormalizes so we can
    sample exact joint scores without tail bias.
    """
    x = np.arange(max_goals)
    from scipy.stats import poisson

    pois_h = poisson.pmf(x, lam)
    pois_a = poisson.pmf(x, mu)
    m = np.outer(pois_h, pois_a)
    m[0, 0] *= 1.0 - lam * mu * rho
    m[1, 0] *= 1.0 + mu * rho
    m[0, 1] *= 1.0 + lam * rho
    m[1, 1] *= 1.0 - rho
    return cast(np.ndarray, m / m.sum())


def _sample_match(
    rng: np.random.Generator,
    lam: float,
    mu: float,
    rho: float,
    max_goals: int = 12,
) -> tuple[int, int]:
    """Sample a joint (home_goals, away_goals) from the DC distribution."""
    m = _score_matrix(lam, mu, rho, max_goals)
    flat = m.flatten()
    idx = rng.choice(flat.size, p=flat)
    return int(idx // max_goals), int(idx % max_goals)


def _simulate_matches(
    *,
    rng: np.random.Generator,
    teams: list[str],
    attack: np.ndarray,
    defence: np.ndarray,
    home_adv: float,
    rho: float,
    n_matches: int,
) -> pd.DataFrame:
    """Generate ``n_matches`` random (home, away) DC matches."""
    n = len(teams)
    home_ids = rng.integers(0, n, size=n_matches)
    away_ids = rng.integers(0, n, size=n_matches)
    # Avoid self-matches by bumping away when collision occurs.
    collide = home_ids == away_ids
    away_ids[collide] = (away_ids[collide] + 1) % n

    rows = []
    base = pd.Timestamp("2024-01-01")
    for i in range(n_matches):
        h, a = int(home_ids[i]), int(away_ids[i])
        lam = float(np.exp(attack[h] + defence[a] + home_adv))
        mu = float(np.exp(attack[a] + defence[h]))
        hg, ag = _sample_match(rng, lam, mu, rho)
        rows.append(
            {
                "home_team": teams[h],
                "away_team": teams[a],
                "home_goals": hg,
                "away_goals": ag,
                "kickoff_utc": base + pd.Timedelta(days=i),
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# TEST-001: analytical gradient vs finite-difference reference
# ---------------------------------------------------------------------------


def test_analytical_gradient_matches_finite_difference() -> None:
    rng = np.random.default_rng(123)
    n_teams = 6
    teams = [f"T{i}" for i in range(n_teams)]
    attack = rng.normal(0, 0.3, size=n_teams)
    defence = rng.normal(0, 0.3, size=n_teams)
    attack -= attack.mean()
    defence -= defence.mean()
    home_adv = 0.25
    rho = -0.08

    matches = _simulate_matches(
        rng=rng,
        teams=teams,
        attack=attack,
        defence=defence,
        home_adv=home_adv,
        rho=rho,
        n_matches=200,
    )
    team_idx = {t: i for i, t in enumerate(teams)}
    home_idx = matches["home_team"].map(team_idx).to_numpy(dtype=np.int64)
    away_idx = matches["away_team"].map(team_idx).to_numpy(dtype=np.int64)
    home_goals = matches["home_goals"].to_numpy(dtype=np.float64)
    away_goals = matches["away_goals"].to_numpy(dtype=np.float64)
    weights = np.ones(len(matches))

    # Perturbed point near the true parameters so the optimum surface is smooth.
    vec_true = _pack(attack, defence, home_adv, rho)
    vec = vec_true + rng.normal(0, 0.05, size=vec_true.shape)

    def f(v: np.ndarray) -> float:
        val, _ = _neg_log_lik_and_grad(
            v,
            home_idx=home_idx,
            away_idx=away_idx,
            home_goals=home_goals,
            away_goals=away_goals,
            weights=weights,
            n_teams=n_teams,
        )
        return val

    _, grad_analytic = _neg_log_lik_and_grad(
        vec,
        home_idx=home_idx,
        away_idx=away_idx,
        home_goals=home_goals,
        away_goals=away_goals,
        weights=weights,
        n_teams=n_teams,
    )

    eps = 1e-5
    grad_numeric = np.zeros_like(vec)
    for k in range(len(vec)):
        v_plus = vec.copy()
        v_minus = vec.copy()
        v_plus[k] += eps
        v_minus[k] -= eps
        grad_numeric[k] = (f(v_plus) - f(v_minus)) / (2 * eps)

    max_abs_diff = float(np.max(np.abs(grad_analytic - grad_numeric)))
    assert max_abs_diff < 1e-4, f"gradient mismatch: max |Δ| = {max_abs_diff:.3e}"


# ---------------------------------------------------------------------------
# TEST-002: parameter recovery on 5000 synthetic matches
# ---------------------------------------------------------------------------


def test_fit_recovers_synthetic_parameters() -> None:
    rng = np.random.default_rng(42)
    n_teams = 20
    teams = [f"T{i:02d}" for i in range(n_teams)]
    # True strengths drawn once and centred.
    attack_true = rng.normal(0, 0.35, size=n_teams)
    defence_true = rng.normal(0, 0.30, size=n_teams)
    attack_true -= attack_true.mean()
    defence_true -= defence_true.mean()
    home_adv_true = 0.28
    rho_true = -0.10

    matches = _simulate_matches(
        rng=rng,
        teams=teams,
        attack=attack_true,
        defence=defence_true,
        home_adv=home_adv_true,
        rho=rho_true,
        n_matches=5000,
    )

    model = DixonColesModel().fit(matches)
    p = model.params
    assert p is not None

    # Re-index recovered params to the true-team order.
    rec_attack = np.array([p.attack_for(t) for t in teams])
    rec_defence = np.array([p.defence_for(t) for t in teams])

    # Strengths are centred in both fitter and ground truth, so direct
    # comparison is valid. Use a max-deviation tolerance — far stricter
    # than a global RMSE — that 5000 matches comfortably supports.
    assert np.max(np.abs(rec_attack - attack_true)) < 0.15
    assert np.max(np.abs(rec_defence - defence_true)) < 0.15
    assert abs(p.home_adv - home_adv_true) < 0.05
    assert abs(p.rho - rho_true) < 0.05


# ---------------------------------------------------------------------------
# TEST-003: ρ correction shifts the four low-score cells the right way
# ---------------------------------------------------------------------------


def test_rho_correction_shifts_low_score_cells() -> None:
    lam, mu = 1.4, 1.1
    rho = -0.10
    max_goals = 10

    # Independent-Poisson reference (ρ = 0) — same code path but with ρ = 0.
    base = DixonColesModel._score_matrix(lam, mu, 0.0, max_goals)
    corrected = DixonColesModel._score_matrix(lam, mu, rho, max_goals)

    # Direction checks (ρ < 0 under canonical DC convention τ(0,0)=1−λμρ etc.):
    # the correction amplifies low-score draws (which independent Poisson
    # under-predicts in real football) and suppresses narrow 1-0 / 0-1 wins.
    # NB: the tech-decomp spec text for TEST-003 stated the opposite direction;
    # the canonical DC sign convention (and Dixon & Coles 1997 empirical fit
    # ρ ≈ −0.07) is what the implementation follows.
    assert corrected[0, 0] > base[0, 0]
    assert corrected[1, 1] > base[1, 1]
    assert corrected[1, 0] < base[1, 0]
    assert corrected[0, 1] < base[0, 1]

    # Cells outside {0,1}² are untouched.
    np.testing.assert_allclose(corrected[2:, :], base[2:, :])
    np.testing.assert_allclose(corrected[:, 2:], base[:, 2:])

    # Sanity: τ-weighted joint over a wide grid sums to ~1.
    wide = DixonColesModel._score_matrix(lam, mu, rho, 20)
    assert abs(wide.sum() - 1.0) < 1e-6

    # Sanity: τ evaluated by the public helper agrees with what the matrix used.
    assert tau_correction(0, 0, lam, mu, rho) == pytest.approx(1.0 - lam * mu * rho)
    assert tau_correction(1, 0, lam, mu, rho) == pytest.approx(1.0 + mu * rho)
    assert tau_correction(0, 1, lam, mu, rho) == pytest.approx(1.0 + lam * rho)
    assert tau_correction(1, 1, lam, mu, rho) == pytest.approx(1.0 - rho)
