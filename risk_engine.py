"""
risk_engine.py
--------------
Position sizing and capital-allocation guardrails.

Sizing rules, applied in order:

1. **1% risk rule** - risk at most ``MAX_RISK_PER_TRADE`` (1%) of account cash
   on the distance to the stop-loss (``STOP_LOSS_PCT``, 2%). With a 2% stop the
   notional works out to ``risk_dollars / 0.02`` = 50% of cash *before* the caps
   below are applied - which is exactly why the exposure cap matters.
2. **Portfolio exposure cap** - ``current_exposure + new_position`` must never
   exceed ``MAX_TOTAL_EXPOSURE_PCT`` (20%) of account cash.
3. **Buying power cap** - never order more than the cash actually available
   (Alpaca crypto is cash-only; there is no margin).
4. **Minimum notional** - anything below ``MIN_NOTIONAL_USD`` ($10) is skipped
   entirely rather than submitted.
5. **Precision** - final quantity is ``round(qty, 4)``.
"""

from typing import Optional

import config
from logger import get_logger

log = get_logger(__name__)


class RiskEngine:
    """Stateless calculator for order sizes and position-count limits."""

    def __init__(
        self,
        max_positions: Optional[int] = None,
        max_risk_per_trade: Optional[float] = None,
        max_total_exposure_pct: Optional[float] = None,
        stop_loss_pct: Optional[float] = None,
        min_notional_usd: Optional[float] = None,
        qty_precision: Optional[int] = None,
    ) -> None:
        self.max_positions = int(max_positions or config.MAX_POSITIONS)
        self.max_risk_per_trade = float(
            max_risk_per_trade if max_risk_per_trade is not None else config.MAX_RISK_PER_TRADE
        )
        self.max_total_exposure_pct = float(
            max_total_exposure_pct
            if max_total_exposure_pct is not None
            else config.MAX_TOTAL_EXPOSURE_PCT
        )
        self.stop_loss_pct = float(
            stop_loss_pct if stop_loss_pct is not None else config.STOP_LOSS_PCT
        )
        self.min_notional_usd = float(
            min_notional_usd if min_notional_usd is not None else config.MIN_NOTIONAL_USD
        )
        self.qty_precision = int(qty_precision or config.QTY_PRECISION)

    # ------------------------------------------------------------------ #
    def can_open_position(self, current_open_positions: int) -> bool:
        """True while the number of open positions is below ``MAX_POSITIONS``."""
        try:
            open_count = int(current_open_positions)
        except (TypeError, ValueError):
            log.error("can_open_position received a non-numeric count: %r", current_open_positions)
            return False

        if open_count >= self.max_positions:
            log.info(
                "Position cap reached (%d/%d open). No new entries this cycle.",
                open_count, self.max_positions,
            )
            return False
        return True

    # ------------------------------------------------------------------ #
    def calculate_order_qty(
        self,
        account_cash: float,
        current_price: float,
        current_exposure_dollars: float = 0.0,
        available_buying_power: Optional[float] = None,
    ) -> float:
        """
        Compute the quantity to buy, applying every risk cap.

        Parameters
        ----------
        account_cash : float
            Total account cash used as the risk/exposure base.
        current_price : float
            Latest price for the asset.
        current_exposure_dollars : float
            Dollar value already deployed across all open positions.
        available_buying_power : float, optional
            Live non-marginable buying power. Defaults to ``account_cash``.

        Returns
        -------
        float
            Quantity rounded to ``QTY_PRECISION`` decimals, or ``0.0`` when the
            trade must be skipped (reason is logged).
        """
        try:
            account_cash = float(account_cash)
            current_price = float(current_price)
            current_exposure_dollars = float(current_exposure_dollars or 0.0)
        except (TypeError, ValueError):
            log.error("calculate_order_qty received non-numeric inputs. Skipping trade.")
            return 0.0

        buying_power = (
            float(available_buying_power) if available_buying_power is not None else account_cash
        )

        if current_price <= 0:
            log.warning("SKIP: invalid current price (%.8f).", current_price)
            return 0.0
        if account_cash <= 0:
            log.warning("SKIP: account cash is %.2f - nothing to allocate.", account_cash)
            return 0.0
        if buying_power <= 0:
            log.warning("SKIP: available buying power is %.2f.", buying_power)
            return 0.0

        # 1) Risk-based notional -------------------------------------------
        risk_dollars = account_cash * self.max_risk_per_trade
        stop_distance_pct = self.stop_loss_pct
        if stop_distance_pct <= 0:
            log.error("SKIP: STOP_LOSS_PCT must be > 0 to size a position.")
            return 0.0
        risk_notional = risk_dollars / stop_distance_pct

        # 2) Portfolio exposure cap ----------------------------------------
        exposure_budget = account_cash * self.max_total_exposure_pct
        remaining_exposure = exposure_budget - current_exposure_dollars
        if remaining_exposure <= 0:
            log.info(
                "SKIP: total exposure cap reached (deployed $%.2f of $%.2f budget, %.0f%% of cash).",
                current_exposure_dollars, exposure_budget, self.max_total_exposure_pct * 100,
            )
            return 0.0

        # 3) Buying power cap ----------------------------------------------
        # Keep a small 0.5% buffer so fees/price drift cannot reject the order.
        spendable = buying_power * 0.995

        notional = min(risk_notional, remaining_exposure, spendable)

        log.debug(
            "Sizing: risk_notional=$%.2f remaining_exposure=$%.2f spendable=$%.2f -> $%.2f",
            risk_notional, remaining_exposure, spendable, notional,
        )

        # 4) Minimum notional ----------------------------------------------
        if notional < self.min_notional_usd:
            log.info(
                "SKIP: computed position size $%.2f is below MIN_NOTIONAL_USD $%.2f "
                "(risk=$%.2f, exposure headroom=$%.2f, buying power=$%.2f). "
                "Not worth the fees on this account size.",
                notional, self.min_notional_usd, risk_notional, remaining_exposure, spendable,
            )
            return 0.0

        # 5) Precision ------------------------------------------------------
        qty = round(notional / current_price, self.qty_precision)

        if qty <= 0:
            log.info(
                "SKIP: quantity rounds to zero at %d decimals "
                "(notional $%.2f / price %.2f). Asset is too expensive for this size.",
                self.qty_precision, notional, current_price,
            )
            return 0.0

        # Rounding up can push the notional past a cap - verify post-rounding.
        rounded_notional = qty * current_price
        if rounded_notional > spendable or rounded_notional > remaining_exposure:
            qty = round(
                (min(spendable, remaining_exposure) / current_price) - 10 ** (-self.qty_precision),
                self.qty_precision,
            )
            rounded_notional = qty * current_price
            if qty <= 0 or rounded_notional < self.min_notional_usd:
                log.info(
                    "SKIP: after precision rounding the order ($%.2f) no longer satisfies "
                    "the caps / minimum notional.", max(rounded_notional, 0.0),
                )
                return 0.0

        log.info(
            "Sized order: qty=%.4f @ %.4f = $%.2f (risk $%.2f = %.2f%% of $%.2f cash, "
            "exposure %.2f%% -> %.2f%% of cap)",
            qty, current_price, rounded_notional, risk_dollars,
            self.max_risk_per_trade * 100, account_cash,
            (current_exposure_dollars / account_cash * 100) if account_cash else 0.0,
            ((current_exposure_dollars + rounded_notional) / account_cash * 100)
            if account_cash else 0.0,
        )
        return qty

    # ------------------------------------------------------------------ #
    def exposure_headroom(self, account_cash: float, current_exposure_dollars: float) -> float:
        """Dollars still available under the total-exposure cap."""
        try:
            return max(
                0.0,
                float(account_cash) * self.max_total_exposure_pct - float(current_exposure_dollars),
            )
        except (TypeError, ValueError):
            return 0.0

    def stop_loss_price(self, entry_price: float) -> float:
        return float(entry_price) * (1.0 - self.stop_loss_pct)

    @staticmethod
    def take_profit_price(entry_price: float) -> float:
        return float(entry_price) * (1.0 + config.TAKE_PROFIT_PCT)
