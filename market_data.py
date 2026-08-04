"""
market_data.py
--------------
Historical crypto bar retrieval via the official alpaca-py SDK.

The crypto market data endpoint does not require API keys, but they are passed
when available so the account's rate limits apply.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import pandas as pd

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, CryptoLatestBarRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import config
from logger import get_logger

log = get_logger(__name__)

EMPTY_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]

# Approximate minutes-per-bar, used to size the lookback window for `limit`
# based requests.
_UNIT_MINUTES = {
    TimeFrameUnit.Minute: 1,
    TimeFrameUnit.Hour: 60,
    TimeFrameUnit.Day: 60 * 24,
    TimeFrameUnit.Week: 60 * 24 * 7,
    TimeFrameUnit.Month: 60 * 24 * 31,
}


def parse_timeframe(timeframe: Union[str, TimeFrame]) -> TimeFrame:
    """
    Convert a string such as ``'15Min'``, ``'1Hour'`` or ``'1Day'`` into an
    alpaca-py :class:`TimeFrame`.
    """
    if isinstance(timeframe, TimeFrame):
        return timeframe

    raw = str(timeframe).strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    letters = "".join(ch for ch in raw if ch.isalpha()).lower()
    amount = int(digits) if digits else 1

    if letters.startswith("min") or letters in ("m", "t"):
        unit = TimeFrameUnit.Minute
    elif letters.startswith("h"):
        unit = TimeFrameUnit.Hour
    elif letters.startswith("d"):
        unit = TimeFrameUnit.Day
    elif letters.startswith("w"):
        unit = TimeFrameUnit.Week
    elif letters.startswith("mo"):
        unit = TimeFrameUnit.Month
    else:
        raise ValueError(f"Unsupported timeframe string: {timeframe!r}")

    return TimeFrame(amount=amount, unit=unit)


def _timeframe_minutes(tf: TimeFrame) -> int:
    return max(1, int(tf.amount) * _UNIT_MINUTES.get(tf.unit, 1))


def _to_utc(value: Union[str, datetime]) -> datetime:
    """Coerce a date string or datetime into a timezone-aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = pd.Timestamp(str(value)).to_pydatetime()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class CryptoDataFetcher:
    """Thin wrapper around :class:`CryptoHistoricalDataClient`."""

    def __init__(self, api_key: Optional[str] = None, secret_key: Optional[str] = None) -> None:
        key = api_key if api_key is not None else config.ALPACA_API_KEY
        secret = secret_key if secret_key is not None else config.ALPACA_SECRET_KEY

        if key and secret:
            self.client = CryptoHistoricalDataClient(api_key=key, secret_key=secret)
        else:
            # Crypto market data is available without credentials.
            log.warning("No API credentials supplied - using anonymous crypto data client.")
            self.client = CryptoHistoricalDataClient()

    # ------------------------------------------------------------------ API --
    def get_bars(
        self,
        symbol: str,
        timeframe: Union[str, TimeFrame] = None,
        limit: int = None,
    ) -> pd.DataFrame:
        """
        Fetch the most recent ``limit`` bars for ``symbol``.

        Returns a DataFrame with columns
        ``['timestamp', 'open', 'high', 'low', 'close', 'volume']`` sorted
        oldest -> newest, timestamps in UTC. Returns an empty DataFrame with the
        same columns on failure so callers can simply check ``df.empty``.
        """
        timeframe = timeframe or config.BAR_TIMEFRAME
        limit = int(limit or config.BAR_LIMIT)
        tf = parse_timeframe(timeframe)

        # Ask for a generous window: crypto trades 24/7 so bar count maps almost
        # 1:1 to wall-clock time, but we pad 3x to survive data gaps/outages.
        minutes_needed = _timeframe_minutes(tf) * limit * 3
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=minutes_needed)

        df = self.get_bars_range(symbol, tf, start, end)
        if df.empty:
            return df
        return df.tail(limit).reset_index(drop=True)

    def get_bars_range(
        self,
        symbol: str,
        timeframe: Union[str, TimeFrame],
        start: Union[str, datetime],
        end: Union[str, datetime],
    ) -> pd.DataFrame:
        """
        Fetch every bar for ``symbol`` between ``start`` and ``end`` (UTC).

        Used by the backtester; alpaca-py transparently pages through results.
        """
        tf = parse_timeframe(timeframe)
        start_dt = _to_utc(start)
        end_dt = _to_utc(end)

        if start_dt >= end_dt:
            log.error("Invalid range for %s: start (%s) is not before end (%s).",
                      symbol, start_dt, end_dt)
            return pd.DataFrame(columns=EMPTY_COLUMNS)

        request = CryptoBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=tf,
            start=start_dt,
            end=end_dt,
        )

        try:
            bars = self.client.get_crypto_bars(request)
        except Exception as exc:  # noqa: BLE001 - never let data errors kill the loop
            log.error("Failed to fetch bars for %s (%s -> %s): %s",
                      symbol, start_dt.date(), end_dt.date(), exc)
            return pd.DataFrame(columns=EMPTY_COLUMNS)

        return self._normalise(bars, symbol)

    def get_latest_price(self, symbol: str) -> Optional[float]:
        """
        Best-effort latest traded price for ``symbol``.

        Falls back to the most recent 15-minute close if the latest-bar endpoint
        fails. Returns None if no price could be resolved.
        """
        try:
            request = CryptoLatestBarRequest(symbol_or_symbols=[symbol])
            latest = self.client.get_crypto_latest_bar(request)
            bar = latest.get(symbol) if isinstance(latest, dict) else None
            if bar is not None and getattr(bar, "close", None):
                return float(bar.close)
        except Exception as exc:  # noqa: BLE001
            log.warning("Latest bar lookup failed for %s: %s. Falling back to recent bars.",
                        symbol, exc)

        df = self.get_bars(symbol, config.BAR_TIMEFRAME, limit=2)
        if df.empty:
            log.error("Unable to determine a current price for %s.", symbol)
            return None
        return float(df["close"].iloc[-1])

    # -------------------------------------------------------------- Helpers --
    @staticmethod
    def _normalise(bars, symbol: str) -> pd.DataFrame:
        """Turn an alpaca-py BarSet into the canonical flat DataFrame."""
        try:
            df = bars.df
        except Exception as exc:  # noqa: BLE001
            log.error("Could not convert bar response to DataFrame for %s: %s", symbol, exc)
            return pd.DataFrame(columns=EMPTY_COLUMNS)

        if df is None or df.empty:
            log.warning("No bars returned for %s.", symbol)
            return pd.DataFrame(columns=EMPTY_COLUMNS)

        df = df.copy()

        # Multi-symbol responses come back with a (symbol, timestamp) MultiIndex.
        if isinstance(df.index, pd.MultiIndex):
            if symbol in df.index.get_level_values(0):
                df = df.xs(symbol, level=0)
            else:
                df = df.droplevel(0)

        df = df.reset_index()

        rename = {}
        for col in df.columns:
            low = str(col).lower()
            if low in ("timestamp", "time", "t", "index"):
                rename[col] = "timestamp"
            elif low in ("open", "high", "low", "close", "volume"):
                rename[col] = low
        df = df.rename(columns=rename)

        for col in EMPTY_COLUMNS:
            if col not in df.columns:
                df[col] = pd.NA

        df = df[EMPTY_COLUMNS]
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = (
            df.dropna(subset=["timestamp", "open", "high", "low", "close"])
            .sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"], keep="last")
            .reset_index(drop=True)
        )

        log.debug("Fetched %d bars for %s (%s -> %s).", len(df), symbol,
                  df["timestamp"].iloc[0] if not df.empty else "n/a",
                  df["timestamp"].iloc[-1] if not df.empty else "n/a")
        return df


# Module level convenience wrappers ------------------------------------------
_DEFAULT_FETCHER: Optional[CryptoDataFetcher] = None


def _default_fetcher() -> CryptoDataFetcher:
    global _DEFAULT_FETCHER
    if _DEFAULT_FETCHER is None:
        _DEFAULT_FETCHER = CryptoDataFetcher()
    return _DEFAULT_FETCHER


def get_bars(symbol: str, timeframe: str = "15Min", limit: int = 250) -> pd.DataFrame:
    """Module-level shortcut for :meth:`CryptoDataFetcher.get_bars`."""
    return _default_fetcher().get_bars(symbol, timeframe, limit)


def get_bars_range(symbol: str, timeframe: str, start, end) -> pd.DataFrame:
    """Module-level shortcut for :meth:`CryptoDataFetcher.get_bars_range`."""
    return _default_fetcher().get_bars_range(symbol, timeframe, start, end)
