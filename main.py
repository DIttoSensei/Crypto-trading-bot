#!/usr/bin/env python
"""
main.py
-------
24/7 entry point for the Alpaca crypto trading bot.

Cycle (every LOOP_INTERVAL_SECONDS, default 900s = 15 minutes):

    1. Heartbeat log (account cash, equity, open positions, exposure).
    2. Reconcile local state against Alpaca's live positions.
    3. For each open position: check take-profit / stop-loss, sell if triggered,
       recording the ACTUAL average fill price.
    4. For each watchlist symbol (while below MAX_POSITIONS and under the
       exposure cap): analyze, size, buy, persist actual fill.
    5. Sleep. Any exception is logged and the loop continues.

Take-profit (+5%) and stop-loss (-2%) are enforced HERE in Python because
Alpaca rejects bracket/OCO order classes for crypto assets. Polling every 15
minutes means a fast move can gap straight through the stop level - see the
"Known risks" section of README.md.

Run modes::

    python main.py              # 24/7 loop (default)
    python main.py --once       # run exactly one cycle then exit
    python main.py --cycles 4   # run four cycles then exit
    python main.py --interval 300

``--once`` exists so the bot can run on a free external scheduler (e.g. GitHub
Actions cron) instead of a paid always-on host. Statelessness is safe there
because every run reconciles against Alpaca first and the broker is the source
of truth.
"""

import argparse
import math
import signal
import sys
import time

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import config
from alpaca_execution import AlpacaExecutionClient
from logger import get_logger
from market_data import CryptoDataFetcher
from risk_engine import RiskEngine
from state_manager import StateManager
from strategy import TechnicalStrategy

log = get_logger(__name__)

_SHUTDOWN = False


def _handle_signal(signum, _frame) -> None:
    """Flip the shutdown flag so the loop exits cleanly between cycles."""
    global _SHUTDOWN
    _SHUTDOWN = True
    log.warning("Received signal %s - finishing the current cycle then exiting.", signum)


