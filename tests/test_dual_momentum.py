"""
Tests for dual_momentum.py — pure logic for trailing returns and target weights.
"""

import numpy as np
import pandas as pd
import pytest

from dual_momentum import (
    is_month_end,
    select_dual_momentum_targets,
    trailing_returns,
)


def _bars(closes, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame({"close": closes}, index=idx)


# --------------------------------------------------------------------------- #
# trailing_returns                                                            #
# --------------------------------------------------------------------------- #
class TestTrailingReturns:
    def test_basic_return_calculation(self):
        bars = _bars(np.linspace(100.0, 200.0, 300))
        out = trailing_returns({"X": bars}, bars.index[-1], lookback_days=252)
        # 252 bars before the last index → close goes from 100*(1 + 252/299*1.0)
        # to last value. We just verify the result is a positive return ~0.7-0.9.
        assert "X" in out
        assert 0.5 < out["X"] < 1.5

    def test_skips_symbol_with_insufficient_history(self):
        # Only 100 bars but ask for 252-day lookback
        bars = _bars(np.linspace(100.0, 200.0, 100))
        out = trailing_returns({"SHORT": bars}, bars.index[-1], lookback_days=252)
        assert out == {}

    def test_skips_symbol_with_no_data_at_as_of(self):
        bars = _bars(np.linspace(100.0, 200.0, 300))
        # Use a date NOT in the bars index
        out = trailing_returns({"X": bars}, pd.Timestamp("2030-01-01"),
                                lookback_days=252)
        assert out == {}

    def test_negative_return_for_declining_series(self):
        bars = _bars(np.linspace(200.0, 100.0, 300))
        out = trailing_returns({"DOWN": bars}, bars.index[-1], lookback_days=252)
        assert out["DOWN"] < 0


# --------------------------------------------------------------------------- #
# select_dual_momentum_targets                                                #
# --------------------------------------------------------------------------- #
class TestSelectTargets:
    UNIVERSE = ["A", "B", "C", "D", "E"]
    SAFE = "AGG"

    def test_top_n_with_all_positive(self):
        returns = {"A": 0.5, "B": 0.3, "C": 0.1, "D": -0.1, "E": -0.5}
        weights = select_dual_momentum_targets(
            returns, self.UNIVERSE, self.SAFE, top_n=3, threshold=0.0,
        )
        # Top 3 are A, B, C; all > 0 → all qualify; equal weights of 1/3
        assert weights == {"A": pytest.approx(1/3), "B": pytest.approx(1/3),
                           "C": pytest.approx(1/3)}

    def test_safe_asset_fills_failed_slots(self):
        # Top 3 = A, B, C — but C's return is negative, so its slot rotates to safe
        returns = {"A": 0.5, "B": 0.3, "C": -0.1, "D": -0.2, "E": -0.5}
        weights = select_dual_momentum_targets(
            returns, self.UNIVERSE, self.SAFE, top_n=3, threshold=0.0,
        )
        assert weights == {"A": pytest.approx(1/3), "B": pytest.approx(1/3),
                           "AGG": pytest.approx(1/3)}

    def test_all_safe_when_nothing_qualifies(self):
        returns = {s: -0.1 for s in self.UNIVERSE}
        weights = select_dual_momentum_targets(
            returns, self.UNIVERSE, self.SAFE, top_n=3, threshold=0.0,
        )
        assert weights == {"AGG": pytest.approx(1.0)}

    def test_all_safe_when_universe_empty(self):
        weights = select_dual_momentum_targets(
            {}, self.UNIVERSE, self.SAFE, top_n=3, threshold=0.0,
        )
        assert weights == {"AGG": 1.0}

    def test_threshold_above_zero(self):
        # Threshold = 4% (T-bill style) — only A's 50% and B's 5% pass; C's 3% fails
        returns = {"A": 0.50, "B": 0.05, "C": 0.03}
        weights = select_dual_momentum_targets(
            returns, ["A", "B", "C"], self.SAFE, top_n=3, threshold=0.04,
        )
        assert weights == {"A": pytest.approx(1/3), "B": pytest.approx(1/3),
                           "AGG": pytest.approx(1/3)}

    def test_weights_sum_to_one(self):
        # Property: weights must always sum to 1.0 regardless of qualifications
        for return_set in [
            {"A": 0.5, "B": 0.3, "C": 0.1},
            {"A": 0.5, "B": -0.1},
            {"A": -0.1, "B": -0.2, "C": -0.3},
            {},
        ]:
            weights = select_dual_momentum_targets(
                return_set, ["A", "B", "C"], self.SAFE,
                top_n=3, threshold=0.0,
            )
            assert sum(weights.values()) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# is_month_end                                                                #
# --------------------------------------------------------------------------- #
class TestIsMonthEnd:
    def test_last_trading_day_of_month(self):
        # Jan 30 → Feb 1 transition; index 1 is Jan 30 (last of January)
        dates = [
            pd.Timestamp("2024-01-29"),
            pd.Timestamp("2024-01-30"),
            pd.Timestamp("2024-02-01"),
        ]
        assert not is_month_end(dates[0], dates, 0)  # Jan 29 → Jan 30
        assert is_month_end(dates[1], dates, 1)      # Jan 30 → Feb 1 (month change)
        assert is_month_end(dates[2], dates, 2)      # last index = end of series

    def test_handles_skipped_days(self):
        # Markets skip weekends, so "last trading day of month" can be e.g. the 28th
        dates = [
            pd.Timestamp("2024-02-26"),
            pd.Timestamp("2024-02-27"),
            pd.Timestamp("2024-02-28"),
            pd.Timestamp("2024-02-29"),
            pd.Timestamp("2024-03-01"),
        ]
        assert is_month_end(dates[3], dates, 3)
        assert not is_month_end(dates[2], dates, 2)
