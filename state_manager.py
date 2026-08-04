"""
state_manager.py
----------------
Thread-safe, atomic persistence of open position state to ``positions.json``.

Schema of positions.json::

    {
      "BTC/USD": {
        "symbol": "BTC/USD",
        "entry_price": 64000.12,
        "qty": 0.0031,
        "take_profit_price": 67200.13,
        "stop_loss_price": 62720.12,
        "trailing_stop_price": 62720.12,
        "highest_price": 64500.0,
        "opened_at": "2026-08-03T20:15:00+00:00",
        "last_updated": "2026-08-03T20:15:00+00:00",
        "source": "bot" | "reconciled"
      }
    }

Every write goes to a temporary file in the same directory and is then
``os.replace``-d over the target, which is atomic on both POSIX and Windows.
That guarantees the file is never left half-written if the process dies.
"""

import json
import math
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config
from logger import get_logger

log = get_logger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _floor_qty(qty: float, precision: int) -> float:
    """
    Truncate ``qty`` DOWN to ``precision`` decimals.

    Ordinary ``round()`` rounds to nearest, which can round *up* past what we
    actually own (0.078593517 -> 0.0786). Selling a quantity larger than the
    real balance gets rejected by Alpaca, which would strand the position and
    prevent the stop-loss from ever executing. Flooring can only ever leave a
    negligible dust remainder behind, which is the safe direction to err.
    """
    factor = 10 ** precision
    return math.floor(float(qty) * factor) / factor



