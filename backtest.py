#!/usr/bin/env python
"""
backtest.py
-----------
Standalone walk-forward backtester for the EXACT strategy used live.

It replays historical bars one at a time, feeding only the data available up to
that bar into :class:`strategy.TechnicalStrategy`, and applies the same
take-profit / stop-loss rules that :mod:`main` enforces in the live loop.

Usage
-----
    python backtest.py --start 2024-01-01 --end 2025-01-01
    python backtest.py --start 2024-01-01 --end 2025-01-01 --symbols BTC/USD ETH/USD
    python backtest.py --start 2024-06-01 --end 2024-12-01 --timeframe 1Hour --capital 10000

Intrabar exit assumptions (deliberately conservative):
* If a bar's low pierces the stop, the stop fills at the stop price.
* If both the stop and the take-profit are touched inside the same bar, the
  STOP is assumed to trigger first (worst case).
* Fills use the bar close for entries; slippage/fees are configurable.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

import config
from logger import get_logger
from market_data import CryptoDataFetcher
from strategy import TechnicalStrategy

log = get_logger(__name__)

DEFAULT_FEE_PCT = 0.0025      # Alpaca crypto taker fee tier ~0.25%
DEFAULT_SLIPPAGE_PCT = 0.0005  # 5 bps of assumed slippage per side


# --------------------------------------------------------------------------- #
# Core engine
# --------------------------------------------------------------------------- #
def run_backtest(
    symbol: str,
    start_date: str,
    end_date: str,
    timeframe: Optional[str] = None,
    starting_capital: float = 10000.0,
    position_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
    stop_loss_pct: Optional[float] = None,
    fee_pct: float = DEFAULT_FEE_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
    fetcher: Optional[CryptoDataFetcher] = None,
    strategy: Optional[TechnicalStrategy] = None,
) -> Dict[str, Any]:
    """
    Walk-forward backtest of the live strategy for a single symbol.

    Parameters
    ----------
    symbol : str
        e.g. ``'BTC/USD'``.
    start_date, end_date : str
        ISO dates (``YYYY-MM-DD``) interpreted as UTC.
    position_pct : float, optional
        Fraction of *starting* capital committed per trade. Defaults to
        ``MAX_TOTAL_EXPOSURE_PCT / MAX_POSITIONS`` so it mirrors how the live
        exposure cap divides capital between concurrent positions.

    Returns
    -------
    dict
        Summary metrics plus the full trade list.
    """
    timeframe = timeframe or config.BAR_TIMEFRAME
    take_profit_pct = config.TAKE_PROFIT_PCT if take_profit_pct is None else take_profit_pct
    stop_loss_pct = config.STOP_LOSS_PCT if stop_loss_pct is None else stop_loss_pct
    if position_pct is None:
        position_pct = config.MAX_TOTAL_EXPOSURE_PCT / max(1, config.MAX_POSITIONS)

    fetcher = fetcher or CryptoDataFetcher()
    strategy = strategy or TechnicalStrategy()

    warmup_bars = strategy.min_bars_required
    # Pull extra history before `start_date` so indicators are already warm on
    # the first tradable bar (no look-ahead: this data precedes the test window).
    warmup_minutes = _timeframe_minutes(timeframe) * warmup_bars * 2
    fetch_start = _parse_date(start_date) - timedelta(minutes=warmup_minutes)

    log.info("Backtesting %s | %s -> %s | timeframe=%s", symbol, start_date, end_date, timeframe)
    df = fetcher.get_bars_range(symbol, timeframe, fetch_start, _parse_date(end_date))

    result = _empty_result(symbol, start_date, end_date, timeframe, starting_capital)

    if df is None or df.empty:
        log.error("No historical data for %s - skipping.", symbol)
        result["error"] = "no data"
        return result

    df = df.reset_index(drop=True)
    enriched = strategy.add_indicators(df)
    window_start = _parse_date(start_date)

    # First index inside the requested window that also has warm indicators.
    tradable_idx = enriched.index[
        (enriched["timestamp"] >= window_start)
        & enriched["sma"].notna()
        & enriched["ema"].notna()
        & enriched["rsi"].notna()
    ]
    if len(tradable_idx) == 0:
        log.error("%s: not enough warm-up history to evaluate the requested window.", symbol)
        result["error"] = "insufficient history for indicator warm-up"
        return result

    first_idx = int(tradable_idx[0])

    equity = float(starting_capital)
    peak_equity = equity
    max_drawdown = 0.0
    equity_curve: List[Dict[str, Any]] = []
    trades: List[Dict[str, Any]] = []

    position: Optional[Dict[str, Any]] = None

    for i in range(first_idx, len(enriched)):
        bar = enriched.iloc[i]
        ts = bar["timestamp"]
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])

        # ---------------- 1. Manage an open position (TP / SL first) -------
        if position is not None:
            exit_price = None
            reason = None

            # Worst case ordering: assume the stop is hit before the target when
            # a single bar spans both levels.
            if low <= position["stop_loss_price"]:
                exit_price = position["stop_loss_price"]
                reason = "STOP_LOSS"
            elif high >= position["take_profit_price"]:
                exit_price = position["take_profit_price"]
                reason = "TAKE_PROFIT"

            if exit_price is not None:
                fill = exit_price * (1.0 - slippage_pct)
                proceeds = position["qty"] * fill
                proceeds -= proceeds * fee_pct
                pnl = proceeds - position["cost_basis"]
                equity += pnl

                trades.append({
                    "symbol": symbol,
                    "entry_time": position["entry_time"],
                    "exit_time": ts,
                    "entry_price": position["entry_price"],
                    "exit_price": fill,
                    "qty": position["qty"],
                    "pnl": pnl,
                    "pnl_pct": (pnl / position["cost_basis"]) if position["cost_basis"] else 0.0,
                    "reason": reason,
                    "bars_held": i - position["entry_index"],
                })
                log.debug("%s %s @ %.4f (pnl=$%.2f)", symbol, reason, fill, pnl)
                position = None

        # ---------------- 2. Look for a new entry -------------------------
        if position is None:
            # Only bars up to and including `i` are visible: no look-ahead.
            signal = strategy.analyze_symbol(df.iloc[: i + 1])

            if signal["signal"] == "BUY":
                notional = min(starting_capital * position_pct, equity)
                if notional >= config.MIN_NOTIONAL_USD and close > 0:
                    entry_fill = close * (1.0 + slippage_pct)
                    qty = round(notional / entry_fill, config.QTY_PRECISION)
                    if qty > 0:
                        cost_basis = qty * entry_fill
                        cost_basis += cost_basis * fee_pct
                        if cost_basis <= equity:
                            position = {
                                "entry_index": i,
                                "entry_time": ts,
                                "entry_price": entry_fill,
                                "qty": qty,
                                "cost_basis": cost_basis,
                                "take_profit_price": entry_fill * (1.0 + take_profit_pct),
                                "stop_loss_price": entry_fill * (1.0 - stop_loss_pct),
                            }
                            log.debug("%s BUY %.6f @ %.4f (RSI=%.1f)",
                                      symbol, qty, entry_fill, signal["rsi"])

        # ---------------- 3. Mark-to-market equity ------------------------
        open_value = 0.0
        if position is not None:
            open_value = position["qty"] * close - position["cost_basis"]
        mark = equity + open_value

        peak_equity = max(peak_equity, mark)
        if peak_equity > 0:
            max_drawdown = max(max_drawdown, (peak_equity - mark) / peak_equity)
        equity_curve.append({"timestamp": ts, "equity": mark})

    # ---------------- 4. Close any still-open position at the last close ---
    if position is not None:
        last_close = float(enriched.iloc[-1]["close"]) * (1.0 - slippage_pct)
        proceeds = position["qty"] * last_close
        proceeds -= proceeds * fee_pct
        pnl = proceeds - position["cost_basis"]
        equity += pnl
        trades.append({
            "symbol": symbol,
            "entry_time": position["entry_time"],
            "exit_time": enriched.iloc[-1]["timestamp"],
            "entry_price": position["entry_price"],
            "exit_price": last_close,
            "qty": position["qty"],
            "pnl": pnl,
            "pnl_pct": (pnl / position["cost_basis"]) if position["cost_basis"] else 0.0,
            "reason": "END_OF_TEST",
            "bars_held": len(enriched) - 1 - position["entry_index"],
        })

    return _summarise(
        symbol, start_date, end_date, timeframe, starting_capital,
        equity, trades, equity_curve, max_drawdown, len(enriched) - first_idx,
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _parse_date(value: str) -> datetime:
    ts = pd.Timestamp(str(value))
    dt = ts.to_pydatetime()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _timeframe_minutes(timeframe: str) -> int:
    raw = str(timeframe).strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    letters = "".join(ch for ch in raw if ch.isalpha()).lower()
    amount = int(digits) if digits else 1
    if letters.startswith("min") or letters in ("m", "t"):
        return amount
    if letters.startswith("h"):
        return amount * 60
    if letters.startswith("d"):
        return amount * 60 * 24
    if letters.startswith("w"):
        return amount * 60 * 24 * 7
    return amount


def _empty_result(symbol, start_date, end_date, timeframe, starting_capital) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "start": start_date,
        "end": end_date,
        "timeframe": timeframe,
        "starting_capital": starting_capital,
        "ending_capital": starting_capital,
        "total_return_pct": 0.0,
        "total_pnl": 0.0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate_pct": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "profit_factor": 0.0,
        "max_drawdown_pct": 0.0,
        "avg_bars_held": 0.0,
        "tp_exits": 0,
        "sl_exits": 0,
        "bars_tested": 0,
        "trade_log": [],
        "error": None,
    }


def _summarise(symbol, start_date, end_date, timeframe, starting_capital,
               equity, trades, equity_curve, max_drawdown, bars_tested) -> Dict[str, Any]:
    result = _empty_result(symbol, start_date, end_date, timeframe, starting_capital)

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    result.update({
        "ending_capital": equity,
        "total_pnl": equity - starting_capital,
        "total_return_pct": ((equity / starting_capital - 1.0) * 100.0) if starting_capital else 0.0,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (len(wins) / len(trades) * 100.0) if trades else 0.0,
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (
            float("inf") if gross_win > 0 else 0.0
        ),
        "max_drawdown_pct": max_drawdown * 100.0,
        "avg_bars_held": (sum(t["bars_held"] for t in trades) / len(trades)) if trades else 0.0,
        "tp_exits": sum(1 for t in trades if t["reason"] == "TAKE_PROFIT"),
        "sl_exits": sum(1 for t in trades if t["reason"] == "STOP_LOSS"),
        "bars_tested": max(0, bars_tested),
        "trade_log": trades,
        "equity_curve": equity_curve,
    })
    return result


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _fmt_pf(value: float) -> str:
    if value == float("inf"):
        return "inf"
    return f"{value:.2f}"


def print_symbol_table(results: List[Dict[str, Any]]) -> None:
    header = (
        f"{'SYMBOL':<10} {'TRADES':>7} {'WIN%':>7} {'RETURN%':>9} {'PNL $':>11} "
        f"{'MAXDD%':>8} {'PF':>6} {'TP':>4} {'SL':>4} {'BARS':>7}"
    )
    print("\n" + "=" * len(header))
    print("PER-SYMBOL BACKTEST RESULTS")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for r in results:
        if r.get("error"):
            print(f"{r['symbol']:<10} {'-':>7} {'-':>7} {'-':>9} {'-':>11} "
                  f"{'-':>8} {'-':>6} {'-':>4} {'-':>4}   ({r['error']})")
            continue
        print(
            f"{r['symbol']:<10} {r['trades']:>7} {r['win_rate_pct']:>6.1f}% "
            f"{r['total_return_pct']:>8.2f}% {r['total_pnl']:>11.2f} "
            f"{r['max_drawdown_pct']:>7.2f}% {_fmt_pf(r['profit_factor']):>6} "
            f"{r['tp_exits']:>4} {r['sl_exits']:>4} {r['bars_tested']:>7}"
        )
    print("=" * len(header))


def print_aggregate(results: List[Dict[str, Any]], starting_capital: float) -> Dict[str, Any]:
    usable = [r for r in results if not r.get("error")]
    all_trades = [t for r in usable for t in r["trade_log"]]

    wins = [t for t in all_trades if t["pnl"] > 0]
    losses = [t for t in all_trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    total_pnl = sum(r["total_pnl"] for r in usable)
    capital_deployed = starting_capital * max(1, len(usable))

    aggregate = {
        "symbols_tested": len(usable),
        "trades": len(all_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (len(wins) / len(all_trades) * 100.0) if all_trades else 0.0,
        "total_pnl": total_pnl,
        "total_return_pct": (total_pnl / capital_deployed * 100.0) if capital_deployed else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (
            float("inf") if gross_win > 0 else 0.0
        ),
        "worst_max_drawdown_pct": max((r["max_drawdown_pct"] for r in usable), default=0.0),
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
        "tp_exits": sum(r["tp_exits"] for r in usable),
        "sl_exits": sum(r["sl_exits"] for r in usable),
    }

    print("\n" + "=" * 62)
    print("AGGREGATE SUMMARY")
    print("=" * 62)
    print(f"  Symbols tested        : {aggregate['symbols_tested']}")
    print(f"  Total trades          : {aggregate['trades']} "
          f"({aggregate['wins']}W / {aggregate['losses']}L)")
    print(f"  Win rate              : {aggregate['win_rate_pct']:.2f}%")
    print(f"  Total P&L             : ${aggregate['total_pnl']:.2f}")
    print(f"  Return on capital     : {aggregate['total_return_pct']:.2f}% "
          f"(${starting_capital:,.2f} per symbol)")
    print(f"  Profit factor         : {_fmt_pf(aggregate['profit_factor'])}")
    print(f"  Avg win / avg loss    : ${aggregate['avg_win']:.2f} / ${aggregate['avg_loss']:.2f}")
    print(f"  Worst max drawdown    : {aggregate['worst_max_drawdown_pct']:.2f}%")
    print(f"  Exits by TP / SL      : {aggregate['tp_exits']} / {aggregate['sl_exits']}")
    print("=" * 62)

    if aggregate["trades"] == 0:
        print("\n  NOTE: zero trades were generated. The entry filter "
              "(trend + RSI + EMA proximity) may be too strict for this window,\n"
              "  or the date range is too short. Widen the range or relax "
              "RSI_BUY_THRESHOLD / EMA_PROXIMITY_PCT before going live.")
    else:
        print("\n  Reminder: these results assume the stop fills exactly at the stop price.")
        print("  Live 15-minute polling can and will fill worse than that. Treat the")
        print("  numbers above as an optimistic ceiling, not an expectation.")
    print()

    return aggregate


def print_trade_log(results: List[Dict[str, Any]], limit: int = 25) -> None:
    trades = sorted(
        (t for r in results if not r.get("error") for t in r["trade_log"]),
        key=lambda t: str(t["entry_time"]),
    )
    if not trades:
        return
    print("\nTRADE LOG (most recent %d)" % min(limit, len(trades)))
    print("-" * 104)
    print(f"{'SYMBOL':<9} {'ENTRY (UTC)':<21} {'EXIT (UTC)':<21} {'ENTRY':>11} "
          f"{'EXIT':>11} {'PNL $':>10} {'PNL %':>8} {'REASON':<12}")
    print("-" * 104)
    for t in trades[-limit:]:
        print(
            f"{t['symbol']:<9} {str(t['entry_time'])[:19]:<21} {str(t['exit_time'])[:19]:<21} "
            f"{t['entry_price']:>11.4f} {t['exit_price']:>11.4f} {t['pnl']:>10.2f} "
            f"{t['pnl_pct'] * 100:>7.2f}% {t['reason']:<12}"
        )
    print("-" * 104)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _default_dates() -> tuple:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=180)
    return start.isoformat(), end.isoformat()


def main(argv: Optional[List[str]] = None) -> int:
    default_start, default_end = _default_dates()

    parser = argparse.ArgumentParser(
        description="Backtest the live crypto strategy against historical Alpaca bars.",
    )
    parser.add_argument("--start", default=default_start,
                        help=f"Start date YYYY-MM-DD (UTC). Default: {default_start}")
    parser.add_argument("--end", default=default_end,
                        help=f"End date YYYY-MM-DD (UTC). Default: {default_end}")
    parser.add_argument("--symbols", nargs="+", default=config.WATCHLIST,
                        help="Symbols to test. Default: the configured WATCHLIST.")
    parser.add_argument("--timeframe", default=config.BAR_TIMEFRAME,
                        help="Bar timeframe, e.g. 15Min, 1Hour, 1Day.")
    parser.add_argument("--capital", type=float, default=10000.0,
                        help="Simulated starting capital per symbol.")
    parser.add_argument("--position-pct", type=float, default=None,
                        help="Fraction of capital per trade. "
                             "Default: MAX_TOTAL_EXPOSURE_PCT / MAX_POSITIONS.")
    parser.add_argument("--fee-pct", type=float, default=DEFAULT_FEE_PCT,
                        help=f"Per-side fee fraction. Default: {DEFAULT_FEE_PCT}")
    parser.add_argument("--slippage-pct", type=float, default=DEFAULT_SLIPPAGE_PCT,
                        help=f"Per-side slippage fraction. Default: {DEFAULT_SLIPPAGE_PCT}")
    parser.add_argument("--show-trades", action="store_true",
                        help="Print the individual trade log.")
    args = parser.parse_args(argv)

    # API keys are optional here: Alpaca serves crypto market data anonymously.
    config.validate_config(require_keys=False)

    print("\n" + "=" * 62)
    print("CRYPTO STRATEGY BACKTEST")
    print("=" * 62)
    print(f"  Window        : {args.start} -> {args.end} (UTC)")
    print(f"  Timeframe     : {args.timeframe}")
    print(f"  Symbols       : {', '.join(args.symbols)}")
    print(f"  Capital/symbol: ${args.capital:,.2f}")
    print(f"  Entry         : close > SMA{config.SMA_PERIOD} AND "
          f"RSI{config.RSI_PERIOD} < {config.RSI_BUY_THRESHOLD} AND "
          f"|close-EMA{config.EMA_PERIOD}| <= {config.EMA_PROXIMITY_PCT:.2%}")
    print(f"  Exits         : TP +{config.TAKE_PROFIT_PCT:.2%} / SL -{config.STOP_LOSS_PCT:.2%}")
    print(f"  Costs         : fee {args.fee_pct:.4%}/side, slippage {args.slippage_pct:.4%}/side")
    print("=" * 62)

    fetcher = CryptoDataFetcher()
    strategy = TechnicalStrategy()

    results: List[Dict[str, Any]] = []
    for symbol in args.symbols:
        try:
            results.append(run_backtest(
                symbol=symbol,
                start_date=args.start,
                end_date=args.end,
                timeframe=args.timeframe,
                starting_capital=args.capital,
                position_pct=args.position_pct,
                fee_pct=args.fee_pct,
                slippage_pct=args.slippage_pct,
                fetcher=fetcher,
                strategy=strategy,
            ))
        except Exception as exc:  # noqa: BLE001
            log.exception("Backtest failed for %s: %s", symbol, exc)
            failed = _empty_result(symbol, args.start, args.end, args.timeframe, args.capital)
            failed["error"] = str(exc)
            results.append(failed)

    print_symbol_table(results)
    if args.show_trades:
        print_trade_log(results)
    print_aggregate(results, args.capital)

    return 0 if any(not r.get("error") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