class TradingBot:
    """Wires the strategy, risk engine, execution client and state together."""

    def __init__(self) -> None:
        config.validate_config(require_keys=True)

        self.state = StateManager()
        self.data = CryptoDataFetcher()
        self.strategy = TechnicalStrategy()
        self.risk = RiskEngine()
        self.broker = AlpacaExecutionClient()
        self.cycle = 0

        log.info("Bot initialised. %s", config.config_summary())

    # ------------------------------------------------------------------ #
    # Startup
    # ------------------------------------------------------------------ #
    def startup_reconcile(self) -> Dict[str, Any]:
        """Align positions.json with the broker before any trading happens."""
        log.info("Startup reconciliation: querying Alpaca for live positions...")
        broker_positions = self.broker.get_all_open_positions()

        for pos in broker_positions:
            log.info(
                "  Broker position: %s qty=%s avg_entry=%s market_value=%s unrealised_pl=%s",
                getattr(pos, "symbol", "?"),
                getattr(pos, "qty", "?"),
                getattr(pos, "avg_entry_price", "?"),
                getattr(pos, "market_value", "?"),
                getattr(pos, "unrealized_pl", "?"),
            )

        reconciled = self.state.reconcile_with_broker(broker_positions)

        cash = self.broker.get_account_balance()
        equity = self.broker.get_equity()
        log.info("Account ready: cash/buying power $%.2f | equity $%.2f | %d open position(s).",
                 cash, equity, len(reconciled))
        return reconciled

    # ------------------------------------------------------------------ #
    # Pricing helpers
    # ------------------------------------------------------------------ #
    def _price_map(self, symbols) -> Dict[str, float]:
        """Latest price for each symbol (missing symbols are simply absent)."""
        prices: Dict[str, float] = {}
        for symbol in symbols:
            price = self.data.get_latest_price(symbol)
            if price and price > 0:
                prices[symbol] = float(price)
            else:
                log.warning("No price available for %s this cycle.", symbol)
        return prices

    # ------------------------------------------------------------------ #
    # Exit management
    # ------------------------------------------------------------------ #
    def manage_open_positions(self, price_map: Dict[str, float]) -> None:
        """Check TP/SL for every tracked position and sell when triggered."""
        positions = self.state.load_positions()
        if not positions:
            log.info("No open positions to manage.")
            return

        for symbol, record in list(positions.items()):
            try:
                self._manage_single_position(symbol, record, price_map)
            except Exception as exc:  # noqa: BLE001
                log.exception("Error managing %s: %s", symbol, exc)

    def _manage_single_position(
        self,
        symbol: str,
        record: Dict[str, Any],
        price_map: Dict[str, float],
    ) -> None:
        qty = float(record.get("qty", 0.0) or 0.0)

        if qty <= 0:
            log.warning("Dropping malformed state record for %s: %r", symbol, record)
            self.state.remove_position(symbol)
            return

        price = price_map.get(symbol) or self.data.get_latest_price(symbol)
        if not price or price <= 0:
            log.warning("Skipping TP/SL check for %s - no current price.", symbol)
            return
        price = float(price)

        entry_price = float(record.get("entry_price", 0.0) or 0.0)
        if entry_price <= 0:
            # Broker reported an implausible (negative/zero) average entry.
            # Anchor TP/SL to the current market price so the position is
            # never left unprotected; once healed, the levels persist.
            log.warning(
                "%s has an invalid entry price (%.4f). Healing it with the "
                "current market price %.4f so TP/SL can be enforced.",
                symbol, entry_price, price,
            )
            entry_price = price
            take_profit = price * (1.0 + config.TAKE_PROFIT_PCT)
            stop_loss = price * (1.0 - config.STOP_LOSS_PCT)
            self.state.update_position(
                symbol,
                entry_price=entry_price,
                take_profit_price=take_profit,
                stop_loss_price=stop_loss,
                trailing_stop_price=stop_loss,
                highest_price=price,
                needs_healing=False,
            )
            record["entry_price"] = entry_price
            record["take_profit_price"] = take_profit
            record["stop_loss_price"] = stop_loss
        else:
            take_profit = float(record.get("take_profit_price")
                                or entry_price * (1.0 + config.TAKE_PROFIT_PCT))
            stop_loss = float(record.get("stop_loss_price")
                              or entry_price * (1.0 - config.STOP_LOSS_PCT))

        pnl_pct = (price / entry_price - 1.0) * 100.0
        log.info(
            "  %s: price=%.4f entry=%.4f P&L=%+.2f%% (TP=%.4f / SL=%.4f) qty=%.6f",
            symbol, price, entry_price, pnl_pct, take_profit, stop_loss, qty,
        )

        # --- Trailing stop ratchet ---------------------------------------- #
        # Once a position is in profit by TRAILING_ACTIVATE_PCT, the stop-loss
        # ratchets UP to stay TRAILING_STOP_PCT below the highest price seen.
        # Once the trailing stop has moved ABOVE the fixed take-profit level, it
        # SUPERSEDES the take-profit - the winner keeps running until price
        # falls back to the trailing level, instead of being capped at +5%.
        highest = float(record.get("highest_price", entry_price) or entry_price)
        if price > highest:
            highest = price
            self.state.update_position(symbol, highest_price=price)

        trailing_stop_price = float(record.get("trailing_stop_price") or stop_loss)
        trailing_armed = config.TRAILING_ACTIVATE_PCT > 0 and (
            highest / entry_price - 1.0 >= config.TRAILING_ACTIVATE_PCT
        )
        if trailing_armed:
            # Ratchet off the true high-water mark, never below the last trailed
            # level nor below the original hard stop.
            trailed = max(
                highest * (1.0 - config.TRAILING_STOP_PCT),
                stop_loss,
                trailing_stop_price,
            )
            if trailed > trailing_stop_price:
                trailing_stop_price = trailed
                if trailed > stop_loss:
                    stop_loss = trailed
                    log.info(
                        "  %s: trailing stop ratcheted to %.4f (%.2f%% below high %.4f).",
                        symbol, trailed, config.TRAILING_STOP_PCT * 100, highest,
                    )
                    self.state.update_position(symbol, trailing_stop_price=trailed)

        reason: Optional[str] = None
        # Once the trailing stop has locked in more than the fixed TP, it is the
        # only exit - sell when price falls back to it, never at the fixed TP.
        if trailing_stop_price >= take_profit:
            if price <= stop_loss:
                reason = "TRAILING_STOP"
        else:
            if price >= take_profit:
                reason = "TAKE_PROFIT"
            elif price <= stop_loss:
                reason = "STOP_LOSS"

        if reason is None:
            return

        log.info("%s TRIGGERED for %s at %.4f (%+.2f%% from entry). Submitting market sell.",
                 reason, symbol, price, pnl_pct)

        result = self.broker.execute_sell(symbol, qty)

        if not result["success"]:
            error = str(result.get("error", ""))
            if "no live position" in error:
                log.warning(
                    "%s is not held at Alpaca (already closed elsewhere). "
                    "Removing the stale local record.", symbol,
                )
                self.state.remove_position(symbol)
            else:
                log.error("Sell for %s failed (%s). Position stays open; will retry next cycle.",
                          symbol, error)
            return

        filled_qty = float(result["filled_qty"])
        fill_price = float(result["filled_avg_price"]) or price
        realised = (fill_price - entry_price) * filled_qty
        realised_pct = (fill_price / entry_price - 1.0) * 100.0

        log.info(
            "CLOSED %s via %s: sold %.8f @ %.6f | realised P&L $%.2f (%+.2f%%) "
            "| slippage vs level %.4f",
            symbol, reason, filled_qty, fill_price, realised, realised_pct,
            fill_price - (take_profit if reason == "TAKE_PROFIT" else stop_loss),
        )

        remaining = round(qty - filled_qty, config.QTY_PRECISION)
        if result.get("partial") and remaining > 0:
            log.warning(
                "PARTIAL EXIT on %s: %.8f still held. Keeping the record with the "
                "reduced quantity; the remainder is retried next cycle.",
                symbol, remaining,
            )
            self.state.update_position(symbol, qty=remaining)
        else:
            self.state.remove_position(symbol)

    # ------------------------------------------------------------------ #
    # Entry management
    # ------------------------------------------------------------------ #
    def scan_for_entries(self, price_map: Dict[str, float]) -> None:
        """Analyze the watchlist and open new positions where allowed."""
        positions = self.state.load_positions()
        open_count = len(positions)

        if not self.risk.can_open_position(open_count):
            return

        if not self.broker.is_account_tradable():
            log.error("Account is not tradable right now. Skipping entries this cycle.")
            return

        cash = self.broker.get_account_balance()
        if cash <= 0:
            log.warning("No available cash ($%.2f). Skipping entries.", cash)
            return

        exposure = self.state.total_exposure(price_map)
        headroom = self.risk.exposure_headroom(cash + exposure, exposure)
        log.info(
            "Entry scan: cash $%.2f | deployed $%.2f | exposure headroom $%.2f "
            "| %d/%d positions open",
            cash, exposure, headroom, open_count, config.MAX_POSITIONS,
        )

        if headroom < config.MIN_NOTIONAL_USD:
            log.info(
                "Exposure headroom $%.2f is below MIN_NOTIONAL_USD $%.2f. "
                "No further capital will be deployed this cycle.",
                headroom, config.MIN_NOTIONAL_USD,
            )
            return

        for symbol in config.WATCHLIST:
            if _SHUTDOWN:
                return

            if not self.risk.can_open_position(open_count):
                break

            if symbol in self.state.load_positions():
                log.debug("  %s: already held, skipping.", symbol)
                continue

            try:
                opened = self._try_open_position(symbol, cash, exposure)
            except Exception as exc:  # noqa: BLE001
                log.exception("Error evaluating %s: %s", symbol, exc)
                continue

            if opened:
                open_count += 1
                exposure += opened
                # Re-read live cash: it dropped by the notional just spent.
                cash = self.broker.get_account_balance()
                if self.risk.exposure_headroom(cash + exposure, exposure) < config.MIN_NOTIONAL_USD:
                    log.info("Exposure cap reached after entering %s. Ending entry scan.", symbol)
                    break

    def _try_open_position(self, symbol: str, cash: float, exposure: float) -> float:
        """
        Evaluate one symbol and buy if the signal and risk checks pass.

        Returns the notional deployed (0.0 when nothing was bought).
        """
        df = self.data.get_bars(symbol, config.BAR_TIMEFRAME, config.BAR_LIMIT)
        if df.empty:
            log.warning("  %s: no bars returned, skipping.", symbol)
            return 0.0

        signal = self.strategy.analyze_symbol(df)
        price = float(signal.get("current_price") or 0.0)

        rsi_txt = f"{signal['rsi']:.2f}" if signal.get("rsi") is not None else "n/a"
        sma_txt = f"{signal['sma']:.2f}" if signal.get("sma") is not None else "n/a"
        ema_txt = f"{signal['ema']:.2f}" if signal.get("ema") is not None else "n/a"
        log.info("  %s: %s | price=%.4f SMA=%s EMA=%s RSI=%s | %s",
                 symbol, signal["signal"], price, sma_txt, ema_txt, rsi_txt, signal["reason"])

        if signal["signal"] != "BUY" or price <= 0:
            return 0.0

        # Size against total capital (cash + already deployed) so the exposure
        # cap stays stable as cash is consumed by earlier entries.
        capital_base = cash + exposure
        qty = self.risk.calculate_order_qty(
            account_cash=capital_base,
            current_price=price,
            current_exposure_dollars=exposure,
            available_buying_power=cash,
        )

        if qty <= 0:
            # calculate_order_qty already logged the specific skip reason.
            return 0.0

        log.info("BUY SIGNAL on %s - submitting market order for %.4f units (~$%.2f).",
                 symbol, qty, qty * price)
        result = self.broker.execute_buy(symbol, qty)

        if not result["success"]:
            log.error("Buy for %s failed: %s", symbol, result.get("error"))
            return 0.0

        filled_qty = float(result["filled_qty"])
        fill_price = float(result["filled_avg_price"]) or price

        if filled_qty <= 0 or fill_price <= 0:
            log.error("Buy for %s reported no usable fill data. State unchanged.", symbol)
            return 0.0

        # Persist the ACTUAL fill, not the request - partial fills included.
        self.state.save_position(
            symbol=symbol,
            entry_price=fill_price,
            qty=round(filled_qty, config.QTY_PRECISION),
            take_profit_price=fill_price * (1.0 + config.TAKE_PROFIT_PCT),
            stop_loss_price=fill_price * (1.0 - config.STOP_LOSS_PCT),
            source="bot",
        )

        notional = filled_qty * fill_price
        log.info(
            "OPENED %s: qty=%.8f @ %.6f ($%.2f) | TP %.4f (+%.2f%%) / SL %.4f (-%.2f%%)",
            symbol, filled_qty, fill_price, notional,
            fill_price * (1.0 + config.TAKE_PROFIT_PCT), config.TAKE_PROFIT_PCT * 100,
            fill_price * (1.0 - config.STOP_LOSS_PCT), config.STOP_LOSS_PCT * 100,
        )
        return notional

    # ------------------------------------------------------------------ #
    # Cycle
    # ------------------------------------------------------------------ #
    def run_cycle(self) -> None:
        self.cycle += 1
        now = datetime.now(timezone.utc)

        log.info("=" * 78)
        log.info("HEARTBEAT cycle #%d | %s UTC", self.cycle, now.strftime("%Y-%m-%d %H:%M:%S"))

        # 1) Broker is the source of truth every cycle, not just at boot.
        broker_positions = self.broker.get_all_open_positions()
        positions = self.state.reconcile_with_broker(broker_positions)

        symbols = sorted(set(list(positions.keys()) + list(config.WATCHLIST)))
        price_map = self._price_map(symbols)

        cash = self.broker.get_account_balance()
        equity = self.broker.get_equity()
        exposure = self.state.total_exposure(price_map)
        log.info(
            "Account: cash $%.2f | equity $%.2f | deployed $%.2f (%.2f%% of capital) | "
            "positions %d/%d",
            cash, equity, exposure,
            (exposure / (cash + exposure) * 100.0) if (cash + exposure) > 0 else 0.0,
            len(positions), config.MAX_POSITIONS,
        )

        # 2) Exits before entries: free capital first.
        self.manage_open_positions(price_map)

        # 3) Entries.
        if not _SHUTDOWN:
            self.scan_for_entries(price_map)

        log.info("Cycle #%d complete.", self.cycle)

    def run_forever(self, max_cycles: Optional[int] = None,
                    interval: Optional[int] = None) -> int:
        """
        The trading loop. Errors are logged; the loop never dies on them.

        Parameters
        ----------
        max_cycles : int, optional
            Stop after this many cycles. ``None`` (default) means run forever.
            ``1`` gives the single-shot mode used by external schedulers.
        interval : int, optional
            Override ``config.LOOP_INTERVAL_SECONDS`` for this run.
        """
        interval = int(interval or config.LOOP_INTERVAL_SECONDS)

        try:
            self.startup_reconcile()
        except Exception as exc:  # noqa: BLE001
            log.exception("Startup reconciliation failed: %s", exc)
            log.error("Refusing to trade without a verified view of broker positions.")
            return 1

        if max_cycles == 1:
            log.info("Single-cycle mode (--once): running one pass then exiting.")
        elif max_cycles:
            log.info("Running %d cycle(s) at %ds intervals then exiting.", max_cycles, interval)
        else:
            log.info("Entering the 24/7 trading loop (interval %ds). Ctrl+C to stop.", interval)

        exit_code = 0

        while not _SHUTDOWN:
            started = time.time()
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                log.warning("KeyboardInterrupt - shutting down.")
                break
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                log.exception("Unhandled error in cycle #%d: %s", self.cycle, exc)
                # In one-shot mode there is no next cycle to recover in, so
                # surface the failure to the scheduler via the exit code.
                if max_cycles is not None:
                    exit_code = 1

            if _SHUTDOWN:
                break

            if max_cycles is not None and self.cycle >= max_cycles:
                log.info("Reached the requested cycle count (%d). Exiting.", max_cycles)
                break

            elapsed = time.time() - started
            sleep_for = max(1.0, interval - elapsed)
            log.info("Cycle took %.1fs. Sleeping %.0fs until the next check.", elapsed, sleep_for)

            # Sleep in short slices so Ctrl+C / SIGTERM is responsive.
            slept = 0.0
            while slept < sleep_for and not _SHUTDOWN:
                chunk = min(5.0, sleep_for - slept)
                try:
                    time.sleep(chunk)
                except KeyboardInterrupt:
                    log.warning("KeyboardInterrupt during sleep - shutting down.")
                    return exit_code
                slept += chunk

        log.info("Trading loop stopped after %d cycle(s). Open positions were left untouched "
                 "and remain recorded in %s.", self.cycle, config.POSITIONS_FILE)
        return exit_code


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Alpaca 24/7 crypto trading bot.",
        epilog="Use --once with an external scheduler (e.g. GitHub Actions cron) "
               "to run for free without an always-on host.",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single cycle then exit (equivalent to --cycles 1).",
    )
    parser.add_argument(
        "--cycles", type=int, default=None, metavar="N",
        help="Run N cycles then exit. Default: run forever.",
    )
    parser.add_argument(
        "--interval", type=int, default=None, metavar="SECONDS",
        help=f"Seconds between cycles. Default: {config.LOOP_INTERVAL_SECONDS}.",
    )
    args = parser.parse_args(argv)

    if args.once:
        args.cycles = 1
    if args.cycles is not None and args.cycles < 1:
        parser.error("--cycles must be at least 1")
    if args.interval is not None and args.interval < 1:
        parser.error("--interval must be at least 1 second")

    return args


def main(argv=None) -> int:
    args = parse_args(argv)

    signal.signal(signal.SIGINT, _handle_signal)

    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (AttributeError, ValueError):  # pragma: no cover - platform dependent
        pass

    log.info("#" * 78)
    log.info("ALPACA CRYPTO TRADING BOT starting up")
    log.info("Mode: %s | TP/SL are enforced in Python (Alpaca rejects crypto brackets)",
             "PAPER" if config.PAPER else "LIVE")
    log.info("Run: %s", "single cycle (--once)" if args.cycles == 1
             else f"{args.cycles} cycle(s)" if args.cycles else "continuous loop")
    log.info("#" * 78)

    try:
        bot = TradingBot()
    except RuntimeError as exc:
        log.error("Configuration error: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        log.exception("Failed to initialise the bot: %s", exc)
        return 1

    return bot.run_forever(max_cycles=args.cycles, interval=args.interval)



if __name__ == "__main__":
    sys.exit(main())
