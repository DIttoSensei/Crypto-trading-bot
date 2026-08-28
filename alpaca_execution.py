"""
alpaca_execution.py
-------------------
Order execution and account access via ``alpaca.trading.client.TradingClient``.

IMPORTANT - Alpaca crypto constraints handled here:

* ``OrderClass.BRACKET`` (and OTO/OCO) is REJECTED for crypto. Only plain
  market/limit orders are submitted; take-profit and stop-loss are enforced in
  Python by :mod:`main`.
* Crypto is cash-only (non-marginable), so ``non_marginable_buying_power`` /
  ``cash`` is used for sizing rather than ``buying_power``.
* BUY quantities are rounded to ``config.QTY_PRECISION`` (4) decimals. SELL
  quantities are FLOORED (truncated down), never rounded - rounding up can
  request more than is actually held and get the exit order rejected for
  insufficient balance, which strands the position and disables the stop-loss.
* Orders are polled after submission so the *actual* filled quantity and
  average fill price are used for state, which correctly handles partial fills.
"""

import math
import time
from typing import Any, Dict, List, Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

import config
from logger import get_logger

log = get_logger(__name__)

# Statuses after which no further fills can occur.
_TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.EXPIRED,
    OrderStatus.REJECTED,
    OrderStatus.SUSPENDED,
    OrderStatus.STOPPED,
}


