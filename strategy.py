"""
strategy.py
-----------
Trend-following pullback strategy with trend-strength and momentum confirmation.

Entry logic (long only)::

    TREND     : close > SMA(200) AND ADX(14) > 20
    PULLBACK  : RSI(14) < RSI_BUY_THRESHOLD (42)
                AND abs(close - EMA(20)) / EMA(20) <= EMA_PROXIMITY_PCT (0.5%)
                AND MACD histogram rising (momentum turning back up)

All conditions must be true on the most recent *closed* bar for a BUY signal.

Indicators are computed with ``pandas-ta`` when it is installed, otherwise with
mathematically identical pure-pandas implementations (Wilder's smoothing for
RSI), so the module never hard-fails on a missing optional dependency.
"""

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

import config
from logger import get_logger

log = get_logger(__name__)

# pandas-ta is optional: fall back to internal implementations if unavailable
# or incompatible with the installed numpy/pandas versions.
try:  # pragma: no cover - environment dependent
    import pandas_ta as ta  # noqa: F401
    _HAS_PANDAS_TA = True
except Exception:  # noqa: BLE001
    ta = None
    _HAS_PANDAS_TA = False
    log.debug("pandas-ta unavailable; using built-in pandas indicator implementations.")


# --------------------------------------------------------------------------- #
# Indicator primitives
# --------------------------------------------------------------------------- #

