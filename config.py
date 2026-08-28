"""
config.py
---------
Central configuration module for the 24/7 Alpaca crypto trading bot.

All secrets are loaded from environment variables via python-dotenv.
NEVER hardcode API keys in this file.
"""

import os
from dotenv import load_dotenv

# Load variables from a local .env file (if present) into the process env.
load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_list(name: str, default: list) -> list:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return list(default)
    items = [item.strip().upper() for item in raw.split(",") if item.strip()]
    return items or list(default)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")

# Paper trading endpoint: https://paper-api.alpaca.markets
PAPER: bool = _get_bool("ALPACA_PAPER", True)

# ---------------------------------------------------------------------------
# Universe / timeframe
# ---------------------------------------------------------------------------
WATCHLIST: list = _get_list("WATCHLIST", ["BTC/USD", "ETH/USD", "SOL/USD"])
BAR_TIMEFRAME: str = os.getenv("BAR_TIMEFRAME", "15Min")
LOOP_INTERVAL_SECONDS: int = _get_int("LOOP_INTERVAL_SECONDS", 900)  # 15 minutes

# ---------------------------------------------------------------------------
# Strategy parameters
# ---------------------------------------------------------------------------
SMA_PERIOD: int = _get_int("SMA_PERIOD", 200)
EMA_PERIOD: int = _get_int("EMA_PERIOD", 20)
RSI_PERIOD: int = _get_int("RSI_PERIOD", 14)
RSI_BUY_THRESHOLD: float = _get_float("RSI_BUY_THRESHOLD", 42.0)
# Max distance from EMA_20 (as a fraction) for a valid "pullback" entry.
EMA_PROXIMITY_PCT: float = _get_float("EMA_PROXIMITY_PCT", 0.005)

# Number of bars pulled per analysis cycle (must comfortably exceed SMA_PERIOD).
BAR_LIMIT: int = _get_int("BAR_LIMIT", 250)

# ---------------------------------------------------------------------------
# Exit rules (monitored manually - Alpaca rejects bracket orders for crypto)
# ---------------------------------------------------------------------------
TAKE_PROFIT_PCT: float = _get_float("TAKE_PROFIT_PCT", 0.05)   # +5%
STOP_LOSS_PCT: float = _get_float("STOP_LOSS_PCT", 0.02)       # -2%
# Trailing stop: once a position is in profit by TRAILING_ACTIVATE_PCT, the
# stop ratchets UP to stay that many percent below the highest price seen.
# Set to 0.0 to disable trailing and rely on the fixed TP/SL only.
TRAILING_ACTIVATE_PCT: float = _get_float("TRAILING_ACTIVATE_PCT", 0.03)   # arm at +3%
TRAILING_STOP_PCT: float = _get_float("TRAILING_STOP_PCT", 0.02)           # trail by 2%

# ---------------------------------------------------------------------------
# Risk / capital management
# ---------------------------------------------------------------------------
MAX_POSITIONS: int = _get_int("MAX_POSITIONS", 2)
MAX_RISK_PER_TRADE: float = _get_float("MAX_RISK_PER_TRADE", 0.01)        # 1% of cash
MAX_TOTAL_EXPOSURE_PCT: float = _get_float("MAX_TOTAL_EXPOSURE_PCT", 0.20)  # 20% of cash
MIN_NOTIONAL_USD: float = _get_float("MIN_NOTIONAL_USD", 10.0)

# Crypto quantity precision required by Alpaca.
QTY_PRECISION: int = _get_int("QTY_PRECISION", 4)

# ---------------------------------------------------------------------------
# Order handling
# ---------------------------------------------------------------------------
ORDER_POLL_TIMEOUT_SECONDS: int = _get_int("ORDER_POLL_TIMEOUT_SECONDS", 60)
ORDER_POLL_INTERVAL_SECONDS: float = _get_float("ORDER_POLL_INTERVAL_SECONDS", 2.0)

# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
POSITIONS_FILE: str = os.getenv("POSITIONS_FILE", os.path.join(BASE_DIR, "positions.json"))
LOG_FILE: str = os.getenv("LOG_FILE", os.path.join(BASE_DIR, "crypto_bot.log"))
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()


def validate_config(require_keys: bool = True) -> None:
    """
    Validate configuration on boot.

    Raises
    ------
    RuntimeError
        If API credentials are missing or numeric parameters are nonsensical.
    """
    if require_keys:
        missing = []
        if not ALPACA_API_KEY:
            missing.append("ALPACA_API_KEY")
        if not ALPACA_SECRET_KEY:
            missing.append("ALPACA_SECRET_KEY")
        if missing:
            raise RuntimeError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill in your Alpaca paper "
                  "trading credentials before running the bot."
            )

    if not WATCHLIST:
        raise RuntimeError("WATCHLIST is empty. Configure at least one symbol, e.g. 'BTC/USD'.")

    if SMA_PERIOD <= 0 or EMA_PERIOD <= 0 or RSI_PERIOD <= 0:
        raise RuntimeError("SMA_PERIOD, EMA_PERIOD and RSI_PERIOD must all be positive integers.")

    if BAR_LIMIT <= SMA_PERIOD:
        raise RuntimeError(
            f"BAR_LIMIT ({BAR_LIMIT}) must be greater than SMA_PERIOD ({SMA_PERIOD}) "
            "so the trend filter can be computed."
        )

    if not (0 < STOP_LOSS_PCT < 1):
        raise RuntimeError("STOP_LOSS_PCT must be a fraction strictly between 0 and 1 (e.g. 0.02).")

    if not (0 < TAKE_PROFIT_PCT < 5):
        raise RuntimeError("TAKE_PROFIT_PCT must be a positive fraction (e.g. 0.05).")

    if TRAILING_ACTIVATE_PCT < 0:
        raise RuntimeError("TRAILING_ACTIVATE_PCT must be >= 0 (0 disables trailing).")
    if TRAILING_ACTIVATE_PCT > 0 and not (0 < TRAILING_STOP_PCT < 1):
        raise RuntimeError(
            "TRAILING_STOP_PCT must be a fraction between 0 and 1 (e.g. 0.02) "
            "when trailing is enabled."
        )

    if not (0 < MAX_RISK_PER_TRADE <= 1):
        raise RuntimeError("MAX_RISK_PER_TRADE must be a fraction between 0 and 1 (e.g. 0.01).")

    if not (0 < MAX_TOTAL_EXPOSURE_PCT <= 1):
        raise RuntimeError("MAX_TOTAL_EXPOSURE_PCT must be a fraction between 0 and 1 (e.g. 0.20).")

    if MAX_POSITIONS < 1:
        raise RuntimeError("MAX_POSITIONS must be at least 1.")

    if MIN_NOTIONAL_USD <= 0:
        raise RuntimeError("MIN_NOTIONAL_USD must be greater than 0.")


def config_summary() -> str:
    """Human readable, secret-free summary of the active configuration."""
    return (
        f"PAPER={PAPER} | WATCHLIST={WATCHLIST} | TIMEFRAME={BAR_TIMEFRAME} | "
        f"SMA={SMA_PERIOD} EMA={EMA_PERIOD} RSI={RSI_PERIOD}<{RSI_BUY_THRESHOLD} | "
        f"TP={TAKE_PROFIT_PCT:.2%} SL={STOP_LOSS_PCT:.2%} "
        f"TRAILING={'OFF' if TRAILING_ACTIVATE_PCT <= 0 else f'arm+{TRAILING_ACTIVATE_PCT:.2%} trail {TRAILING_STOP_PCT:.2%}'} | "
        f"MAX_POSITIONS={MAX_POSITIONS} RISK/TRADE={MAX_RISK_PER_TRADE:.2%} "
        f"MAX_EXPOSURE={MAX_TOTAL_EXPOSURE_PCT:.2%} MIN_NOTIONAL=${MIN_NOTIONAL_USD:.2f}"
    )