class AlpacaExecutionClient:
    """Trading-side wrapper around alpaca-py with defensive error handling."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        paper: Optional[bool] = None,
    ) -> None:
        key = api_key if api_key is not None else config.ALPACA_API_KEY
        secret = secret_key if secret_key is not None else config.ALPACA_SECRET_KEY
        self.paper = config.PAPER if paper is None else bool(paper)

        if not key or not secret:
            raise RuntimeError(
                "Alpaca API credentials are required for trading. "
                "Set ALPACA_API_KEY and ALPACA_SECRET_KEY in your .env file."
            )

        self.client = TradingClient(api_key=key, secret_key=secret, paper=self.paper)
        log.info(
            "TradingClient initialised against %s endpoint.",
            "PAPER (https://paper-api.alpaca.markets)" if self.paper else "LIVE",
        )

    # ------------------------------------------------------------ Account --
    def get_account(self) -> Optional[Any]:
        try:
            return self.client.get_account()
        except Exception as exc:  # noqa: BLE001
            log.error("Could not fetch account: %s", exc)
            return None

    def get_account_balance(self) -> float:
        """
        Cash available for crypto buying.

        Crypto on Alpaca is non-marginable, so the smallest of
        ``non_marginable_buying_power`` and ``cash`` is the honest number.
        Returns 0.0 if the account cannot be read.
        """
        account = self.get_account()
        if account is None:
            return 0.0

        candidates = []
        for field in ("non_marginable_buying_power", "cash"):
            raw = getattr(account, field, None)
            if raw is not None:
                try:
                    candidates.append(float(raw))
                except (TypeError, ValueError):
                    continue

        if not candidates:
            log.error("Account object exposed no usable cash fields.")
            return 0.0

        balance = max(0.0, min(candidates))
        log.debug("Available crypto buying power: $%.2f", balance)
        return balance

    def get_equity(self) -> float:
        account = self.get_account()
        if account is None:
            return 0.0
        try:
            return float(getattr(account, "equity", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def is_account_tradable(self) -> bool:
        """False when the account is blocked, restricted or flagged."""
        account = self.get_account()
        if account is None:
            return False
        blocked = bool(getattr(account, "trading_blocked", False)) or bool(
            getattr(account, "account_blocked", False)
        )
        if blocked:
            log.error("Account is blocked from trading. Halting new entries.")
            return False
        return True

    # ---------------------------------------------------------- Positions --
    def get_all_open_positions(self) -> List[Any]:
        """Live positions from Alpaca (used as the reconciliation source of truth)."""
        try:
            positions = self.client.get_all_positions() or []
            log.debug("Broker reports %d open position(s).", len(positions))
            return list(positions)
        except Exception as exc:  # noqa: BLE001
            log.error("Could not fetch open positions: %s", exc)
            return []

    def get_position_qty(self, symbol: str) -> float:
        """
        Quantity currently held for ``symbol`` (0.0 if flat).

        Handles Alpaca returning crypto symbols without the slash (``BTCUSD``).
        """
        target = symbol.replace("/", "").upper()
        for pos in self.get_all_open_positions():
            raw = str(getattr(pos, "symbol", "")).replace("/", "").upper()
            if raw == target:
                try:
                    return abs(float(getattr(pos, "qty", 0.0) or 0.0))
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    # ------------------------------------------------------------- Orders --
    def execute_buy(self, symbol: str, qty: float) -> Dict[str, Any]:
        """Submit a GTC market BUY and return the realised fill details."""
        return self._submit_market_order(symbol, qty, OrderSide.BUY)

    def execute_sell(self, symbol: str, qty: float) -> Dict[str, Any]:
        """
        Submit a GTC market SELL and return the realised fill details.

        The requested quantity is clamped to the quantity actually held and then
        FLOORED (truncated down) to ``QTY_PRECISION`` decimals. Rounding to
        nearest can round *up* past what we actually hold (0.340967717 -> 0.341),
        and Alpaca then rejects the order with "insufficient balance", which
        strands the position and disables the take-profit / stop-loss. Flooring
        leaves a negligible dust remainder instead, which is the safe direction
        to err.

        A full-position exit (``qty`` within precision of the held balance) is
        routed through the broker's dedicated ``close_position`` endpoint, which
        liquidates the EXACT held quantity - no dust remainder, and nothing that
        could exceed the balance.
        """
        held = self.get_position_qty(symbol)
        if held <= 0:
            log.warning("execute_sell: no live position in %s - nothing to sell.", symbol)
            return self._failure(symbol, "sell", qty, "no live position at broker")

        qty = min(float(qty), held)
        if abs(qty - held) <= 10 ** (-config.QTY_PRECISION):
            # Full position: broker close liquidates the exact balance.
            return self.close_position(symbol)

        qty = _floor_qty(qty, config.QTY_PRECISION)
        if qty <= 0:
            log.warning("execute_sell: quantity %.8f floors to zero for %s.", qty, symbol)
            return self._failure(symbol, "sell", qty, "quantity floors to zero")

        return self._submit_market_order(symbol, qty, OrderSide.SELL)

    def execute_limit_buy(self, symbol: str, qty: float, limit_price: float) -> Dict[str, Any]:
        """
        GTC limit BUY. Provided for completeness; ``main`` uses market orders.
        Alpaca rejects bracket order classes for crypto, so this stays plain.
        """
        qty = round(float(qty), config.QTY_PRECISION)
        if qty <= 0:
            return self._failure(symbol, "buy", qty, "quantity rounds to zero")
        try:
            request = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,
                limit_price=round(float(limit_price), 2),
            )
            order = self.client.submit_order(order_data=request)
            log.info("Submitted LIMIT BUY %s qty=%.4f @ %.2f (id=%s)",
                     symbol, qty, limit_price, getattr(order, "id", "?"))
            return self._poll_order(order, symbol, "buy", qty)
        except Exception as exc:  # noqa: BLE001
            log.error("LIMIT BUY failed for %s: %s", symbol, exc)
            return self._failure(symbol, "buy", qty, str(exc))

    def close_position(self, symbol: str) -> Dict[str, Any]:
        """Liquidate the entire position via the broker's close endpoint."""
        held = self.get_position_qty(symbol)
        if held <= 0:
            return self._failure(symbol, "sell", 0.0, "no live position at broker")
        try:
            order = self.client.close_position(symbol.replace("/", ""))
            log.info("Submitted CLOSE for %s (qty=%.8f, id=%s)",
                     symbol, held, getattr(order, "id", "?"))
            return self._poll_order(order, symbol, "sell", held)
        except Exception as exc:  # noqa: BLE001
            log.error("close_position failed for %s: %s. Falling back to market sell.", symbol, exc)
            return self._submit_market_order(symbol, held, OrderSide.SELL)

    # ------------------------------------------------------------ Internal --
    def _submit_market_order(self, symbol: str, qty: float, side: OrderSide) -> Dict[str, Any]:
        action = "buy" if side == OrderSide.BUY else "sell"
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            return self._failure(symbol, action, 0.0, "quantity is not numeric")

        if action == "sell":
            # NEVER round a sell up. Round-to-nearest can round past the real
            # balance (0.340967717 -> 0.341) and Alpaca rejects the order for
            # "insufficient balance", stranding the position. Floor instead.
            qty = _floor_qty(qty, config.QTY_PRECISION)
        else:
            qty = round(qty, config.QTY_PRECISION)

        if qty <= 0:
            log.warning("%s order for %s skipped: quantity %.8f is not positive.",
                        action.upper(), symbol, qty)
            return self._failure(symbol, action, qty, "quantity is not positive")

        try:
            # NOTE: no OrderClass.BRACKET - Alpaca rejects it for crypto assets.
            request = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.GTC,
            )
            order = self.client.submit_order(order_data=request)
            log.info("Submitted MARKET %s %s qty=%.4f (id=%s)",
                     action.upper(), symbol, qty, getattr(order, "id", "?"))
        except Exception as exc:  # noqa: BLE001
            log.error("MARKET %s failed for %s qty=%.4f: %s", action.upper(), symbol, qty, exc)
            return self._failure(symbol, action, qty, str(exc))

        return self._poll_order(order, symbol, action, qty)

    def _poll_order(self, order: Any, symbol: str, action: str, requested_qty: float) -> Dict[str, Any]:
        """
        Poll an order until it reaches a terminal state or the timeout expires.

        Returns the ACTUAL filled quantity and average fill price so partial
        fills are recorded correctly in ``positions.json``.
        """
        order_id = getattr(order, "id", None)
        deadline = time.time() + config.ORDER_POLL_TIMEOUT_SECONDS
        latest = order

        while order_id is not None and time.time() < deadline:
            try:
                latest = self.client.get_order_by_id(order_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("Order status poll failed for %s (%s): %s", symbol, order_id, exc)
                break

            status = getattr(latest, "status", None)
            if status in _TERMINAL_STATUSES:
                break
            time.sleep(config.ORDER_POLL_INTERVAL_SECONDS)

        filled_qty = self._as_float(getattr(latest, "filled_qty", 0.0))
        avg_price = self._as_float(getattr(latest, "filled_avg_price", 0.0))
        status = getattr(latest, "status", None)
        status_name = getattr(status, "value", str(status))

        if filled_qty <= 0:
            log.error(
                "%s %s did not fill (status=%s, requested=%.4f). No state change.",
                action.upper(), symbol, status_name, requested_qty,
            )
            return {
                "success": False,
                "symbol": symbol,
                "side": action,
                "requested_qty": requested_qty,
                "filled_qty": 0.0,
                "filled_avg_price": 0.0,
                "status": status_name,
                "order_id": str(order_id) if order_id else None,
                "error": f"order not filled (status={status_name})",
            }

        if avg_price <= 0:
            log.warning("%s %s filled %.8f but reported no average price.",
                        action.upper(), symbol, filled_qty)

        partial = filled_qty + 10 ** (-config.QTY_PRECISION) < requested_qty
        if partial:
            log.warning(
                "PARTIAL FILL: %s %s filled %.8f of %.8f requested (status=%s). "
                "Using the actual filled quantity for state.",
                action.upper(), symbol, filled_qty, requested_qty, status_name,
            )

        log.info(
            "%s FILLED %s qty=%.8f @ avg %.6f (notional $%.2f, status=%s)",
            action.upper(), symbol, filled_qty, avg_price, filled_qty * avg_price, status_name,
        )

        return {
            "success": True,
            "symbol": symbol,
            "side": action,
            "requested_qty": requested_qty,
            "filled_qty": filled_qty,
            "filled_avg_price": avg_price,
            "notional": filled_qty * avg_price,
            "partial": partial,
            "status": status_name,
            "order_id": str(order_id) if order_id else None,
            "error": None,
        }

    @staticmethod
    def _failure(symbol: str, side: str, qty: float, error: str) -> Dict[str, Any]:
        return {
            "success": False,
            "symbol": symbol,
            "side": side,
            "requested_qty": float(qty or 0.0),
            "filled_qty": 0.0,
            "filled_avg_price": 0.0,
            "notional": 0.0,
            "partial": False,
            "status": "not_submitted",
            "order_id": None,
            "error": error,
        }

    @staticmethod
    def _as_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0


def _floor_qty(qty: float, precision: int) -> float:
    """
    Truncate ``qty`` DOWN to ``precision`` decimals.

    Ordinary ``round()`` rounds to nearest, which can round *up* past what we
    actually own (0.340967717 -> 0.341). Selling a quantity larger than the
    real balance gets rejected by Alpaca ("insufficient balance"), which would
    strand the position and prevent the take-profit / stop-loss from ever
    executing. Flooring can only ever leave a negligible dust remainder behind,
    which is the safe direction to err.
    """
    factor = 10 ** int(precision)
    return math.floor(float(qty) * factor) / factor