def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average (standard 2/(n+1) smoothing)."""
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int) -> pd.Series:
    """Relative Strength Index using Wilder's smoothing (RMA)."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    out = out.where(avg_loss != 0.0, 100.0)
    out = out.where(~((avg_gain == 0.0) & (avg_loss != 0.0)), 0.0)
    return out


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index — measures trend strength (not direction)."""
    high, low, close = df["high"], df["low"], df["close"]
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where(plus_dm > 0, 0.0)
    minus_dm = minus_dm.where(minus_dm > 0, 0.0)
    plus_dm = plus_dm.where(plus_dm > minus_dm, 0.0)
    minus_dm = minus_dm.where(minus_dm > plus_dm, 0.0)
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr)
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def macd_histogram(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    """MACD histogram: MACD line minus its signal line. Rising = momentum turning up."""
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line - signal_line


class TechnicalStrategy:
    """SMA-200 + ADX trend filter, EMA-20 / RSI-14 / MACD pullback entry."""

    def __init__(
        self,
        sma_period: Optional[int] = None,
        ema_period: Optional[int] = None,
        rsi_period: Optional[int] = None,
        rsi_buy_threshold: Optional[float] = None,
        ema_proximity_pct: Optional[float] = None,
        adx_period: int = 14,
        adx_threshold: float = 20.0,
    ) -> None:
        self.sma_period = int(sma_period or config.SMA_PERIOD)
        self.ema_period = int(ema_period or config.EMA_PERIOD)
        self.rsi_period = int(rsi_period or config.RSI_PERIOD)
        self.rsi_buy_threshold = float(
            rsi_buy_threshold if rsi_buy_threshold is not None else config.RSI_BUY_THRESHOLD
        )
        self.ema_proximity_pct = float(
            ema_proximity_pct if ema_proximity_pct is not None else config.EMA_PROXIMITY_PCT
        )
        self.adx_period = int(adx_period)
        self.adx_threshold = float(adx_threshold)

    # ------------------------------------------------------------------ #
    @property
    def min_bars_required(self) -> int:
        """Minimum number of bars needed before any signal can be produced."""
        return max(self.sma_period, self.ema_period, self.rsi_period, 35) + 1

    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Return a copy of ``df`` with ``SMA_<n>``, ``EMA_<n>``, ``RSI_<n>``,
        ``adx`` and ``macd_hist`` columns appended (plus generic aliases
        ``sma``/``ema``/``rsi``).
        """
        if df is None or df.empty or "close" not in df.columns:
            return pd.DataFrame(columns=list(getattr(df, "columns", [])) or ["close"])

        out = df.copy().reset_index(drop=True)
        close = pd.to_numeric(out["close"], errors="coerce")

        sma_col = f"SMA_{self.sma_period}"
        ema_col = f"EMA_{self.ema_period}"
        rsi_col = f"RSI_{self.rsi_period}"

        if _HAS_PANDAS_TA:
            try:
                out[sma_col] = ta.sma(close, length=self.sma_period)
                out[ema_col] = ta.ema(close, length=self.ema_period)
                out[rsi_col] = ta.rsi(close, length=self.rsi_period)
            except Exception as exc:  # noqa: BLE001
                log.warning("pandas-ta failed (%s); using built-in indicators.", exc)
                out[sma_col] = sma(close, self.sma_period)
                out[ema_col] = ema(close, self.ema_period)
                out[rsi_col] = rsi(close, self.rsi_period)
        else:
            out[sma_col] = sma(close, self.sma_period)
            out[ema_col] = ema(close, self.ema_period)
            out[rsi_col] = rsi(close, self.rsi_period)

        out["sma"] = out[sma_col]
        out["ema"] = out[ema_col]
        out["rsi"] = out[rsi_col]

        if "high" in out.columns and "low" in out.columns:
            out["adx"] = adx(out, self.adx_period)
        else:
            out["adx"] = np.nan
            log.debug("No high/low columns available; ADX filter disabled for this frame.")

        out["macd_hist"] = macd_histogram(close)
        out["macd_hist_prev"] = out["macd_hist"].shift(1)

        return out

    # ------------------------------------------------------------------ #
    def analyze_symbol(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Evaluate the most recent bar of ``df``.

        Returns
        -------
        dict
            ``{'signal': 'BUY'|'NEUTRAL', 'current_price': float, ...}`` with
            diagnostic indicator values and condition flags included.
        """
        neutral = {
            "signal": "NEUTRAL",
            "current_price": 0.0,
            "sma": None,
            "ema": None,
            "rsi": None,
            "adx": None,
            "trend_ok": False,
            "pullback_ok": False,
            "reason": "insufficient data",
        }

        if df is None or df.empty:
            return neutral

        if len(df) < self.min_bars_required:
            neutral["reason"] = (
                f"need >= {self.min_bars_required} bars, got {len(df)}"
            )
            try:
                neutral["current_price"] = float(df["close"].iloc[-1])
            except (KeyError, IndexError, TypeError, ValueError):
                pass
            return neutral

        enriched = self.add_indicators(df)
        last = enriched.iloc[-1]

        try:
            current_price = float(last["close"])
            sma_val = float(last["sma"])
            ema_val = float(last["ema"])
            rsi_val = float(last["rsi"])
            adx_val = float(last["adx"])
            macd_hist_val = float(last["macd_hist"])
            macd_hist_prev_val = float(last["macd_hist_prev"])
        except (TypeError, ValueError, KeyError):
            neutral["reason"] = "indicator values unavailable (NaN warm-up period)"
            return neutral

        core_vals = (current_price, sma_val, ema_val, rsi_val)
        if any(pd.isna(v) for v in core_vals) or ema_val == 0:
            neutral["current_price"] = current_price if not pd.isna(current_price) else 0.0
            neutral["reason"] = "indicator values unavailable (NaN warm-up period)"
            return neutral

        # ADX / MACD may briefly be NaN even after warm-up on the very first
        # eligible bar; treat that as "filter not satisfied" rather than crashing.
        adx_ok_value = 0.0 if pd.isna(adx_val) else adx_val
        macd_hist_val = 0.0 if pd.isna(macd_hist_val) else macd_hist_val
        macd_hist_prev_val = 0.0 if pd.isna(macd_hist_prev_val) else macd_hist_prev_val

        trend_ok = (current_price > sma_val) and (adx_ok_value > self.adx_threshold)
        ema_distance = abs(current_price - ema_val) / ema_val
        momentum_turning = macd_hist_val > macd_hist_prev_val
        pullback_ok = (
            (rsi_val < self.rsi_buy_threshold)
            and (ema_distance <= self.ema_proximity_pct)
            and momentum_turning
        )
        signal = "BUY" if (trend_ok and pullback_ok) else "NEUTRAL"

        if signal == "BUY":
            reason = "trend + ADX + pullback + momentum confirmed"
        elif current_price <= sma_val:
            reason = f"price {current_price:.4f} <= SMA{self.sma_period} {sma_val:.4f}"
        elif adx_ok_value <= self.adx_threshold:
            reason = f"ADX {adx_ok_value:.2f} <= {self.adx_threshold} (no real trend)"
        elif rsi_val >= self.rsi_buy_threshold:
            reason = f"RSI {rsi_val:.2f} >= {self.rsi_buy_threshold}"
        elif ema_distance > self.ema_proximity_pct:
            reason = (
                f"price {ema_distance:.4%} away from EMA{self.ema_period} "
                f"(max {self.ema_proximity_pct:.4%})"
            )
        else:
            reason = "MACD histogram not rising (momentum not confirmed)"

        return {
            "signal": signal,
            "current_price": current_price,
            "sma": sma_val,
            "ema": ema_val,
            "rsi": rsi_val,
            "adx": adx_ok_value,
            "macd_hist": macd_hist_val,
            "ema_distance_pct": ema_distance,
            "trend_ok": bool(trend_ok),
            "pullback_ok": bool(pullback_ok),
            "reason": reason,
            "timestamp": last.get("timestamp"),
        }


# Module-level convenience wrapper -------------------------------------------
_DEFAULT_STRATEGY: Optional[TechnicalStrategy] = None


def analyze_symbol(df: pd.DataFrame) -> Dict[str, Any]:
    """Analyze ``df`` with a default-configured :class:`TechnicalStrategy`."""
    global _DEFAULT_STRATEGY
    if _DEFAULT_STRATEGY is None:
        _DEFAULT_STRATEGY = TechnicalStrategy()
    return _DEFAULT_STRATEGY.analyze_symbol(df)
