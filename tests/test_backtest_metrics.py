"""
Tests for backtest metric helpers (max_drawdown, annualized_sharpe).
Uses hand-calculated inputs so expected values are unambiguous.
"""

import math

import numpy as np
import pandas as pd
import pytest

from backtest import annualized_sharpe, max_drawdown


# --------------------------------------------------------------------------- #
# max_drawdown                                                                #
# --------------------------------------------------------------------------- #
class TestMaxDrawdown:
    def test_empty_series_returns_zero(self):
        assert max_drawdown(pd.Series(dtype=float)) == 0.0

    def test_monotonically_increasing_has_zero_drawdown(self):
        s = pd.Series([100, 105, 110, 115, 120], dtype=float)
        assert max_drawdown(s) == pytest.approx(0.0)

    def test_simple_drawdown(self):
        # Peak 100 → trough 75 = 25% drawdown
        s = pd.Series([100, 90, 75, 80, 85], dtype=float)
        assert max_drawdown(s) == pytest.approx(0.25)

    def test_picks_worst_of_multiple_drawdowns(self):
        # First DD: 100→90 (10%). Recovery to 120. Second DD: 120→90 (25%).
        s = pd.Series([100, 90, 100, 110, 120, 110, 90, 100], dtype=float)
        assert max_drawdown(s) == pytest.approx(0.25)

    def test_handles_all_same_values(self):
        assert max_drawdown(pd.Series([50, 50, 50], dtype=float)) == 0.0


# --------------------------------------------------------------------------- #
# annualized_sharpe                                                           #
# --------------------------------------------------------------------------- #
class TestSharpe:
    def test_too_short_series_returns_zero(self):
        assert annualized_sharpe(pd.Series([100.0])) == 0.0
        assert annualized_sharpe(pd.Series([], dtype=float)) == 0.0

    def test_constant_equity_returns_zero(self):
        # Zero variance → Sharpe undefined; helper returns 0.0
        s = pd.Series([100.0] * 50)
        assert annualized_sharpe(s) == 0.0

    def test_positive_drift_gives_positive_sharpe(self):
        # Construct equity as compounding daily returns of +0.1%
        rng = np.random.default_rng(0)
        daily_rets = 0.001 + 0.0 * rng.standard_normal(252)
        equity = pd.Series(100 * (1 + pd.Series(daily_rets)).cumprod())
        # With zero volatility, this is actually 0 variance → 0.
        # So add tiny noise:
        noisy_rets = 0.001 + 0.005 * rng.standard_normal(252)
        equity = pd.Series(100 * (1 + pd.Series(noisy_rets)).cumprod())
        sharpe = annualized_sharpe(equity, risk_free=0.0)
        assert sharpe > 0

    def test_negative_drift_gives_negative_sharpe(self):
        rng = np.random.default_rng(1)
        noisy_rets = -0.001 + 0.005 * rng.standard_normal(252)
        equity = pd.Series(100 * (1 + pd.Series(noisy_rets)).cumprod())
        sharpe = annualized_sharpe(equity, risk_free=0.0)
        assert sharpe < 0

    def test_sharpe_matches_manual_calculation(self):
        # Known inputs: returns alternating +1% and -0.5% for 252 periods
        rets = pd.Series([0.01, -0.005] * 126)
        equity = pd.Series(100 * (1 + rets).cumprod())
        # Manual reference: mean ~0.0025, std of ~0.0075, no RF
        # annualized = 0.0025 / 0.0075 * sqrt(252) ≈ 5.29
        sharpe = annualized_sharpe(equity, risk_free=0.0)
        # Loose bound — we just want to confirm the formula shape is right
        assert 4.0 < sharpe < 7.0
