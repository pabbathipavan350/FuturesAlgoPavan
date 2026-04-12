# CHANGES.md — Algo v3 session log

Tracks every meaningful change made across sessions:
what changed, why it was changed, and which files were affected.
Share this file in any new chat to restore full context.

---

## v3 baseline — session 1

### Core rewrite from v2

**Why:** v2 used option VWAP as signal. Option VWAP is noisy — wide spreads, thin volume,
delta effects all cause frequent false crosses. Futures VWAP is clean: index liquidity,
0.05pt spreads, represents the actual market.

**What changed:**
- Signal source: option VWAP → futures VWAP (`ap` field on futures token)
- Added `FuturesVWAPEngine` with cross + pullback detection
- Added `OptionVWAPTracker` for mid-trade reversal confirmation
- Fixed SL/target: −15pts / +50pts (high VIX), −10pts / +35pts (low VIX)
- `pick_strike()`: delta ≥ 0.85 + OI ≥ 12L, walk 8 steps, fallback if needed

**Files:** `futures_engine.py` (new), `main.py` (rewrite), `option_manager.py` (rewrite), `config.py`

---

## v3 — session 2: WS connect() fix

**Why:** `'NeoAPI' object has no attribute 'ws_connect'` on startup crash.

**Root cause:** Kotak Neo v2 SDK does not have `ws_connect()` or `connect()`.
Correct pattern: assign callbacks as attributes, call `subscribe()` to start WS.

**What changed:**
- `_setup_websocket()`: callbacks assigned as `client.on_message = ...` (correct v2 pattern)
- `run()`: calls `_subscribe_futures()` which triggers WS start
- `_on_reconnect()`: calls `_subscribe_futures()` after re-login

**Files:** `main.py`

---

## v3 — session 3: Double-subscribe crash fix

**Why:** `socket is already closed` / `NoneType has no attribute 'sock'` immediately after connect.

**Root cause:** `_subscribe_futures()` was called twice:
1. From `run()` to start WS
2. From `_on_ws_open` callback

Second `subscribe()` on already-open socket crashed it.

**What changed:**
- Removed `_subscribe_futures()` from `_on_ws_open` — it now only logs `[WS] Connected`
- Single call path: `run()` → `_subscribe_futures()` → WS opens → `_on_ws_open` logs

**Files:** `main.py`

---

## v3 — session 4: Ticks showing 0.00 (message parsing fix)

**Why:** WS connected but `Futures=0.00 VWAP=0.00 ticks=0` — no data.

**Root cause:** Kotak Neo v2 WS wraps ticks in a container:
```json
{ "type": "stock_feed", "data": [{ "tk": "66691", "ltP": 23400.5, "ap": 23398.2 }] }
```
The code was treating the wrapper as a tick, so `message.get("tk")` returned None always.

**What changed:**
- `_on_message()`: added type check (`stock_feed/sf`), unwrapped `message['data']`
- All tick field names updated to handle both `ltp` and `ltP` (v2 capitalisation)
- `fetch_oi()` and `_get_option_ltp()`: unwrap `resp['message'] or resp['data']`

**Files:** `main.py`, `futures_engine.py`, `option_manager.py`

---

## v3 — session 5: Wrong expiry (monthly → weekly)

**Why:** Algo picked strike at Rs 552 but live market had same strike at Rs 429 — Rs 123 gap.

**Root cause:** `get_current_month_expiry()` returned last Tuesday of month (28 Apr).
Nifty has **weekly** expiries every Tuesday. Nearest liquid expiry was 13 Apr.

**What changed:**
- Added `get_next_weekly_expiry()` — finds nearest Tuesday, shifts to Monday if holiday
- `OptionManager.__init__` uses `get_next_weekly_expiry()` instead of monthly
- Added `NSE_WEEKLY_EXPIRY_HOLIDAYS` for known holiday Tuesdays

**Files:** `option_manager.py`

---

## v3 — session 6: Strike not found fix

**Why:** `[OptionManager] No valid strike found for CE/PE` on every signal.

**Root causes:**
1. `find_option_token()` downloaded scrip master CSV on every candidate strike (14 HTTP calls per signal)
2. Delta walk cap hit before finding tokens — small DTE meant many strikes failed delta check