class StateManager:
    """Persist and reconcile the bot's view of its open positions."""

    def __init__(self, filepath: Optional[str] = None) -> None:
        self.filepath = filepath or config.POSITIONS_FILE
        self._lock = threading.RLock()
        self._ensure_file()

    # ------------------------------------------------------------------ IO --
    def _ensure_file(self) -> None:
        """Create an empty state file if it does not exist yet."""
        with self._lock:
            if not os.path.exists(self.filepath):
                directory = os.path.dirname(os.path.abspath(self.filepath))
                if directory and not os.path.isdir(directory):
                    os.makedirs(directory, exist_ok=True)
                self._atomic_write({})
                log.info("Created new state file at %s", self.filepath)

    def _atomic_write(self, data: Dict[str, Any]) -> None:
        """Write ``data`` as JSON atomically (temp file + os.replace)."""
        directory = os.path.dirname(os.path.abspath(self.filepath)) or "."
        fd, tmp_path = tempfile.mkstemp(prefix=".positions_", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.filepath)
        except Exception:
            # Never leave orphaned temp files behind.
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise

    def load_positions(self) -> Dict[str, Any]:
        """
        Return the full position dictionary.

        A corrupted or unreadable file is backed up and replaced with an empty
        dict rather than crashing the trading loop.
        """
        with self._lock:
            if not os.path.exists(self.filepath):
                return {}
            try:
                with open(self.filepath, "r", encoding="utf-8") as handle:
                    raw = handle.read().strip()
                if not raw:
                    return {}
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("positions.json root element must be a JSON object")
                return data
            except (json.JSONDecodeError, ValueError, OSError) as exc:
                backup = f"{self.filepath}.corrupt.{int(datetime.now(timezone.utc).timestamp())}"
                log.error("State file unreadable (%s). Backing up to %s and resetting.", exc, backup)
                try:
                    os.replace(self.filepath, backup)
                except OSError:
                    pass
                self._atomic_write({})
                return {}

    def _write_all(self, positions: Dict[str, Any]) -> None:
        with self._lock:
            self._atomic_write(positions)

    # -------------------------------------------------------------- Records --
    def save_position(
        self,
        symbol: str,
        entry_price: float,
        qty: float,
        take_profit_price: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
        source: str = "bot",
        opened_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Insert or update a position record and persist it.

        Take-profit / stop-loss levels default to the configured percentages.
        """
        entry_price = float(entry_price)
        qty = float(qty)

        if take_profit_price is None:
            take_profit_price = entry_price * (1.0 + config.TAKE_PROFIT_PCT)
        if stop_loss_price is None:
            stop_loss_price = entry_price * (1.0 - config.STOP_LOSS_PCT)

        with self._lock:
            positions = self.load_positions()
            existing = positions.get(symbol, {})
            record = {
                "symbol": symbol,
                "entry_price": entry_price,
                "qty": qty,
                "take_profit_price": float(take_profit_price),
                "stop_loss_price": float(stop_loss_price),
                # Trailing stop starts at the hard stop and only ever ratchets up.
                "trailing_stop_price": float(
                    max(existing.get("trailing_stop_price", stop_loss_price), stop_loss_price)
                ),
                "highest_price": float(max(existing.get("highest_price", entry_price), entry_price)),
                "opened_at": opened_at or existing.get("opened_at") or _utc_now_iso(),
                "last_updated": _utc_now_iso(),
                "source": source,
            }
            positions[symbol] = record
            self._write_all(positions)

        log.info(
            "State saved: %s qty=%.8f entry=%.6f TP=%.6f SL=%.6f (source=%s)",
            symbol, qty, entry_price, record["take_profit_price"], record["stop_loss_price"], source,
        )
        return record

    def update_position(self, symbol: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """Patch arbitrary fields on an existing record (e.g. trailing stop)."""
        with self._lock:
            positions = self.load_positions()
            if symbol not in positions:
                log.warning("update_position called for unknown symbol %s", symbol)
                return None
            positions[symbol].update(fields)
            positions[symbol]["last_updated"] = _utc_now_iso()
            self._write_all(positions)
            return positions[symbol]

    def remove_position(self, symbol: str) -> bool:
        """Delete a position record. Returns True if something was removed."""
        with self._lock:
            positions = self.load_positions()
            if symbol in positions:
                positions.pop(symbol, None)
                self._write_all(positions)
                log.info("State removed for %s", symbol)
                return True
            log.debug("remove_position: %s not present in state", symbol)
            return False

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return a single position record or None."""
        return self.load_positions().get(symbol)

    def open_symbols(self) -> List[str]:
        return list(self.load_positions().keys())

    def position_count(self) -> int:
        return len(self.load_positions())

    def total_exposure(self, price_map: Optional[Dict[str, float]] = None) -> float:
        """
        Total dollar value currently deployed.

        Uses ``price_map`` (symbol -> live price) where available, otherwise
        falls back to the stored entry price.
        """
        price_map = price_map or {}
        total = 0.0
        for symbol, record in self.load_positions().items():
            price = float(price_map.get(symbol, record.get("entry_price", 0.0)) or 0.0)
            total += price * float(record.get("qty", 0.0) or 0.0)
        return total

    # ---------------------------------------------------------- Reconcile --
    def reconcile_with_broker(self, broker_positions: List[Any]) -> Dict[str, Any]:
        """
        Reconcile the local state file against Alpaca's live positions.

        Alpaca is ALWAYS the source of truth:

        * Position live at broker but missing locally -> adopt it, deriving
          TP/SL from the broker's average entry price and log a warning.
        * Position live at broker with a different qty -> update local qty.
        * Position local but NOT live at broker (closed manually / elsewhere)
          -> drop the stale local record and log a warning.

        Parameters
        ----------
        broker_positions : list
            Objects returned by ``TradingClient.get_all_positions()`` (or dicts
            with ``symbol`` / ``qty`` / ``avg_entry_price`` keys).

        Returns
        -------
        dict
            The reconciled position dictionary as persisted to disk.
        """
        normalised: Dict[str, Dict[str, float]] = {}
        for pos in broker_positions or []:
            symbol = self._normalise_symbol(self._attr(pos, "symbol"))
            if not symbol:
                continue
            try:
                qty = abs(float(self._attr(pos, "qty") or 0.0))
                avg_entry = float(self._attr(pos, "avg_entry_price") or 0.0)
            except (TypeError, ValueError):
                log.warning("Skipping broker position with unparsable numbers: %r", pos)
                continue
            if qty <= 0:
                continue
            normalised[symbol] = {"qty": qty, "avg_entry_price": avg_entry}

        with self._lock:
            local = self.load_positions()
            reconciled: Dict[str, Any] = {}

            for symbol, broker in normalised.items():
                # FLOOR, never round-to-nearest. round() can round UP (e.g.
                # 0.078593517 -> 0.0786), which would make us try to sell more
                # than we actually hold and get the exit order rejected for
                # insufficient balance. Flooring leaves a harmless dust remainder.
                qty = _floor_qty(broker["qty"], config.QTY_PRECISION)
                entry = broker["avg_entry_price"]

                record = local.get(symbol)

                if record is None:
                    log.warning(
                        "RECONCILE: %s is open at Alpaca (qty=%.8f entry=%.6f) but missing "
                        "from local state. Adopting broker data as source of truth.",
                        symbol, qty, entry,
                    )
                    reconciled[symbol] = {
                        "symbol": symbol,
                        "entry_price": entry,
                        "qty": qty,
                        "take_profit_price": entry * (1.0 + config.TAKE_PROFIT_PCT),
                        "stop_loss_price": entry * (1.0 - config.STOP_LOSS_PCT),
                        "trailing_stop_price": entry * (1.0 - config.STOP_LOSS_PCT),
                        "highest_price": entry,
                        "opened_at": _utc_now_iso(),
                        "last_updated": _utc_now_iso(),
                        "source": "reconciled",
                    }
                    continue

                updated = dict(record)
                if abs(float(record.get("qty", 0.0)) - qty) > 10 ** (-config.QTY_PRECISION):
                    log.warning(
                        "RECONCILE: %s qty mismatch (local=%.8f broker=%.8f). Using broker qty.",
                        symbol, float(record.get("qty", 0.0)), qty,
                    )
                    updated["qty"] = qty

                local_entry = float(record.get("entry_price", 0.0) or 0.0)
                if entry > 0 and (local_entry <= 0 or abs(local_entry - entry) / entry > 0.005):
                    log.warning(
                        "RECONCILE: %s entry price mismatch (local=%.6f broker=%.6f). "
                        "Using broker average entry and recomputing TP/SL.",
                        symbol, local_entry, entry,
                    )
                    updated["entry_price"] = entry
                    updated["take_profit_price"] = entry * (1.0 + config.TAKE_PROFIT_PCT)
                    updated["stop_loss_price"] = entry * (1.0 - config.STOP_LOSS_PCT)
                    updated["trailing_stop_price"] = entry * (1.0 - config.STOP_LOSS_PCT)
                    updated["highest_price"] = max(entry, float(record.get("highest_price", entry)))

                updated.setdefault("symbol", symbol)
                updated.setdefault("highest_price", updated.get("entry_price", entry))
                updated.setdefault(
                    "trailing_stop_price",
                    updated.get("stop_loss_price", entry * (1.0 - config.STOP_LOSS_PCT)),
                )
                updated["last_updated"] = _utc_now_iso()
                reconciled[symbol] = updated

            for symbol in local.keys():
                if symbol not in normalised:
                    log.warning(
                        "RECONCILE: %s exists in local state but is NOT open at Alpaca. "
                        "Dropping the stale local record (broker is source of truth).",
                        symbol,
                    )

            self._write_all(reconciled)

        log.info("Reconciliation complete. Tracking %d open position(s): %s",
                 len(reconciled), list(reconciled.keys()) or "none")
        return reconciled

    # ------------------------------------------------------------ Helpers --
    @staticmethod
    def _attr(obj: Any, name: str) -> Any:
        """Read ``name`` from an object attribute or a dict key."""
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    @staticmethod
    def _normalise_symbol(symbol: Any) -> str:
        """
        Alpaca returns crypto positions as ``BTCUSD`` while requests use
        ``BTC/USD``. Map broker symbols back onto the configured watchlist form.
        """
        if not symbol:
            return ""
        raw = str(symbol).upper().strip()
        if raw in config.WATCHLIST:
            return raw
        compact = raw.replace("/", "")
        for watch in config.WATCHLIST:
            if watch.replace("/", "").upper() == compact:
                return watch
        # Fall back to a best-effort slash insertion for common quote currencies.
        for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
            if compact.endswith(quote) and len(compact) > len(quote):
                return f"{compact[: -len(quote)]}/{quote}"
        return raw
