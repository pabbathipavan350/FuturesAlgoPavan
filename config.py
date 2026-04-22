# ============================================================
# CONFIG.PY — Nifty 9:29–9:45 VWAP Touch Strategy v5
# ============================================================
# Window  : 9:29 AM to 9:45 AM only
# Signal  : Nifty Futures comes within 5pts of VWAP
# CE trade: price above VWAP -> buy ITM CE
# PE trade: price below VWAP -> buy ITM PE
# SL      : -10 pts (option premium)
# Target  : +40 pts (option premium)
# Trail   : +10->BE | +20->lock+10 | +30->lock+20
# Entries : Multiple (each fresh zone-touch after price leaves)
# ============================================================

import os

def _load_env(path=".env"):
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val

_load_env()

# ── Mode ──────────────────────────────────────────────────
PAPER_TRADE         = True

# ── Kotak Neo Credentials (from .env) ─────────────────────
KOTAK_CONSUMER_KEY    = os.getenv("KOTAK_CONSUMER_KEY",    "")
KOTAK_CONSUMER_SECRET = os.getenv("KOTAK_CONSUMER_SECRET", "")
KOTAK_MOBILE_NUMBER   = os.getenv("KOTAK_MOBILE_NUMBER",   "")
KOTAK_UCC             = os.getenv("KOTAK_UCC",             "")
KOTAK_MPIN            = os.getenv("KOTAK_MPIN",            "")
KOTAK_ENVIRONMENT     = os.getenv("KOTAK_ENVIRONMENT",     "prod")

# ── Capital & Position Sizing ─────────────────────────────
TOTAL_CAPITAL       = 500000
LOTS                = 5
LOT_SIZE            = 65
INITIAL_CAPITAL     = TOTAL_CAPITAL

# ── Session Window ────────────────────────────────────────
MARKET_OPEN         = "09:15"
ENTRY_WINDOW_START  = "09:29"   # First possible entry
ENTRY_WINDOW_END    = "09:45"   # No NEW entries after this
SQUARE_OFF_TIME     = "15:25"   # Square off any open position
EXPIRY_DAY_CUTOFF   = "14:30"

# ── VWAP Touch Zone ───────────────────────────────────────
# A signal fires when |futures_ltp - vwap| <= VWAP_TOUCH_DIST
# Direction: ltp > vwap -> CE   |   ltp < vwap -> PE
# ltp == vwap (within 1pt) -> skip (too close to call direction)
VWAP_TOUCH_DIST     = 5.0    # pts from VWAP to trigger entry
VWAP_DIRECTION_MIN  = 0.5    # min dist from VWAP to establish direction

# Reset guard: after a signal fires, price must move at least
# VWAP_RESET_DIST away from VWAP before the zone is "armed" again.
# Prevents repeated signals while price is hugging VWAP.
VWAP_RESET_DIST     = 8.0    # pts away from VWAP to re-arm zone

# Minimum ticks before VWAP is trusted
VWAP_MIN_TICKS      = 3

# ── Budget Cap ────────────────────────────────────────────
# Maximum cost PER LOT (not total across all lots).
# cost per lot = option_ltp  x  LOT_SIZE
# With LOT_SIZE = 65:  25000 / 65 = 384.6 pts max LTP.
# pick_strike() filters out any strike whose LTP exceeds this.
# main.py re-checks the live LTP before placing the order as a safety net.
MAX_OPTION_COST_PER_LOT_RS = 25000   # Rs — budget per lot
# Derived max option LTP (auto-adjusts if LOT_SIZE ever changes):
#   max_ltp = MAX_OPTION_COST_PER_LOT_RS / LOT_SIZE = 25000 / 65 = ~384 pts

# ── Stop Loss & Target (option premium) ───────────────────
SL_PTS              = 10.0   # stop loss in option premium points
TARGET_PTS          = 40.0   # full exit target

# ── Trailing SL Ladder ────────────────────────────────────
# Option profit milestone -> new SL offset from entry
# +10 pts  -> SL = entry        (breakeven)
# +20 pts  -> SL = entry + 10   (lock +10)
# +30 pts  -> SL = entry + 20   (lock +20)
TRAIL_1_TRIGGER     = 10.0
TRAIL_1_LOCK        = 0.0    # breakeven

TRAIL_2_TRIGGER     = 20.0
TRAIL_2_LOCK        = 10.0   # lock +10

TRAIL_3_TRIGGER     = 30.0
TRAIL_3_LOCK        = 20.0   # lock +20

# ── Daily Guards ──────────────────────────────────────────
MAX_DAILY_TRADES        = 3       # hard cap — no new entries after 3 trades
MAX_CONSEC_SL           = 3
MAX_DAILY_LOSS_RS       = -15000