**What changed:**
- Added `_scrip_cache` module-level list — downloads once, all lookups in memory
- `_get_scrip_master(client)` helper — returns cache, downloads on first call
- `find_option_token()` and `find_futures_token()` both use cache
- `pick_strike()`: relaxed early-skip floor from 0.85 → 0.50, count `token_walk` only for found tokens, fallback always returns best candidate

**Files:** `option_manager.py`

---

## v3 — session 7: VWAP zone ±5 → ±2

**Why:** Too many entries at wide distances from VWAP. The tighter zone ensures we only
enter when price is genuinely near VWAP, reducing false signals from distant crosses.

**What changed:**
- `FRESH_CROSS_MAX_DIST`: 5.0 → 2.0
- `PULLBACK_MAX_DIST`: 5.0 → 2.0

**Files:** `config.py`

---

## v3 — session 8: VWAP trend filter

**Why:** In a clearly falling market (VWAP declining every minute), the algo was still
taking CE trades on minor pullbacks, leading to counter-trend losses.

**Logic:** Track VWAP at end of each minute. Compare oldest vs newest over 5-min window.
Rising VWAP → CE only. Falling VWAP → PE only. Flat → both allowed.
Filter activates at 9:30 (15 min of history built by then) but tracking starts at 9:15.

**What changed:**
- `FuturesVWAPEngine`: added `_vwap_minute_history` deque, snapshot on minute boundary
- `vwap_trend()`: compares oldest vs newest snapshot, returns `rising/falling/flat`
- `vwap_trend_detail()`: returns dict with change amount and snapshot count
- `_on_signal()`: trend filter applied if `t.time() >= VWAP_TREND_START (9:30)`

**Config added:** `VWAP_TREND_LOOKBACK=5`, `VWAP_TREND_MIN_CHANGE=0.5`, `VWAP_TREND_START="09:30"`

**Files:** `futures_engine.py`, `main.py`, `config.py`

---

## v3 — session 9: Pullback limit + trend filter don't apply pre-9:30

**Why:** User confirmed: trend filter should not block entries before 9:30 (market hasn't
settled). But tracking must still run from 9:15 so history is ready when filter activates.

**What changed:**
- Trend filter wrapped in `if t.time() >= trend_start` check
- Pullback counter initialised per-day, increments on confirmed fill only
- `MAX_PULLBACK_PER_DIR = 2` in config

**Files:** `main.py`, `config.py`

---

## v3 — session 10: Two-session SL/trail system

**Why:** Early session (9:16–9:39) needs tighter SL (market still finding direction) but
also tighter trade count (max 2). Normal session needs a multi-step trail ladder to capture
more of each move without giving back all profit.

### Early session (9:16–9:39)
- Max 2 trades total
- SL = 7 pts (tight — market still choppy)
- After +25 → SL to entry+5
- After +35 → SL to entry+25
- Target = +45 (book profit)
- After a loss: wait 10 min from entry before 2nd trade
- After a win: 2nd trade immediately

### Normal session (9:40+)
- SL = 15 pts
- +20 → SL entry+1, +30 → entry+10, +35 → entry+20, +40 → entry+25
- From +45 onward: trail every 5 pts
- Target = +50

**What changed:**
- Config: all trail/SL constants split into `EARLY_*` and `NORMAL_*`
- `_on_signal()`: early session guard checks trade count and loss-wait
- `_on_option_tick()`: session-aware trail ladder
- `_exit_trade()`: records `early_last_result` for loss-wait logic
- Entry block: sets SL/target based on session, logs `[EARLY]` or `[NORMAL]`

**Files:** `config.py`, `main.py`

---

## v3 — session 11: 3-tick cross confirmation

**Why:** A single tick above VWAP followed by an immediate dip back was triggering cross
signals. These are wicks / noise, not genuine direction changes.

**Logic:** When price first crosses VWAP, start a `_cross_pending` counter.
The signal only fires after `CROSS_CONFIRM_TICKS=3` consecutive ticks remain on the
new side. If price dips back before 3 ticks, counter resets — no signal.

Applies to fresh cross signals only (not pullbacks, which have their own proximity filter).

**What changed:**
- `FuturesVWAPEngine.__init__`: added `_cross_pending` (direction str) and `_cross_ticks` (int)
- `on_tick()`: replaced immediate signal fire with accumulation logic + fire after threshold
- Console print when cross confirmed: `[Cross] CE confirmed after 3 ticks above VWAP`

