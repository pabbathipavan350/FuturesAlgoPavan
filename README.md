# Nifty Options VWAP Algo — v3

## What this algo does

Trades Nifty options intraday using the **Nifty futures VWAP** as the sole signal source.
The futures `ap` field (session VWAP from Kotak WebSocket) is tracked tick-by-tick.
When futures crosses VWAP — either a fresh cross or a pullback to VWAP — the algo
enters the corresponding ITM option (CE for above, PE for below).

No candle math. No indicators. No external data feeds. Just VWAP from the exchange.

---

## Architecture

```
Kotak WebSocket (futures token)
        │  ap = session VWAP, ltp = futures price
        ▼
FuturesVWAPEngine
  ├─ Cross detection (3-tick confirmation)
  ├─ Pullback detection (proximity zone)
  ├─ VWAP trend (per-minute snapshots)
  └─ check_signal() → (CE|PE|None, cross|pullback)
        │
        ▼
main.py _on_signal()
  ├─ Session guards (early / normal)
  ├─ VWAP trend filter (active from 9:30)
  ├─ Pullback limit (2/direction/day)
  ├─ Direction fatigue guard
  └─ Entry → OptionManager.pick_strike()
        │
        ▼
OptionManager
  ├─ Cached scrip master lookup
  ├─ Delta ≥ 0.85 + OI ≥ 12L filter
  └─ place_buy_order() / place_exit_order()
        │
        ▼
Kotak Order API
```

---

## Sessions and rules

### Early session — 9:16 to 9:39 AM

| Rule | Value |
|---|---|
| Max trades | 2 |
| SL | 7 pts |
| Trail at +25 | SL → entry + 5 |
| Trail at +35 | SL → entry + 25 |
| Book profit | +45 pts |
| After a loss | Wait 10 min from first entry before second |
| After a win | Second trade immediately |

### Normal session — 9:40 AM onward

| Profit trigger | SL moves to |
|---|---|
| +20 pts | Entry + 1 (near breakeven) |
| +30 pts | Entry + 10 |
| +35 pts | Entry + 20 |
| +40 pts | Entry + 25 |
| +45, +50, +55… | Trail every 5 pts continuously |
| Target | +50 pts (book profit) |
| SL (fixed) | −15 pts |

---

## Signal logic

### Fresh cross (confirmed)
Price crosses VWAP from below (CE) or above (PE). Signal only fires after **3 consecutive
ticks** remain on the new side. A single tick across and back = noise = ignored.

### Pullback
Price was above VWAP (CE) or below VWAP (PE), moves back within `PULLBACK_MAX_DIST` (2 pts)
of VWAP. One-shot per zone visit — resets when price leaves the zone.
Max 2 pullback entries per direction per day.

### VWAP trend filter (active from 9:30 AM)
Snapshots VWAP at the end of each minute. Compares oldest vs newest over 5-minute window.
- VWAP rising (+0.5 pts over 5 min) → CE only, PE signals blocked
- VWAP falling (−0.5 pts over 5 min) → PE only, CE signals blocked
- Flat → both directions allowed

Tracking starts at 9:15 so 15 minutes of history exist before the filter activates.

---

## Strike selection

1. Resolve weekly expiry (nearest Tuesday, or Monday if Tuesday is holiday)
2. ATM = round(futures spot / 50) × 50
3. Walk from 300 pts ITM toward ATM
4. Pick first strike with delta ≥ 0.85 and OI ≥ 12,00,000
5. If OI threshold not met after 8 steps, use best available (OI fallback)
6. Scrip master cached at startup — no repeated HTTP calls

---

## Files

| File | Purpose |
|---|---|
| `main.py` | Orchestrator — WS handling, signal dispatch, entry/exit |
| `futures_engine.py` | Cross/pullback detection, VWAP trend, tick confirmation |
| `option_manager.py` | Strike selection, order placement, scrip master cache |
| `config.py` | All tuneable parameters — edit here, not in code |
| `report_manager.py` | CSV trade log, daily text report, JSON history |
| `capital_manager.py` | Capital tracking, drawdown guard |
| `session_manager.py` | Kotak session keepalive (pings every 25 min) |
| `auth.py` | TOTP login, MPIN validation |
| `telegram_notifier.py` | Entry/exit/risk alerts via Telegram |
| `CHANGES.md` | Session-by-session change log |

---

## Config quick reference

```python
# Session boundaries
EARLY_SESSION_END        = "09:40"
EARLY_SESSION_MAX_TRADES = 2
EARLY_SL_PTS             = 7.0
EARLY_TARGET_PTS         = 45.0
EARLY_LOSS_WAIT_MINS     = 10

NORMAL_SL_PTS            = 15.0
NORMAL_TARGET_PTS        = 50.0

# Cross confirmation
CROSS_CONFIRM_TICKS      = 3     # ticks price must hold new side

# Entry zone
FRESH_CROSS_MAX_DIST     = 2.0   # max pts from VWAP at cross
PULLBACK_MAX_DIST        = 2.0   # pullback zone width

# VWAP trend
VWAP_TREND_LOOKBACK      = 5     # minutes
VWAP_TREND_MIN_CHANGE    = 0.5   # pts change to call it trending
VWAP_TREND_START         = "09:30"

# Pullback cap
MAX_PULLBACK_PER_DIR     = 2     # per day per direction
```

---

## Output files

| File | Contents |
|---|---|
| `reports/trade_log.csv` | One row per trade, 40+ columns — see CHANGES.md for column list |
| `reports/report_YYYYMMDD.txt` | Daily summary with exit breakdown, trail efficiency |
| `reports/daily_summary.json` | Historical daily P&L for last-10-days bar chart |
| `logs/algo_YYYYMMDD.log` | Full debug log — every tick event, signal, order |

---

## Running

```bash
# Paper mode (default — PAPER_TRADE = True in config.py)
python main.py

# Switch to live
# Set PAPER_TRADE = False in config.py
# Double-check all credentials in .env
```

---

## .env required keys

```
KOTAK_CONSUMER_KEY=...
KOTAK_CONSUMER_SECRET=...
KOTAK_MOBILE_NUMBER=...
KOTAK_UCC=...
KOTAK_MPIN=...
TOTP_SECRET_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```
