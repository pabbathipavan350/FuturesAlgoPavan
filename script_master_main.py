# ============================================================
# MAIN.PY — Nifty Futures VWAP Options Algo v3
# ============================================================
# NEW in v3 vs v2:
#
# 1. STARTUP PRE-LOAD
#    All strike tokens + OI fetched at startup.
#    pick_strike() at signal time reads from cache — no delay.
#
# 2. VIX-BASED SL + TARGET
#    Fetched at startup via index quote.
#    High VIX (>15): SL=15, Target=50
#    Low  VIX (≤15): SL=10, Target=35
#
# 3. TRAILING SL
#    Activates at +20 pts profit.
#    SL = current_ltp - TRAIL_BUFFER (ratchets up, never down).
#
# 4. ENTRY PROXIMITY FILTER
#    Fresh cross: only if futures within ±10 pts of VWAP.
#    Pullback   : only if within 0–15 pts of VWAP.
#    (Implemented in FuturesVWAPEngine.)
#
# 5. OPTION VWAP CONFIRMATION (mid-trade cross)
#    When in a trade and futures crosses VWAP again:
#      option LTP > option VWAP → just a pullback → hold
#      option LTP < option VWAP → real reversal → flip/exit
#
# 6. DIRECTION FATIGUE RULE
#    After 2 profitable exits in same direction:
#      → skip next signal in that direction for 15 mins
#
# 7. DAILY GUARDS
#    Max 6 trades/day
#    3 consecutive SL hits → stop for day
#    2 full targets hit    → stop for day
# ============================================================

import threading
import signal
import logging
import logging.handlers
import os
import datetime
import sys
sys.stdout.reconfigure(line_buffering=True)
import time

import config
from auth              import get_kotak_session
from futures_engine    import FuturesVWAPEngine
from option_manager    import OptionManager, find_futures_token, fetch_ltp
from capital_manager   import CapitalManager
from report_manager    import ReportManager
from session_manager   import SessionManager
from telegram_notifier import TelegramNotifier


def now_ist() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)


def setup_logging():
    os.makedirs("logs", exist_ok=True)
    log_file = f"logs/algo_{now_ist().strftime('%Y%m%d')}.log"
    fmt      = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
    root     = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=20*1024*1024, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(fmt))
    root.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(fmt))
    root.addHandler(ch)
    return logging.getLogger("main")