**Config added:** `CROSS_CONFIRM_TICKS = 3`

**Files:** `futures_engine.py`, `config.py`

---

## v3 — session 11 (continued): Expanded CSV trade log

**Why:** Original CSV had ~30 columns. Missing: option low price, MAE, VWAP at exit,
futures price at exit, session tag, signal type, trail step reached, pullback count,
ITM depth, early session trade count.

### New columns added (40+ total)

| Column | What it tracks |
|---|---|
| Session | EARLY or NORMAL |
| Signal Type | cross or pullback |
| ITM Depth (pts) | How deep ITM the strike was |
| Option Low | Lowest option LTP seen during trade |
| Pts Min (low) | Worst drawdown from entry |
| Max Adverse Excursion | Worst loss seen during trade (risk management metric) |
| VWAP at Exit | VWAP when trade closed |
| VWAP Trend at Entry | rising/falling/flat at time of entry |
| Futures at Entry/Exit | Futures LTP at both points |
| SL Pts Used / Target Pts Used | Actual values used (differ early vs normal) |
| SL Price / Target Price | Absolute price levels |
| Trail Step Reached | Last trail level hit (e.g. +35→SL+20) |
| Early Session Trade# | Which trade it was in early session (1 or 2) |
| Pullback Count at Entry | Running pullback count for that direction |
| High VIX Regime | Y/N |

**Files:** `report_manager.py`, `main.py`

---

## CSV column reference (full list)

```
Date, Trade#, Mode, Session,
Entry Time, Exit Time, Duration (mins),
Signal Type, Direction,
Strike, Expiry, ATM at Entry, ITM Depth (pts),
Option Entry, Option Exit, Option Peak, Option Low,
Pts Gained, Pts Max (peak), Pts Min (low), Pts Missed, Max Adverse Excursion,
Trail Efficiency %,
VWAP at Entry, Dist from VWAP (pts), VWAP Trend at Entry, VWAP at Exit,
Futures at Entry, Futures at Exit, Futures Move,
SL Pts Used, Target Pts Used, SL Price, Target Price, Trail Step Reached,
Gross PnL Rs, Cost Rs, Net PnL Rs, Capital After Rs, Return on Capital %,
Exit Reason, Exit Phase, Breakeven Triggered, Trail Triggered,
Early Session Trade#, Pullback Count at Entry,
VIX, High VIX Regime
```

---

## v3 — session 12: Strike cache expansion + OI fix

### Problems
1. Cache only held 5 specific ITM depths (100,150,200,250,300pts) ± 50 = ~15 strikes.
   If Nifty moved 350+pts, cached strikes became OTM and algo fell to slow live scan.

2. OI field always 0 in preload. `fetch_oi_and_ltp()` was called per-token (sequential)
   and either the quote_type wasn't returning OI or the response parsing missed the field.

3. `pick_strike()` used `strike < atm` to check ITM — wrong after a big market move.
   Delta is the correct ITM proxy regardless of spot movement.

### Fix 1 — Continuous cache range (`config.py`, `option_manager.py`)
- Replaced `PRELOAD_ITM_DEPTHS` list with `PRELOAD_ITM_MIN=50`, `PRELOAD_ITM_MAX=500`,
  `PRELOAD_STEP=50` — caches every strike from ATM±50 to ATM±500 in 50pt steps
- That's 10 strikes per direction = 20 total, covering market moving ±500pts from open

### Fix 2 — Batch OI fetch (`option_manager.py`)
- `preload_strikes()` now resolves ALL tokens first (scrip master, no HTTP), then sends
  ALL tokens in a SINGLE `client.quotes()` call per direction
- Prints `[OI_DEBUG]` with ALL field names from the first batch response — so we can
  immediately see which field Kotak actually puts OI in (it varies by API version)
- Added `dOpenInt` and `openint` to the OI field search list

### Fix 3 — OI-aware pick_strike logic (`option_manager.py`)
- If cache has OI > 0: filter delta + OI (existing behaviour)
- If cache OI = 0 (API limitation): filter delta only, then fetch live OI for top 5
  candidates to find one with good OI — only 1–5 API calls at signal time, not 14+
- ITM check uses delta >= MIN_DELTA (not `strike < atm`) — correct after market moves
- Fallback: if OI API returns 0 for all, use highest-delta strike and warn clearly

**Files:** `config.py`, `option_manager.py`