# ── Strike pre-loading ────────────────────────────────────
PRELOAD_ITM_MIN         = 50
PRELOAD_ITM_MAX         = 500
PRELOAD_STEP            = 50

# ── Strike selection ──────────────────────────────────────
MIN_DELTA               = 0.80
MIN_OI                  = 1200000
STRIKE_STEP             = 50
MAX_OI_WALK_STEPS       = 8
MAX_ITM_DEPTH_PTS       = 350
IV_PCT                  = 12.0
RISK_FREE_RATE          = 0.065

# ── Order execution ───────────────────────────────────────
BUY_LIMIT_BUFFER        = 2.0
ORDER_STATUS_POLL_SECS  = 1.0
ORDER_FILL_TIMEOUT_SECS = 15
EXIT_FILL_TIMEOUT_SECS  = 12
EXIT_RETRY_ATTEMPTS     = 3
ENABLE_AMO_OUTSIDE_HOURS = True

# ── Costs ─────────────────────────────────────────────────
BROKERAGE_PER_ORDER = 20.0
STT_PCT             = 0.0005
EXCHANGE_TXN_PCT    = 0.00053
SEBI_PCT            = 0.000001
GST_PCT             = 0.18
STAMP_DUTY_PCT      = 0.00003

# ── Segments & tokens ─────────────────────────────────────
FO_SEGMENT          = "nse_fo"
CM_SEGMENT          = "nse_cm"
NIFTY_INDEX_TOKEN   = "26000"

# ── Files ─────────────────────────────────────────────────
CAPITAL_FILE        = "capital.json"
CAPITAL_BACKUP_DAYS = 30
TRADE_LOG_FILE      = "reports/trade_log.csv"

# ── Legacy aliases (so option_manager / report_manager compile) ───
VIX_HIGH_THRESHOLD       = 15.0
SL_PTS_HIGH_VIX          = SL_PTS
TARGET_PTS_HIGH_VIX      = TARGET_PTS
SL_PTS_LOW_VIX           = SL_PTS
TARGET_PTS_LOW_VIX       = TARGET_PTS
NORMAL_SL_PTS            = SL_PTS
NORMAL_TARGET_PTS        = TARGET_PTS
EARLY_SL_PTS             = SL_PTS
EARLY_TARGET_PTS         = TARGET_PTS
EARLY_SESSION_END        = "09:40"
EARLY_SESSION_MAX_TRADES = 99
EARLY_LOSS_WAIT_MINS     = 0
EARLY_TRAIL_1_TRIGGER    = TRAIL_1_TRIGGER
EARLY_TRAIL_1_LOCK       = TRAIL_1_LOCK
EARLY_TRAIL_2_TRIGGER    = TRAIL_2_TRIGGER
EARLY_TRAIL_2_LOCK       = TRAIL_2_LOCK
NORMAL_TRAIL_1_TRIGGER   = TRAIL_1_TRIGGER
NORMAL_TRAIL_1_LOCK      = TRAIL_1_LOCK
NORMAL_TRAIL_2_TRIGGER   = TRAIL_2_TRIGGER
NORMAL_TRAIL_2_LOCK      = TRAIL_2_LOCK
NORMAL_TRAIL_3_TRIGGER   = TRAIL_3_TRIGGER
NORMAL_TRAIL_3_LOCK      = TRAIL_3_LOCK
NORMAL_TRAIL_4_TRIGGER   = TRAIL_3_TRIGGER
NORMAL_TRAIL_4_LOCK      = TRAIL_3_LOCK
NORMAL_TRAIL_STEP_START  = 40.0
NORMAL_TRAIL_STEP_SIZE   = 5.0
TRAIL_BREAKEVEN_TRIGGER  = TRAIL_1_TRIGGER
TRAIL_LOCK_TRIGGER       = TRAIL_2_TRIGGER
TRAIL_LOCK_PTS           = TRAIL_2_LOCK
FRESH_CROSS_MAX_DIST     = 5.0
PULLBACK_MAX_DIST        = VWAP_TOUCH_DIST
PULLBACK_MIN_DIST        = VWAP_DIRECTION_MIN
VWAP_TREND_LOOKBACK      = 3
VWAP_TREND_MIN_CHANGE    = 0.5
VWAP_TREND_START         = "09:16"
MAX_PULLBACK_PER_DIR     = 99
ENABLE_OPTION_VWAP_CONFIRM = False
DIRECTION_FATIGUE_COUNT  = 999
DIRECTION_COOLDOWN_MINS  = 0
FATIGUE_MIN_PROFIT_PTS   = 10.0
CROSS_CONFIRM_TICKS      = 2