class FuturesVWAPAlgo:

    def __init__(self):
        self.logger         = setup_logging()
        self.client         = None
        self.session_mgr    = None
        self.opt_mgr        = None
        self.cap_mgr        = None
        self.report_mgr     = None
        self.telegram       = TelegramNotifier()
        self.futures_engine = FuturesVWAPEngine()

        # Tokens
        self.futures_token  = None
        self.option_token   = None

        # VIX state (set at startup)
        self.current_vix    = 0.0
        self.high_vix       = True    # default to high until VIX fetched
        self.sl_pts         = config.SL_PTS_HIGH_VIX
        self.target_pts     = config.TARGET_PTS_HIGH_VIX

        # Position state
        self.in_trade       = False
        self.direction      = None
        self.entry_type     = None
        self.strike         = None
        self.entry_price    = 0.0
        self.entry_time     = None
        self.entry_vwap     = 0.0
        self.sl_price       = 0.0
        self.target_price   = 0.0
        self.peak_price     = 0.0
        self.low_price      = 0.0   # lowest option LTP seen during trade
        self.trail_step     = ""    # last trail step label hit
        self.trail_active   = False
        self.qty            = config.LOTS * config.LOT_SIZE
        self.option_ltp     = 0.0

        # Day counters
        self.day_pnl_rs         = 0.0
        self.trade_count        = 0
        self.consec_sl          = 0   # consecutive SL hits
        self.day_stopped        = False

        # Early session (9:16–9:39) counters
        self.early_trade_count  = 0          # trades taken in early session
        self.early_last_result  = None       # 'win' | 'loss' | None
        self.early_last_entry_t = None       # datetime of last early entry

        # Direction fatigue tracking
        # {direction: {'wins': N, 'last_win_time': datetime}}
        self._dir_wins: dict      = {"CE": {"wins": 0, "last_win_time": None},
                                     "PE": {"wins": 0, "last_win_time": None}}

        # Pullback trade counter per direction — resets each day
        # Max MAX_PULLBACK_PER_DIR pullback entries allowed per direction
        self._pullback_count: dict = {"CE": 0, "PE": 0}

        # Pre-load flag
        self._preloaded         = False
        self._first_futures_ltp = 0.0

        # Reconnect guard — prevents multiple threads racing on the same drop
        self._reconnecting      = False
        self._reconnect_lock    = threading.Lock()

        # Pre-subscribed option WS tracking
        # Subscribed at startup so live LTP is available the moment a signal fires.
        # {direction: token}  e.g. {"CE": "54321", "PE": "98765"}
        self._pre_subscribed: dict[str, str]   = {}
        self._pre_ltp:        dict[str, float] = {}  # direction → live WS LTP

        # Auto-shutdown timer (set properly in initialize())
        self._start_time    = now_ist()
        self._shutdown_time = self._start_time + datetime.timedelta(hours=5, minutes=50)

        # No-tick circuit breaker
        self._last_tick_time  = now_ist()
        self._circuit_alerted = False

        self._running = True
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        signal.signal(signal.SIGINT,  self._handle_sigterm)

    # ── Init ─────────────────────────────────────────────

    def initialize(self):
        print("\n" + "="*60)
        print("  Nifty Futures VWAP Options Algo  v3")
        print(f"  Mode    : {'*** PAPER TRADE ***' if config.PAPER_TRADE else '*** LIVE ***'}")
        print(f"  Capital : Rs {config.TOTAL_CAPITAL:,.0f}  |  {config.LOTS} lots × {config.LOT_SIZE}")
        print(f"  Guards  : {config.MAX_CONSEC_SL} consec SL stops day | "
              f"Loss limit Rs{config.MAX_DAILY_LOSS_RS:,.0f}")
        print("="*60)

        # Record start time — auto-shutdown after 5h 50m from start
        self._start_time    = now_ist()
        self._shutdown_time = self._start_time + datetime.timedelta(hours=5, minutes=50)
        print(f"[Init] Started at   : {self._start_time.strftime('%H:%M:%S')} IST")
        print(f"[Init] Auto-shutdown: {self._shutdown_time.strftime('%H:%M:%S')} IST")

        self.client = get_kotak_session()

        self.session_mgr = SessionManager(self.client, get_kotak_session)
        self.session_mgr.on_reconnect = self._on_reconnect
        self.session_mgr.start()

        self.cap_mgr    = CapitalManager()
        self.opt_mgr    = OptionManager(self.client)
        self.report_mgr = ReportManager(self.cap_mgr)

        # Resolve futures token
        self._resolve_futures_token()

        # Fetch VIX at startup
        self._fetch_vix()

        # Setup WS callbacks
        self._setup_websocket()

        exp = self.opt_mgr.expiry_date
        print(f"\n[Init] Expiry : {exp.strftime('%d %b %Y')} "
              f"(in {(exp - datetime.date.today()).days}d)")
        vix_regime = 'HIGH' if self.high_vix else 'LOW'
        print(f"[Init] VIX    : {self.current_vix:.1f}  ->  {vix_regime} regime  "
              f"SL={self.sl_pts}  Target={self.target_pts}  "
              f"Trail: +{config.TRAIL_BREAKEVEN_TRIGGER}pts->BE  "
              f"+{config.TRAIL_LOCK_TRIGGER}pts->+{config.TRAIL_LOCK_PTS}locked")
        print(f"[Init] Futures token: {self.futures_token}")

        # Pre-load strikes NOW using Nifty index quote — before 9:15
        self._preload_at_startup()

        self.telegram.alert_startup(
            mode   = "PAPER" if config.PAPER_TRADE else "LIVE",
            expiry = str(exp),
            atm    = f"VIX={self.current_vix:.1f}",
        )
        print("[Init] Initialisation complete — starting WebSocket")
        print("[Init] Entries blocked until 09:16:00 IST\n")

    def _preload_at_startup(self):
        """
        Fetch spot from the Nifty futures token quote (already resolved, no
        separate index API call). Falls back to index quote then to a fixed
        estimate so preload ALWAYS runs at startup — never waits for first tick.
        """
        # Try futures token first (most reliable — same token WS uses)
        spot = self._fetch_spot_from_futures_token()
        if spot <= 0:
            spot = self._fetch_nifty_spot()   # try index token
        if spot <= 0:
            spot = 24000.0   # fixed estimate — cache covers ±500pts so still useful
            print(f"[PreLoad] Spot fetch failed — using estimate {spot:.0f}")

        print(f"[PreLoad] Startup spot: {spot:.0f} — pre-loading strikes now...")
        try:
            self.opt_mgr.preload_strikes(spot)
            self._preloaded = True
            self._subscribe_preloaded_options()   # ← subscribe top CE+PE for live LTP
            print("[PreLoad] ✅ Startup pre-load done — ready before 9:15")
        except Exception as e:
            self.logger.error(f"[PreLoad] Startup error: {e}", exc_info=True)
            print(f"[PreLoad] ⚠️  Preload error: {e}")

    def _fetch_spot_from_futures_token(self) -> float:
        """Fetch LTP from the Nifty futures token — no index quote needed."""
        if not self.futures_token:
            return 0.0
        try:
            resp = self.client.quotes(
                instrument_tokens=[{"instrument_token": self.futures_token,
                                    "exchange_segment": config.FO_SEGMENT}],
                quote_type="ltp",
            )
            data = resp if isinstance(resp, list) else (
                   resp.get("message") or resp.get("data") or [] if isinstance(resp, dict) else [])
            if data:
                for f in ("ltp", "ltP", "last_price", "lastPrice", "close"):
                    v = data[0].get(f)
                    if v and float(v) > 0:
                        return float(v)
        except Exception as e:
            self.logger.debug(f"_fetch_spot_from_futures_token: {e}")
        return 0.0

    def _fetch_nifty_spot(self) -> float:
        """Fetch current Nifty 50 index LTP (used for startup pre-load)."""
        try:
            resp = self.client.quotes(
                instrument_tokens=[{"instrument_token": config.NIFTY_INDEX_TOKEN,
                                    "exchange_segment": config.CM_SEGMENT}],
                quote_type="ltp"
            )
            if isinstance(resp, dict):
                data = resp.get("message") or resp.get("data") or []
            elif isinstance(resp, list):
                data = resp
            else:
                data = []
            if data:
                for f in ("ltp", "ltP", "last_price", "lastPrice", "close"):
                    v = data[0].get(f)
                    if v and float(v) > 0:
                        return float(v)
        except Exception as e:
            self.logger.debug(f"_fetch_nifty_spot: {e}")
        return 0.0

    def _fetch_vix(self):
        """Fetch India VIX from Kotak quotes API and set SL/target."""
        INDIA_VIX_TOKEN = "26074"   # India VIX index token on NSE
        try:
            resp = self.client.quotes(
                instrument_tokens=[{"instrument_token": INDIA_VIX_TOKEN,
                                    "exchange_segment": config.CM_SEGMENT}],
                quote_type="ltp"
            )
            if isinstance(resp, dict):
                data = resp.get("message") or resp.get("data") or []
            elif isinstance(resp, list):
                data = resp
            else:
                data = []
            if data:
                vix = float(data[0].get("ltp") or data[0].get("ltP") or 0)
                if vix > 0:
                    self.current_vix = vix
        except Exception as e:
            self.logger.debug(f"VIX fetch error: {e}")

        if self.current_vix <= 0:
            print("[VIX] Could not fetch — defaulting to HIGH VIX regime")
            self.current_vix = 20.0   # safe default

        self.high_vix     = self.current_vix > config.VIX_HIGH_THRESHOLD
        self.sl_pts       = config.SL_PTS_HIGH_VIX     if self.high_vix else config.SL_PTS_LOW_VIX
        self.target_pts   = config.TARGET_PTS_HIGH_VIX  if self.high_vix else config.TARGET_PTS_LOW_VIX
        self.report_mgr.set_vix(self.current_vix)

    def _resolve_futures_token(self):
        expiry_str = self.opt_mgr.expiry_str
        print(f"[Init] Resolving futures token for expiry {expiry_str}...")
        self.futures_token = find_futures_token(self.client, self.opt_mgr.expiry_date)
        if not self.futures_token:
            raise RuntimeError(
                f"Could not resolve Nifty futures token for {expiry_str}."
            )

    # ── WebSocket ─────────────────────────────────────────

    def _setup_websocket(self):
        self.client.on_message = self._on_message
        self.client.on_error   = self._on_ws_error
        self.client.on_close   = self._on_ws_close
        self.client.on_open    = self._on_ws_open

    def _on_ws_open(self, *args):
        print("[WS] Connected")
        # Always resubscribe on open — handles both first connect AND SDK-internal
        # reconnects (the SDK calls on_open after every successful reconnect, but
        # it does NOT restore subscriptions — we must do it ourselves).
        threading.Thread(target=self._resubscribe_all,
                         daemon=True, name="WSSub").start()

    def _resubscribe_all(self):
        """Resubscribe futures + pre-loaded options + active option after any connect/reconnect."""
        time.sleep(0.5)   # tiny settle delay — SDK needs a moment before accepting subscribe calls
        self._subscribe_futures()
        for d, pre_tok in self._pre_subscribed.items():
            try:
                self.client.subscribe(
                    instrument_tokens=[{"instrument_token": pre_tok,
                                        "exchange_segment": config.FO_SEGMENT}],
                    isIndex=False, isDepth=False,
                )
                self.logger.debug(f"[WS] Re-subscribed pre-loaded {d} token={pre_tok}")
            except Exception as e:
                self.logger.debug(f"[WS] Re-sub pre-loaded {d} error: {e}")
        if self.option_token:
            try:
                self._subscribe_option(self.option_token)
            except Exception as e:
                self.logger.debug(f"[WS] Re-sub active option error: {e}")

    def _on_ws_error(self, error):
        err_str = str(error)
        # "already closed" / "socket is already closed" — expected noise during
        # close storms (Kotak drops all connections near market close).
        # Log as debug only to avoid spamming the console.
        if "already closed" in err_str.lower() or "nonetype" in err_str.lower():
            self.logger.debug(f"[WS] Error (expected during close): {error}")
        else:
            self.logger.error(f"[WS] Error: {error}")

    def _on_ws_close(self, *args):
        # Kotak drops all connections near market close — this fires many times.
        # After 15:00 there is nothing more to trade, so just log quietly and exit.
        now_t = now_ist().time()
        if now_t >= datetime.time(15, 0):
            self.logger.debug("[WS] Closed after 15:00 — no reconnect needed")
            return
        self.logger.warning("[WS] Closed")
        if not self._running:
            return
        with self._reconnect_lock:
            if self._reconnecting:
                self.logger.debug("[WS] Close event ignored — reconnect already in progress")
                return
            self._reconnecting = True
        # Small delay so the SDK's own reconnect attempt can settle first,
        # preventing our loop and the SDK's loop from fighting each other.
        threading.Thread(target=self._ws_reconnect_loop,
                         daemon=True, name="WSReconnect").start()

    def _ws_reconnect_loop(self):
        """
        Our fallback reconnect — only runs if the SDK's internal reconnect also fails.
        The SDK's own reconnect calls on_open which triggers _resubscribe_all,
        so we do NOT need to subscribe tokens here — just re-establish the connection.
        Only ONE instance of this runs at a time — guarded by _reconnect_lock.
        """
        delays = [5, 10, 20, 30]
        try:
            for attempt, delay in enumerate(delays, 1):
                if not self._running:
                    return
                # Check again — market may have closed while we were waiting
                if now_ist().time() >= datetime.time(15, 0):
                    print("[WS] Reconnect aborted — after 15:00, no more trading")
                    return
                print(f"\n[WS] Reconnect attempt {attempt}/{len(delays)} in {delay}s...")
                time.sleep(delay)
                if not self._running:
                    return
                try:
                    self._setup_websocket()
                    # Subscriptions are handled by _on_ws_open → _resubscribe_all
                    # Just call subscribe_futures to trigger the open event
                    self._subscribe_futures()
                    print(f"[WS] ✅ Reconnected (attempt {attempt})")
                    return
                except Exception as e:
                    self.logger.error(f"[WS] Reconnect attempt {attempt} failed: {e}")
            print("[WS] ❌ All reconnect attempts failed — session manager will handle")
        finally:
            with self._reconnect_lock:
                self._reconnecting = False

    def _subscribe_futures(self):
        if not self.futures_token:
            return
        try:
            self.client.subscribe(
                instrument_tokens=[{"instrument_token": self.futures_token,
                                    "exchange_segment": config.FO_SEGMENT}],
                isIndex=False, isDepth=False,
            )
            print(f"[WS] Subscribed futures token={self.futures_token}")
        except Exception as e:
            self.logger.error(f"Subscribe futures: {e}")

    def _subscribe_option(self, token: str):
        try:
            self.client.subscribe(
                instrument_tokens=[{"instrument_token": token,
                                    "exchange_segment": config.FO_SEGMENT}],
                isIndex=False, isDepth=False,
            )
            self.futures_engine.register_option_token(token)
        except Exception as e:
            self.logger.error(f"Subscribe option: {e}")

    def _unsubscribe_option(self, token: str):
        try:
            self.client.unsubscribe(
                instrument_tokens=[{"instrument_token": token,
                                    "exchange_segment": config.FO_SEGMENT}],
                isIndex=False, isDepth=False,
            )
            self.futures_engine.unregister_option_token(token)
        except Exception:
            pass

    # ── Tick handler ──────────────────────────────────────

    def _on_message(self, message):
        try:
            if not isinstance(message, dict):
                return
            msg_type = message.get('type', '')
            if msg_type not in ('stock_feed', 'sf', 'index_feed', 'if'):
                return
            ticks = message.get('data', [])
            if not ticks:
                return
            for tick in ticks:
                token = str(tick.get('tk') or tick.get('token') or
                            tick.get('instrument_token') or '')
                ltp   = float(tick.get('ltp') or tick.get('ltP') or 0)
                if ltp <= 0:
                    continue
                self._last_tick_time  = now_ist()
                self._circuit_alerted = False

                # Track live LTP for pre-subscribed option tokens
                # (before any trade is open — keeps LTP fresh for order pricing)
                for _d, _pre_tok in self._pre_subscribed.items():
                    if token == str(_pre_tok):
                        self._pre_ltp[_d] = ltp
                        break

                if token == str(self.futures_token):
                    self._on_futures_tick(tick)
                elif token == str(self.option_token):
                    self._on_option_tick(tick)
                # Always forward to option VWAP tracker
                self.futures_engine.on_option_tick(token, tick)

        except Exception as e:
            self.logger.error(f"_on_message: {e}", exc_info=True)

    def _on_futures_tick(self, tick: dict):
        self.futures_engine.on_tick(tick)
        state = self.futures_engine.get_state()
        ltp   = state["ltp"]

        if not self._is_market_hours(now_ist()):
            return

        sig, sig_type = self.futures_engine.check_signal()
        if not sig:
            return

        if self.in_trade and self.direction == sig:
            # Same direction — check if it's a real reversal via option VWAP
            self.logger.debug(f"[Guard] {sig} signal while in {self.direction} — ignored")
            return

        if self.in_trade and self.direction != sig:
            # Opposite direction signal — use option VWAP confirmation
            self._handle_opposite_signal(sig, sig_type, state)
        else:
            # No open trade — evaluate entry
            self._on_signal(sig, sig_type, state["ltp"], state["vwap"], now_ist())

    def _handle_opposite_signal(self, sig: str, sig_type: str, state: dict):
        """
        Opposite direction signal while in a trade.
        Use option VWAP to confirm reversal vs pullback.
        """
        if not config.ENABLE_OPTION_VWAP_CONFIRM or not self.option_token:
            # No confirmation filter — flip immediately
            self._on_signal(sig, sig_type, state["ltp"], state["vwap"], now_ist())
            return

        opt_above_vwap = self.futures_engine.get_option_vwap_position(self.option_token)

        if opt_above_vwap is None:
            # Not enough option VWAP data — flip (safe default)
            self.logger.info("[Confirm] No option VWAP data — treating as reversal")
            self._on_signal(sig, sig_type, state["ltp"], state["vwap"], now_ist())
            return

        if opt_above_vwap:
            # Option above its VWAP → just a pullback → hold current trade
            self.logger.info(
                f"[Confirm] {sig} signal but option LTP above option VWAP "
                f"→ PULLBACK — holding {self.direction} trade"
            )
            print(f"[Confirm] Futures crossed {sig} but option is ABOVE its VWAP "
                  f"→ pullback, not reversal — holding {self.direction}")
        else:
            # Option below its VWAP → real reversal → flip
            self.logger.info(
                f"[Confirm] {sig} signal + option LTP below option VWAP "
                f"→ REVERSAL — flipping to {sig}"
            )
            print(f"[Confirm] Option BELOW its VWAP → confirmed reversal → flip to {sig}")
            self._on_signal(sig, sig_type, state["ltp"], state["vwap"], now_ist())

    def _on_option_tick(self, tick: dict):
        ltp = float(tick.get("ltp") or tick.get("ltP") or tick.get("lp") or 0)
        if ltp <= 0 or not self.in_trade:
            return

        self.option_ltp = ltp
        if ltp > self.peak_price:
            self.peak_price = ltp
        if self.low_price == 0.0 or ltp < self.low_price:
            self.low_price = ltp

        profit_pts = ltp - self.entry_price
        now_t      = now_ist().time()
        early_end  = datetime.time(*map(int, config.EARLY_SESSION_END.split(":")))
        is_early   = now_t < early_end

        # ── Trailing SL ───────────────────────────────────
        new_sl = self.sl_price

        if is_early:
            # Early session ladder: 25→entry+5, 35→entry+25
            if profit_pts >= config.EARLY_TRAIL_2_TRIGGER:
                new_sl = round(self.entry_price + config.EARLY_TRAIL_2_LOCK, 2)
            elif profit_pts >= config.EARLY_TRAIL_1_TRIGGER:
                new_sl = round(self.entry_price + config.EARLY_TRAIL_1_LOCK, 2)
        else:
            # Normal session ladder: 20→+1, 30→+10, 35→+20, 40→+25, then trail every 5
            if profit_pts >= config.NORMAL_TRAIL_STEP_START:
                # Continuous trail every 5 pts from +45 onward
                steps  = int((profit_pts - config.NORMAL_TRAIL_STEP_START)
                             / config.NORMAL_TRAIL_STEP_SIZE)
                offset = config.NORMAL_TRAIL_4_LOCK + steps * config.NORMAL_TRAIL_STEP_SIZE
                new_sl = round(self.entry_price + offset, 2)
            elif profit_pts >= config.NORMAL_TRAIL_4_TRIGGER:
                new_sl = round(self.entry_price + config.NORMAL_TRAIL_4_LOCK, 2)
            elif profit_pts >= config.NORMAL_TRAIL_3_TRIGGER:
                new_sl = round(self.entry_price + config.NORMAL_TRAIL_3_LOCK, 2)
            elif profit_pts >= config.NORMAL_TRAIL_2_TRIGGER:
                new_sl = round(self.entry_price + config.NORMAL_TRAIL_2_LOCK, 2)
            elif profit_pts >= config.NORMAL_TRAIL_1_TRIGGER:
                new_sl = round(self.entry_price + config.NORMAL_TRAIL_1_LOCK, 2)

        if new_sl > self.sl_price:
            self.logger.info(f"[Trail] SL {self.sl_price:.2f} → {new_sl:.2f} "
                             f"(+{profit_pts:.1f}pts {'early' if is_early else 'normal'})")
            print(f"  [Trail] SL moved {self.sl_price:.2f} → {new_sl:.2f}")
            self.sl_price     = new_sl
            self.trail_active = True
            self.trail_step   = f"+{profit_pts:.0f}pts→SL+{new_sl - self.entry_price:.0f}"

        # ── Target check ──────────────────────────────────
        if ltp >= self.target_price:
            self.logger.info(f"[Exit] TARGET ltp={ltp:.2f} tgt={self.target_price:.2f}")
            self._exit_trade(ltp, "Target")
            return

        # ── SL check ─────────────────────────────────────
        if ltp <= self.sl_price:
            reason = "Trail SL" if self.trail_active else "SL"
            self.logger.info(f"[Exit] {reason} ltp={ltp:.2f} sl={self.sl_price:.2f}")
            self._exit_trade(ltp, reason)

    # ── Pre-subscribed options ────────────────────────────

    def _subscribe_preloaded_options(self):
        """
        Subscribe the best CE and PE tokens immediately after preload.
        This means live LTP arrives via WebSocket BEFORE any signal fires,
        so we never need to use the stale cached LTP for order pricing.
        Called once at startup right after preload_strikes() completes.
        """
        for direction in ["CE", "PE"]:
            candidates = self.opt_mgr._strike_cache.get(direction, [])
            if not candidates:
                print(f"[PreSub] ⚠️  No {direction} candidates in cache — skipping")
                continue
            best = candidates[0]   # already sorted best-delta first
            tok  = best["token"]
            self._pre_subscribed[direction] = tok
            self._pre_ltp[direction]        = best.get("ltp", 0.0)   # seed with cached value
            try:
                self.client.subscribe(
                    instrument_tokens=[{"instrument_token": tok,
                                        "exchange_segment": config.FO_SEGMENT}],
                    isIndex=False, isDepth=False,
                )
                print(f"[PreSub] ✅ Subscribed {direction} {best['strike']} "
                      f"token={tok}  (seeded LTP={best.get('ltp', 0.0):.1f})")
            except Exception as e:
                self.logger.error(f"[PreSub] {direction} subscribe error: {e}")

    # ── Pre-load trigger ─────────────────────────────────

    def _trigger_preload(self, spot: float):
        """Run preload_strikes() in a background thread so WS keeps ticking."""
        self._preloaded = True
        print(f"\n[PreLoad] First futures tick: spot={spot:.0f} — starting pre-load...")

        def _do_preload():
            try:
                self.opt_mgr.preload_strikes(spot)
            except Exception as e:
                self.logger.error(f"[PreLoad] Error: {e}", exc_info=True)

        t = threading.Thread(target=_do_preload, daemon=True, name="PreLoad")
        t.start()

    # ── Signal handler ────────────────────────────────────

    def _on_signal(self, direction: str, sig_type: str,
                   futures_ltp: float, futures_vwap: float,
                   t: datetime.datetime):

        # ── Day stop guards ───────────────────────────────
        if self.day_stopped:
            self.logger.info(f"[Guard] Day stopped — ignoring {direction} signal")
            return

        if self.consec_sl >= config.MAX_CONSEC_SL:
            if not self.day_stopped:
                self.day_stopped = True
                msg = f"{config.MAX_CONSEC_SL} consecutive SLs — stopped for day"
                print(f"\n[Guard] {msg}")
                self.telegram.alert_risk(msg)
            return

        if self.day_pnl_rs <= config.MAX_DAILY_LOSS_RS:
            if not self.day_stopped:
                self.day_stopped = True
                msg = f"Daily loss limit Rs{config.MAX_DAILY_LOSS_RS:,.0f} hit — stopped"
                print(f"\n[Guard] {msg}")
                self.telegram.alert_risk(msg)
            return

        # ── Timing guards ─────────────────────────────────
        # No entries before 9:16:00 — give market time to stabilise
        entry_open = datetime.time(9, 16, 0)
        if t.time() < entry_open:
            self.logger.info(f"[Guard] Entry blocked — before 09:16 ({t.strftime('%H:%M:%S')})")
            return

        sq_time = datetime.time(*map(int, config.SQUARE_OFF_TIME.split(":")))
        if t.time() >= sq_time:
            return
        if datetime.date.today() == self.opt_mgr.expiry_date:
            cutoff = datetime.time(*map(int, config.EXPIRY_DAY_CUTOFF.split(":")))
            if t.time() >= cutoff:
                return

        # ── Early session guard (9:16–9:39) ──────────────
        early_end = datetime.time(*map(int, config.EARLY_SESSION_END.split(":")))
        is_early  = t.time() < early_end

        if is_early:
            # Max 2 trades in early session
            if self.early_trade_count >= config.EARLY_SESSION_MAX_TRADES:
                self.logger.info("[EarlyGuard] 2 trades done — blocked until 9:40")
                print("[EarlyGuard] Max 2 early trades reached — waiting for 9:40")
                return

            # After a loss: wait 10 min from last entry time before 2nd trade
            if (self.early_last_result == "loss" and
                    self.early_last_entry_t is not None):
                wait_secs = config.EARLY_LOSS_WAIT_MINS * 60
                elapsed   = (t - self.early_last_entry_t).total_seconds()
                if elapsed < wait_secs:
                    remaining = int((wait_secs - elapsed) / 60) + 1
                    self.logger.info(
                        f"[EarlyGuard] Loss wait — {remaining}min remaining")
                    print(f"[EarlyGuard] Last trade was a loss — "
                          f"waiting {remaining}min more")
                    return

        # ── Direction fatigue guard ───────────────────────
        if self._is_direction_fatigued(direction, t):
            self.logger.info(f"[Fatigue] {direction} fatigued — skipping")
            print(f"[Fatigue] {direction} skipped — {config.DIRECTION_FATIGUE_COUNT} wins, "
                  f"cooldown {config.DIRECTION_COOLDOWN_MINS} min")
            return

        # ── VWAP trend filter ─────────────────────────────
        # Applied only from 9:30 AM onward (but tracking starts at 9:15).
        # CE only in rising VWAP trend; PE only in falling VWAP trend.
        # 'flat' = not enough history or indeterminate → allow both.
        trend_start = datetime.time(
            *map(int, config.VWAP_TREND_START.split(":")))
        if t.time() >= trend_start:
            trend  = self.futures_engine.vwap_trend()
            detail = self.futures_engine.vwap_trend_detail()
            if trend == "falling" and direction == "CE":
                self.logger.info(
                    f"[TrendFilter] VWAP falling "
                    f"({detail['oldest_vwap']:.2f}→{detail['newest_vwap']:.2f} "
                    f"Δ{detail['change']:+.2f}) — CE skipped")
                print(f"[TrendFilter] VWAP ↓ ({detail['change']:+.2f}pts "
                      f"over {detail['snapshots']}min) — CE skipped")
                return
            if trend == "rising" and direction == "PE":
                self.logger.info(
                    f"[TrendFilter] VWAP rising "
                    f"({detail['oldest_vwap']:.2f}→{detail['newest_vwap']:.2f} "
                    f"Δ{detail['change']:+.2f}) — PE skipped")
                print(f"[TrendFilter] VWAP ↑ ({detail['change']:+.2f}pts "
                      f"over {detail['snapshots']}min) — PE skipped")
                return

        # ── Pullback trade limit ──────────────────────────
        # Maximum MAX_PULLBACK_PER_DIR pullback entries per direction per day
        if sig_type == "pullback":
            count = self._pullback_count.get(direction, 0)
            if count >= config.MAX_PULLBACK_PER_DIR:
                self.logger.info(
                    f"[PullbackLimit] {direction} pullback #{count} blocked "
                    f"(max {config.MAX_PULLBACK_PER_DIR})")
                print(f"[PullbackLimit] {direction} pullback limit reached "
                      f"({config.MAX_PULLBACK_PER_DIR}/day) — skipped")
                return

        # ── Close opposite trade if open ──────────────────
        if self.in_trade and self.direction != direction:
            print(f"\n[Flip] Closing {self.direction} → entering {direction}")
            self._exit_trade(self.option_ltp or self.entry_price, "Flip")

        # ── Pre-load not ready yet? ───────────────────────
        if not self._preloaded or not self.opt_mgr._strike_cache.get(direction):
            print(f"[Signal] {direction} signal but pre-load not ready — running live scan")

        print(f"\n{'='*55}")
        print(f"[Signal] {direction} {sig_type.upper()} at {t.strftime('%H:%M:%S')}")
        print(f"         Futures={futures_ltp:.2f}  VWAP={futures_vwap:.2f}  "
              f"dist={futures_ltp-futures_vwap:+.2f}pts")

        # ── Pick strike (from cache) ──────────────────────
        info = self.opt_mgr.pick_strike(futures_ltp, direction)
        if not info:
            self.logger.error(f"No strike for {direction}")
            self.telegram.alert_risk(f"No {direction} strike at {t.strftime('%H:%M')}")
            return

        # ── Get fresh LTP for order pricing ──────────────
        # Priority 1: pre-subscribed WS feed — live LTP, zero REST latency
        # Priority 2: live REST fetch — for any strike that differs from pre-sub
        # Priority 3: NEVER use cached preload LTP — it may be many hours stale
        option_ltp = 0.0

        # Check if picked strike matches a pre-subscribed token
        for d, pre_tok in self._pre_subscribed.items():
            if info["token"] == pre_tok and d == direction:
                ws_ltp = self._pre_ltp.get(d, 0.0)
                if ws_ltp > 0:
                    option_ltp = ws_ltp
                    print(f"[LTP] Using pre-subscribed WS LTP: {option_ltp:.2f}")
                break

        # Fall back to live REST if WS LTP not available
        if option_ltp <= 0:
            print(f"[LTP] Fetching live LTP via REST for {direction} {info['strike']}...")
            option_ltp = fetch_ltp(self.client, info["token"])

        if option_ltp <= 0:
            self.logger.error(f"[LTP] Cannot get option LTP for {direction} {info['strike']} — aborting entry")
            return

        print(f"[Strike] {direction} {info['strike']}  "
              f"delta={info['delta']:.2f}  OI={info['oi']:,}  LTP={option_ltp:.2f}")

        # ── Place buy order ───────────────────────────────
        fill = self.opt_mgr.place_buy_order(
            token=info["token"], strike=info["strike"],
            direction=direction, ltp=option_ltp,
        )
        if not fill:
            self.logger.error("Buy order not filled")
            return

        fill_px = fill["fill_price"]

        # ── Set trade state ───────────────────────────────
        self.in_trade     = True
        self.direction    = direction
        self.entry_type   = sig_type

        # Increment pullback counter for this direction
        if sig_type == "pullback":
            self._pullback_count[direction] = self._pullback_count.get(direction, 0) + 1
            self.logger.info(
                f"[PullbackCount] {direction} pullback count now "
                f"{self._pullback_count[direction]}/{config.MAX_PULLBACK_PER_DIR}")
        # Determine session and set SL/target accordingly
        early_end = datetime.time(*map(int, config.EARLY_SESSION_END.split(":")))
        is_early  = t.time() < early_end
        if is_early:
            used_sl     = config.EARLY_SL_PTS
            used_target = config.EARLY_TARGET_PTS
            trail_desc  = (f"+{config.EARLY_TRAIL_1_TRIGGER}→+{config.EARLY_TRAIL_1_LOCK}, "
                           f"+{config.EARLY_TRAIL_2_TRIGGER}→+{config.EARLY_TRAIL_2_LOCK}, "
                           f"book@+{config.EARLY_TARGET_PTS}")
        else:
            used_sl     = config.NORMAL_SL_PTS
            used_target = config.NORMAL_TARGET_PTS
            trail_desc  = (f"+20→BE+1, +30→+10, +35→+20, +40→+25, "
                           f"then trail every {config.NORMAL_TRAIL_STEP_SIZE}pts")

        self.strike       = info["strike"]
        self.option_token = info["token"]
        self.entry_price  = fill_px
        self.entry_time   = t
        self.entry_vwap   = futures_vwap
        self.sl_price     = round(fill_px - used_sl, 2)
        self.target_price = round(fill_px + used_target, 2)
        self.peak_price   = fill_px
        self.low_price    = fill_px
        self.trail_step   = ""
        self.trail_active = False
        self.option_ltp   = fill_px
        self.sl_pts       = used_sl
        self.target_pts   = used_target

        # Track early session state
        if is_early:
            self.early_trade_count  += 1
            self.early_last_entry_t  = t

        self.trade_count += 1
        self._subscribe_option(info["token"])

        session_tag = "EARLY (9:16–9:39)" if is_early else "NORMAL (9:40+)"
        print(f"\n✅ ENTRY #{self.trade_count}  [{session_tag}]")
        print(f"   Direction  : {direction}  ({sig_type.upper()})")
        print(f"   Strike     : {info['strike']}  exp={info['expiry_str']}")
        print(f"   Entry      : Rs {fill_px:.2f}")
        print(f"   SL         : Rs {self.sl_price:.2f}  (−{used_sl} pts)")
        print(f"   Target     : Rs {self.target_price:.2f}  (+{used_target} pts)")
        print(f"   Trail      : {trail_desc}")
        print(f"   Futures    : {futures_ltp:.2f}  VWAP={futures_vwap:.2f}")
        print(f"   VIX regime : {'HIGH' if self.high_vix else 'LOW'} ({self.current_vix:.1f})")

        self.telegram.alert_entry(
            direction=direction, strike=info["strike"],
            entry_price=fill_px, vwap=futures_vwap,
            sl=self.sl_price, target=self.target_price, qty=self.qty,
        )

    # ── Exit handler ─────────────────────────────────────

    def _exit_trade(self, exit_ltp: float, reason: str):
        if not self.in_trade:
            return

        self.in_trade = False
        exit_time     = now_ist()

        actual_exit = self.opt_mgr.place_exit_order(
            token=self.option_token, strike=self.strike,
            direction=self.direction, qty=self.qty, reason=reason,
        )
        exit_price = actual_exit if actual_exit else exit_ltp

        pts_gained = round(exit_price - self.entry_price, 2)
        pnl_rs     = round(pts_gained * self.qty, 2)
        cost       = OptionManager.calc_trade_cost(self.entry_price, exit_price, self.qty)
        net_rs     = round(pnl_rs - cost, 2)

        self.day_pnl_rs += net_rs
        self.cap_mgr.update_after_trade(net_rs)
        duration = round((exit_time - self.entry_time).total_seconds() / 60, 1)

        # ── Update day counters ───────────────────────────
        is_sl = reason in ("SL", "Trail SL")

        if is_sl:
            self.consec_sl += 1
        else:
            self.consec_sl  = 0   # any non-SL exit resets streak

        # Track early session result for loss-wait logic
        early_end = datetime.time(*map(int, config.EARLY_SESSION_END.split(":")))
        if exit_time.time() < early_end:
            self.early_last_result = "loss" if pts_gained < 0 else "win"

        # ── Direction fatigue tracking ────────────────────
        if pts_gained >= config.FATIGUE_MIN_PROFIT_PTS:
            dw = self._dir_wins[self.direction]
            dw["wins"]          += 1
            dw["last_win_time"]  = exit_time

        print(f"\n{'='*55}")
        print(f"  EXIT  #{self.trade_count} — {reason}")
        print(f"  {self.direction} {self.strike}  "
              f"{self.entry_time.strftime('%H:%M')}→{exit_time.strftime('%H:%M')} "
              f"({duration}m)")
        print(f"  Entry={self.entry_price:.2f}  Exit={exit_price:.2f}  "
              f"Peak={self.peak_price:.2f}")
        print(f"  P&L: {pts_gained:+.2f}pts = Rs{pnl_rs:+.0f}  "
              f"Cost={cost:.0f}  Net=Rs{net_rs:+.0f}")
        print(f"  Day P&L: Rs{self.day_pnl_rs:+,.0f}  ConsecSL={self.consec_sl}")

        self.telegram.alert_exit(
            direction=self.direction, strike=self.strike,
            entry_price=self.entry_price, exit_price=exit_price,
            pnl_pts=pts_gained, net_rs=net_rs, reason=reason,
        )

        early_end  = datetime.time(*map(int, config.EARLY_SESSION_END.split(":")))
        is_early_x = self.entry_time.time() < early_end
        trend_det  = self.futures_engine.vwap_trend_detail()

        self.report_mgr.log_trade({
            "entry_time"              : self.entry_time,
            "exit_time"               : exit_time,
            "direction"               : self.direction,
            "strike"                  : self.strike,
            "expiry"                  : self.opt_mgr.expiry_str,
            "atm_at_entry"            : round(self.entry_vwap / 50) * 50,
            "entry_price"             : self.entry_price,
            "exit_price"              : exit_price,
            "peak_price"              : self.peak_price,
            "low_price"               : self.low_price,
            "entry_vwap"              : self.entry_vwap,
            "exit_vwap"               : self.futures_engine.vwap,
            "entry_dist"              : round(abs(self.entry_price - self.entry_vwap), 2),
            "futures_at_entry"        : self.futures_engine.ltp,
            "futures_at_exit"         : self.futures_engine.ltp,
            "pnl_rs"                  : pnl_rs,
            "total_cost"              : cost,
            "net_rs"                  : net_rs,
            "exit_reason"             : reason,
            "exit_phase"              : reason,
            "breakeven_done"          : self.trail_active,
            "trail_active"            : self.trail_active,
            "trail_step_reached"      : self.trail_step,
            "signal_type"             : self.entry_type,
            "session"                 : "EARLY" if is_early_x else "NORMAL",
            "sl_pts_used"             : self.sl_pts,
            "target_pts_used"         : self.target_pts,
            "sl_price"                : self.entry_price - self.sl_pts,
            "target_price"            : self.entry_price + self.target_pts,
            "vwap_trend_at_entry"     : trend_det.get("trend", ""),
            "early_trade_count"       : self.early_trade_count if is_early_x else "",
            "pullback_count_at_entry" : self._pullback_count.get(self.direction, 0),
            "high_vix"                : self.high_vix,
        })

        if self.option_token:
            self._unsubscribe_option(self.option_token)
            self.option_token = None

    # ── Direction fatigue check ───────────────────────────

    def _is_direction_fatigued(self, direction: str, t: datetime.datetime) -> bool:
        """
        Returns True if this direction should be skipped due to fatigue.
        Fatigue: >= DIRECTION_FATIGUE_COUNT profitable exits in this direction.
        Resets after DIRECTION_COOLDOWN_MINS minutes since last win.
        """
        dw = self._dir_wins[direction]
        if dw["wins"] < config.DIRECTION_FATIGUE_COUNT:
            return False
        if dw["last_win_time"] is None:
            return False
        mins_since = (t - dw["last_win_time"]).total_seconds() / 60
        if mins_since >= config.DIRECTION_COOLDOWN_MINS:
            # Cooldown expired — reset wins
            dw["wins"]         = 0
            dw["last_win_time"] = None
            return False
        return True   # still in cooldown

    # ── Helpers ──────────────────────────────────────────

    def _is_market_hours(self, t: datetime.datetime) -> bool:
        open_t  = datetime.time(*map(int, config.MARKET_OPEN.split(":")))
        close_t = datetime.time(*map(int, config.SQUARE_OFF_TIME.split(":")))
        return open_t <= t.time() <= close_t

    def _on_reconnect(self, new_client):
        self.client = new_client
        self.opt_mgr.client = new_client
        self._setup_websocket()
        time.sleep(2)
        self._resubscribe_all()

    def _square_off_all(self):
        if self.in_trade:
            print(f"\n[SquareOff] 3:25 PM — closing {self.direction} {self.strike}")
            ltp = self.option_ltp if self.option_ltp > 0 else self.entry_price
            self._exit_trade(ltp, "Square-off 3:25 PM")

    def _end_of_day(self):
        print("\n" + "="*60 + "\n  END OF DAY")
        report = self.report_mgr.generate_daily_report()
        print(report)
        self.cap_mgr.print_status()
        self.report_mgr.close()
        self.telegram.alert_shutdown(
            trades=self.trade_count, net_pnl=self.day_pnl_rs)

    def _print_status(self):
        t     = now_ist()
        state = self.futures_engine.get_state()
        pos   = "▲" if state["was_above"] else "▼"
        detail    = self.futures_engine.vwap_trend_detail()
        trend_sym = "↑" if detail["trend"] == "rising" else (
                    "↓" if detail["trend"] == "falling" else "→")
        pb_ce = self._pullback_count.get("CE", 0)
        pb_pe = self._pullback_count.get("PE", 0)
        print(f"\n[{t.strftime('%H:%M:%S')}] "
              f"F={state['ltp']:.2f} VWAP={state['vwap']:.2f}{pos} "
              f"Trend={trend_sym}({detail['change']:+.2f}/{detail['snapshots']}min) "
              f"VIX={self.current_vix:.1f} "
              f"Trades={self.trade_count} ConsecSL={self.consec_sl} "
              f"PB(CE={pb_ce}/PE={pb_pe})", end="")
        if self.in_trade:
            unreal = round((self.option_ltp - self.entry_price) * self.qty, 0)
            trail_tag = "🔒" if self.trail_active else ""
            print(f" | {self.direction}{self.strike} "
                  f"E={self.entry_price:.0f} L={self.option_ltp:.0f} "
                  f"SL={self.sl_price:.0f} TGT={self.target_price:.0f} "
                  f"Unreal=Rs{unreal:+.0f}{trail_tag}", end="")
        print(f" | DayPnL=Rs{self.day_pnl_rs:+,.0f}")

    def _check_no_tick(self):
        t       = now_ist()
        elapsed = (t - self._last_tick_time).total_seconds()
        if elapsed > 300 and self._is_market_hours(t) and not self._circuit_alerted:
            msg = f"No tick for {elapsed/60:.0f} mins — circuit halt?"
            self.logger.warning(f"[Circuit] {msg}")
            self.telegram.alert_risk(msg)
            self._circuit_alerted = True

    def _handle_sigterm(self, signum, frame):
        print(f"\n[Shutdown] Signal {signum} received — stopping...")
        self._running = False

    def _graceful_shutdown(self):
        print("\n[Shutdown] Saving state and exiting...")
        try:
            self._square_off_all()
        except Exception as e:
            self.logger.error(f"[Shutdown] square_off error: {e}")
        try:
            self._end_of_day()
        except Exception as e:
            self.logger.error(f"[Shutdown] end_of_day error: {e}")
        try:
            if self.session_mgr:
                self.session_mgr.stop()
        except Exception:
            pass
        print("[Shutdown] Done.")
        import os as _os
        _os._exit(0)   # force exit — kills all threads including WS and pre-load

    # ── Main loop ─────────────────────────────────────────

    def run(self):
        self.initialize()
        self._subscribe_futures()

        print(f"[Main] WS started — entry from {config.ENTRY_START} IST")
        print(f"[Main] Square-off at {config.SQUARE_OFF_TIME} IST\n")

        sq_done         = False
        last_status_min = -1
        last_save_min   = -1

        print(f"[Main] Entry from 09:16 IST | Square-off at {config.SQUARE_OFF_TIME} IST")
        print(f"[Main] Auto-shutdown at {self._shutdown_time.strftime('%H:%M:%S')} IST\n")

        try:
            while self._running:
                t = now_ist()

                # ── Auto-shutdown: 5h 50m from start time ─────────
                if t >= self._shutdown_time:
                    print(f"\n[Main] Auto-shutdown time reached "
                          f"({self._shutdown_time.strftime('%H:%M:%S')}) — stopping")
                    break

                # ── Square-off at config time ──────────────────────
                sq_time = datetime.time(*map(int, config.SQUARE_OFF_TIME.split(":")))
                if t.time() >= sq_time and not sq_done:
                    self._square_off_all()
                    sq_done = True

                # ── Status print every minute ──────────────────────
                if t.minute != last_status_min:
                    self._print_status()
                    self._check_no_tick()
                    last_status_min = t.minute

                # ── Autosave every 30 min ─────────────────────────
                if t.minute % 30 == 0 and t.minute != last_save_min:
                    self.cap_mgr._save()
                    last_save_min = t.minute

                time.sleep(1)

        except KeyboardInterrupt:
            print("\n[Main] Keyboard interrupt — shutting down")
        finally:
            self._graceful_shutdown()


if __name__ == "__main__":
    algo = FuturesVWAPAlgo()
    algo.run()
