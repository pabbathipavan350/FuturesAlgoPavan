# ============================================================
# MAIN.PY — Nifty 9:29–9:45 VWAP Touch Options Algo  v5
# ============================================================
#
# STRATEGY SUMMARY
# ─────────────────
#   Window  : 9:29 AM to 9:45 AM (entries only in this window)
#   Signal  : Nifty Futures comes within 5 pts of VWAP
#   CE trade: price above VWAP → buy ITM CE
#   PE trade: price below VWAP → buy ITM PE
#   SL      : -10 pts option premium (hard stop)
#   Target  : +40 pts option premium (full exit)
#   Trail   : +10 → SL to breakeven
#             +20 → lock +10
#             +30 → lock +20
#   Re-entry: zone re-arms once price moves 8+ pts away from VWAP
#   After 9:45: NO new entries; any open trade managed to SL/TGT/3:25
#
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
        self.futures_token = None
        self.option_token  = None

        # VIX (info only)
        self.current_vix = 0.0
        self.high_vix    = True

        # ── Position state ────────────────────────────────
        self.in_trade     = False
        self.direction    = None
        self.strike       = None
        self.entry_price  = 0.0
        self.entry_time   = None
        self.entry_vwap   = 0.0
        self.sl_price     = 0.0     # option premium SL (absolute)
        self.target_price = 0.0     # option premium target (absolute)
        self.peak_price   = 0.0
        self.low_price    = 0.0
        self.trail_step   = ""      # last trail label
        self.trail_active = False
        self.option_ltp   = 0.0
        self.qty          = config.LOTS * config.LOT_SIZE

        # ── Day counters ──────────────────────────────────
        self.day_pnl_rs  = 0.0
        self.trade_count = 0
        self.consec_sl   = 0
        self.day_stopped = False

        # ── Pre-load ──────────────────────────────────────
        self._preloaded      = False
        self._pre_subscribed: dict = {}
        self._pre_ltp:        dict = {}

        # ── Reconnect guard ───────────────────────────────
        self._reconnecting   = False
        self._reconnect_lock = threading.Lock()

        # ── Timers ────────────────────────────────────────
        self._start_time    = now_ist()
        self._shutdown_time = self._start_time + datetime.timedelta(hours=6, minutes=30)

        # No-tick watchdog
        self._last_tick_time  = now_ist()
        self._circuit_alerted = False

        self._running = True
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        signal.signal(signal.SIGINT,  self._handle_sigterm)

    # ──────────────────────────────────────────────────────
    # Initialisation
    # ──────────────────────────────────────────────────────

    def initialize(self):
        print("\n" + "="*60)
        print("  Nifty 9:29–9:45 VWAP Touch Options Algo  v5")
        print(f"  Mode    : {'*** PAPER TRADE ***' if config.PAPER_TRADE else '*** LIVE ***'}")
        print(f"  Capital : Rs {config.TOTAL_CAPITAL:,.0f}  |  "
              f"{config.LOTS} lots x {config.LOT_SIZE}")
        print("="*60)
        print()
        print("  STRATEGY RULES:")
        print(f"  Entry window  : {config.ENTRY_WINDOW_START} – {config.ENTRY_WINDOW_END}")
        print(f"  Signal        : Futures within {config.VWAP_TOUCH_DIST}pts of VWAP")
        print(f"  Direction     : Above VWAP → CE  |  Below VWAP → PE  (ITM)")
        print(f"  Stop loss     : -{config.SL_PTS}pts option premium (hard)")
        print(f"  Target        : +{config.TARGET_PTS}pts option premium (full exit)")
        print(f"  Trail         : +{config.TRAIL_1_TRIGGER}→BE  "
              f"+{config.TRAIL_2_TRIGGER}→lock+{config.TRAIL_2_LOCK:.0f}  "
              f"+{config.TRAIL_3_TRIGGER}→lock+{config.TRAIL_3_LOCK:.0f}")
        print(f"  Re-arm after  : price moves {config.VWAP_RESET_DIST}pts from VWAP")
        print(f"  Daily guards  : {config.MAX_CONSEC_SL} consec SL stops day  |  "
              f"Loss limit Rs{config.MAX_DAILY_LOSS_RS:,.0f}")
        print("="*60)

        self._start_time    = now_ist()
        self._shutdown_time = self._start_time + datetime.timedelta(hours=6, minutes=30)
        print(f"\n[Init] Started       : {self._start_time.strftime('%H:%M:%S')} IST")
        print(f"[Init] Auto-shutdown : {self._shutdown_time.strftime('%H:%M:%S')} IST")

        self.client      = get_kotak_session()
        self.session_mgr = SessionManager(self.client, get_kotak_session)
        self.session_mgr.on_reconnect = self._on_reconnect
        self.session_mgr.start()

        self.cap_mgr    = CapitalManager()
        self.opt_mgr    = OptionManager(self.client)
        self.report_mgr = ReportManager(self.cap_mgr)

        self._resolve_futures_token()
        self._fetch_vix()
        self._setup_websocket()

        exp = self.opt_mgr.expiry_date
        print(f"[Init] Expiry   : {exp.strftime('%d %b %Y')} "
              f"(in {(exp - datetime.date.today()).days}d)")
        print(f"[Init] VIX      : {self.current_vix:.1f}  (info only)")
        print(f"[Init] Futures  : token={self.futures_token}")

        self._preload_at_startup()

        self.telegram.alert_startup(
            mode   = "PAPER" if config.PAPER_TRADE else "LIVE",
            expiry = str(exp),
            atm    = f"VIX={self.current_vix:.1f}",
        )
        print("\n[Init] Ready — watching for VWAP touches from "
              f"{config.ENTRY_WINDOW_START} IST\n")

    def _preload_at_startup(self):
        spot = self._fetch_spot_from_futures_token()
        if spot <= 0:
            spot = self._fetch_nifty_spot()
        if spot <= 0:
            spot = 24000.0
            print(f"[PreLoad] Spot fetch failed — using estimate {spot:.0f}")
        print(f"[PreLoad] Startup spot: {spot:.0f} — pre-loading strikes...")
        try:
            self.opt_mgr.preload_strikes(spot)
            self._preloaded = True
            self._subscribe_preloaded_options()
            print("[PreLoad] Done — all strikes cached and subscribed")
        except Exception as e:
            self.logger.error(f"[PreLoad] Error: {e}", exc_info=True)
            print(f"[PreLoad] Warning — preload error: {e}")

    def _fetch_spot_from_futures_token(self) -> float:
        if not self.futures_token:
            return 0.0
        try:
            resp = self.client.quotes(
                instrument_tokens=[{"instrument_token": self.futures_token,
                                    "exchange_segment": config.FO_SEGMENT}],
                quote_type="ltp",
            )
            data = (resp if isinstance(resp, list) else
                    resp.get("message") or resp.get("data") or []
                    if isinstance(resp, dict) else [])
            if data:
                for f in ("ltp", "ltP", "last_price", "lastPrice", "close"):
                    v = data[0].get(f)
                    if v and float(v) > 0:
                        return float(v)
        except Exception as e:
            self.logger.debug(f"_fetch_spot_from_futures_token: {e}")
        return 0.0

    def _fetch_nifty_spot(self) -> float:
        try:
            resp = self.client.quotes(
                instrument_tokens=[{"instrument_token": config.NIFTY_INDEX_TOKEN,
                                    "exchange_segment": config.CM_SEGMENT}],
                quote_type="ltp"
            )
            data = (resp if isinstance(resp, dict)
                    else (resp.get("message") or resp.get("data") or []))
            if not isinstance(data, list):
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
        INDIA_VIX_TOKEN = "26074"
        try:
            resp = self.client.quotes(
                instrument_tokens=[{"instrument_token": INDIA_VIX_TOKEN,
                                    "exchange_segment": config.CM_SEGMENT}],
                quote_type="ltp"
            )
            data = (resp if isinstance(resp, list) else
                    resp.get("message") or resp.get("data") or []
                    if isinstance(resp, dict) else [])
            if data:
                vix = float(data[0].get("ltp") or data[0].get("ltP") or 0)
                if vix > 0:
                    self.current_vix = vix
        except Exception as e:
            self.logger.debug(f"VIX fetch: {e}")
        if self.current_vix <= 0:
            self.current_vix = 20.0
        self.high_vix = self.current_vix > config.VIX_HIGH_THRESHOLD
        self.report_mgr.set_vix(self.current_vix)

    def _resolve_futures_token(self):
        expiry_str = self.opt_mgr.expiry_str
        print(f"[Init] Resolving futures token for expiry {expiry_str}...")
        self.futures_token = find_futures_token(self.client, self.opt_mgr.expiry_date)
        if not self.futures_token:
            raise RuntimeError(
                f"Could not resolve Nifty futures token for {expiry_str}.")

    # ──────────────────────────────────────────────────────
    # WebSocket
    # ──────────────────────────────────────────────────────

    def _setup_websocket(self):
        self.client.on_message = self._on_message
        self.client.on_error   = self._on_ws_error
        self.client.on_close   = self._on_ws_close
        self.client.on_open    = self._on_ws_open

    def _on_ws_open(self, *args):
        print("[WS] Connected")
        threading.Thread(target=self._resubscribe_all,
                         daemon=True, name="WSSub").start()

    def _resubscribe_all(self):
        time.sleep(0.5)
        self._subscribe_futures()
        for d, tok in self._pre_subscribed.items():
            try:
                self.client.subscribe(
                    instrument_tokens=[{"instrument_token": tok,
                                        "exchange_segment": config.FO_SEGMENT}],
                    isIndex=False, isDepth=False,
                )
            except Exception as e:
                self.logger.debug(f"[WS] Re-sub pre-loaded {d}: {e}")
        if self.option_token:
            try:
                self._subscribe_option(self.option_token)
            except Exception as e:
                self.logger.debug(f"[WS] Re-sub active option: {e}")

    def _on_ws_error(self, error):
        s = str(error)
        if "already closed" in s.lower() or "nonetype" in s.lower():
            self.logger.debug(f"[WS] Error (expected): {error}")
        else:
            self.logger.error(f"[WS] Error: {error}")

    def _on_ws_close(self, *args):
        if now_ist().time() >= datetime.time(15, 0):
            self.logger.debug("[WS] Closed after 15:00")
            return
        self.logger.warning("[WS] Closed")
        if not self._running:
            return
        with self._reconnect_lock:
            if self._reconnecting:
                return
            self._reconnecting = True
        threading.Thread(target=self._ws_reconnect_loop,
                         daemon=True, name="WSReconnect").start()

    def _ws_reconnect_loop(self):
        delays = [5, 10, 20, 30]
        try:
            for attempt, delay in enumerate(delays, 1):
                if not self._running:
                    return
                if now_ist().time() >= datetime.time(15, 0):
                    return
                print(f"\n[WS] Reconnect {attempt}/{len(delays)} in {delay}s...")
                time.sleep(delay)
                if not self._running:
                    return
                try:
                    self._setup_websocket()
                    self._subscribe_futures()
                    print(f"[WS] Reconnected (attempt {attempt})")
                    return
                except Exception as e:
                    self.logger.error(f"[WS] Reconnect {attempt} failed: {e}")
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
            self.logger.error(f"Subscribe option {token}: {e}")

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

    def _subscribe_preloaded_options(self):
        for direction in ["CE", "PE"]:
            candidates = self.opt_mgr._strike_cache.get(direction, [])
            if not candidates:
                continue
            best = candidates[0]
            tok  = best["token"]
            self._pre_subscribed[direction] = tok
            self._pre_ltp[direction]        = best.get("ltp", 0.0)
            try:
                self.client.subscribe(
                    instrument_tokens=[{"instrument_token": tok,
                                        "exchange_segment": config.FO_SEGMENT}],
                    isIndex=False, isDepth=False,
                )
                print(f"[PreSub] {direction} {best['strike']} "
                      f"token={tok}  LTP~{best.get('ltp', 0.0):.1f}")
            except Exception as e:
                self.logger.error(f"[PreSub] {direction}: {e}")

    # ──────────────────────────────────────────────────────
    # Tick handlers
    # ──────────────────────────────────────────────────────

    def _on_message(self, message):
        try:
            if not isinstance(message, dict):
                return
            if message.get("type", "") not in ("stock_feed", "sf", "index_feed", "if"):
                return
            ticks = message.get("data", [])
            if not ticks:
                return
            for tick in ticks:
                token = str(tick.get("tk") or tick.get("token") or
                            tick.get("instrument_token") or "")
                ltp   = float(tick.get("ltp") or tick.get("ltP") or 0)
                if ltp <= 0:
                    continue
                self._last_tick_time  = now_ist()
                self._circuit_alerted = False

                # Track pre-subscribed option LTPs
                for _d, _tok in self._pre_subscribed.items():
                    if token == str(_tok):
                        self._pre_ltp[_d] = ltp
                        break

                if token == str(self.futures_token):
                    self._on_futures_tick(tick)
                elif token == str(self.option_token):
                    self._on_option_tick(tick)

                self.futures_engine.on_option_tick(token, tick)

        except Exception as e:
            self.logger.error(f"_on_message: {e}", exc_info=True)

    def _on_futures_tick(self, tick: dict):
        """Feed to engine; check for new signal if not in trade."""
        self.futures_engine.on_tick(tick)

        if not self._is_market_hours(now_ist()):
            return

        # If in trade, no new entries — manage from option tick
        if self.in_trade:
            return

        # Day guards
        if self.day_stopped:
            return

        sig, sig_type = self.futures_engine.check_signal()
        if not sig:
            return

        state = self.futures_engine.get_state()
        self._on_signal(sig, sig_type, state["ltp"], state["vwap"], now_ist())

    def _on_option_tick(self, tick: dict):
        """
        Manage open trade: apply strict trailing SL, check SL and target.
        """
        ltp = float(tick.get("ltp") or tick.get("ltP") or tick.get("lp") or 0)
        if ltp <= 0 or not self.in_trade:
            return

        self.option_ltp = ltp
        if ltp > self.peak_price:
            self.peak_price = ltp
        if self.low_price == 0.0 or ltp < self.low_price:
            self.low_price = ltp

        profit_pts = ltp - self.entry_price

        # ── Strict Trailing SL ────────────────────────────
        # +10 → SL = entry (breakeven)
        # +20 → SL = entry + 10
        # +30 → SL = entry + 20
        new_sl = self.sl_price

        if profit_pts >= config.TRAIL_3_TRIGGER:        # +30
            candidate = round(self.entry_price + config.TRAIL_3_LOCK, 2)
            if candidate > new_sl:
                new_sl = candidate
        elif profit_pts >= config.TRAIL_2_TRIGGER:      # +20
            candidate = round(self.entry_price + config.TRAIL_2_LOCK, 2)
            if candidate > new_sl:
                new_sl = candidate
        elif profit_pts >= config.TRAIL_1_TRIGGER:      # +10
            candidate = round(self.entry_price + config.TRAIL_1_LOCK, 2)
            if candidate > new_sl:
                new_sl = candidate

        if new_sl > self.sl_price:
            lock = new_sl - self.entry_price
            self.logger.info(f"[Trail] SL {self.sl_price:.2f} -> {new_sl:.2f} "
                             f"(+{profit_pts:.1f}pts profit, lock=+{lock:.0f})")
            print(f"  [Trail] SL raised: {self.sl_price:.2f} -> {new_sl:.2f}  "
                  f"(profit={profit_pts:+.1f}pts  locked=+{lock:.0f}pts)")
            self.sl_price     = new_sl
            self.trail_active = True
            self.trail_step   = (f"+{profit_pts:.0f}pts->"
                                 f"SL+{lock:.0f}")

        # ── Target hit ────────────────────────────────────
        if ltp >= self.target_price:
            self.logger.info(f"[Exit] TARGET ltp={ltp:.2f} tgt={self.target_price:.2f}")
            print(f"\n  [TARGET] +{config.TARGET_PTS}pts hit — exiting {self.direction}")
            self._exit_trade(ltp, f"Target +{config.TARGET_PTS}pts")
            return

        # ── SL hit ────────────────────────────────────────
        if ltp <= self.sl_price:
            reason = "Trail SL" if self.trail_active else "SL"
            self.logger.info(f"[Exit] {reason} ltp={ltp:.2f} sl={self.sl_price:.2f}")
            print(f"\n  [SL] {reason} hit — exiting {self.direction} "
                  f"(LTP={ltp:.2f} SL={self.sl_price:.2f})")
            self._exit_trade(ltp, reason)

    # ──────────────────────────────────────────────────────
    # Signal → Entry
    # ──────────────────────────────────────────────────────

    def _on_signal(self, direction: str, sig_type: str,
                   futures_ltp: float, futures_vwap: float,
                   t: datetime.datetime):

        # ── Daily guards ──────────────────────────────────
        if self.trade_count >= config.MAX_DAILY_TRADES:
            if not self.day_stopped:
                self.day_stopped = True
                msg = f"{config.MAX_DAILY_TRADES} trades done — no more entries today"
                print(f"\n[Guard] {msg}")
                self.telegram.alert_risk(msg)
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
                msg = (f"Daily loss limit Rs{config.MAX_DAILY_LOSS_RS:,.0f} "
                       f"hit — stopped")
                print(f"\n[Guard] {msg}")
                self.telegram.alert_risk(msg)
            return

        # ── Window guard ──────────────────────────────────
        # Engine already restricts signals to window, but double-check.
        win_start = datetime.time(*map(int, config.ENTRY_WINDOW_START.split(":")))
        win_end   = datetime.time(*map(int, config.ENTRY_WINDOW_END.split(":")))
        if not (win_start <= t.time() <= win_end):
            self.logger.info(f"[Guard] Signal outside entry window "
                             f"({t.strftime('%H:%M:%S')}) — ignored")
            return

        # ── Square-off / expiry guards ────────────────────
        sq_time = datetime.time(*map(int, config.SQUARE_OFF_TIME.split(":")))
        if t.time() >= sq_time:
            return
        if datetime.date.today() == self.opt_mgr.expiry_date:
            cutoff = datetime.time(*map(int, config.EXPIRY_DAY_CUTOFF.split(":")))
            if t.time() >= cutoff:
                return

        # ── Pick ITM strike ───────────────────────────────
        if not self._preloaded or not self.opt_mgr._strike_cache.get(direction):
            print(f"[Signal] Pre-load not ready — running live scan for {direction}")

        info = self.opt_mgr.pick_strike(futures_ltp, direction)
        if not info:
            self.logger.error(f"No ITM strike found for {direction}")
            self.telegram.alert_risk(
                f"No ITM {direction} strike at {t.strftime('%H:%M')}")
            return

        # ── Get live option LTP ───────────────────────────
        option_ltp = 0.0
        for d, pre_tok in self._pre_subscribed.items():
            if info["token"] == pre_tok and d == direction:
                ws_ltp = self._pre_ltp.get(d, 0.0)
                if ws_ltp > 0:
                    option_ltp = ws_ltp
                    print(f"[LTP] Pre-subscribed WS LTP: {option_ltp:.2f}")
                break

        if option_ltp <= 0:
            print(f"[LTP] Fetching live REST LTP for {direction} {info['strike']}...")
            option_ltp = fetch_ltp(self.client, info["token"])

        if option_ltp <= 0:
            self.logger.error(
                f"Cannot get LTP for {direction} {info['strike']} — abort")
            return

        print(f"[Strike] {direction} {info['strike']}  "
              f"delta={info['delta']:.2f}  OI={info['oi']:,}  LTP={option_ltp:.2f}")

        # ── Place buy order ───────────────────────────────
        fill = self.opt_mgr.place_buy_order(
            token=info["token"], strike=info["strike"],
            direction=direction, ltp=option_ltp,
        )
        if not fill:
            self.logger.error("Buy order not filled — aborting entry")
            return

        fill_px = fill["fill_price"]

        # ── Set trade state ───────────────────────────────
        self.in_trade     = True
        self.direction    = direction
        self.strike       = info["strike"]
        self.option_token = info["token"]
        self.entry_price  = fill_px
        self.entry_time   = t
        self.entry_vwap   = futures_vwap
        self.sl_price     = round(fill_px - config.SL_PTS, 2)
        self.target_price = round(fill_px + config.TARGET_PTS, 2)
        self.peak_price   = fill_px
        self.low_price    = fill_px
        self.trail_step   = ""
        self.trail_active = False
        self.option_ltp   = fill_px
        self.trade_count += 1

        self._subscribe_option(info["token"])

        print(f"\n{'='*55}")
        print(f"  ENTRY #{self.trade_count}  [{t.strftime('%H:%M:%S')}]")
        print(f"  Direction  : {direction}  (price {'above' if direction=='CE' else 'below'} VWAP)")
        print(f"  Strike     : {info['strike']}  exp={info['expiry_str']}")
        print(f"  Futures    : {futures_ltp:.2f}  VWAP={futures_vwap:.2f}  "
              f"dist={abs(futures_ltp-futures_vwap):.1f}pts")
        print(f"  Fill price : Rs {fill_px:.2f}")
        print(f"  Stop loss  : Rs {self.sl_price:.2f}  "
              f"(-{config.SL_PTS:.0f}pts hard)")
        print(f"  Target     : Rs {self.target_price:.2f}  "
              f"(+{config.TARGET_PTS:.0f}pts)")
        print(f"  Trail      : +{config.TRAIL_1_TRIGGER:.0f}→BE  "
              f"+{config.TRAIL_2_TRIGGER:.0f}→+{config.TRAIL_2_LOCK:.0f}  "
              f"+{config.TRAIL_3_TRIGGER:.0f}→+{config.TRAIL_3_LOCK:.0f}")
        print(f"{'='*55}\n")

        self.telegram.alert_entry(
            direction=direction, strike=info["strike"],
            entry_price=fill_px, vwap=futures_vwap,
            sl=self.sl_price, target=self.target_price, qty=self.qty,
        )

    # ──────────────────────────────────────────────────────
    # Exit handler
    # ──────────────────────────────────────────────────────

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
        cost       = OptionManager.calc_trade_cost(
            self.entry_price, exit_price, self.qty)
        net_rs     = round(pnl_rs - cost, 2)

        self.day_pnl_rs += net_rs
        self.cap_mgr.update_after_trade(net_rs)
        duration = round((exit_time - self.entry_time).total_seconds() / 60, 1)

        is_sl = reason in ("SL", "Trail SL")
        if is_sl:
            self.consec_sl += 1
        else:
            self.consec_sl = 0

        trend_det = self.futures_engine.vwap_trend_detail()

        print(f"\n{'='*55}")
        print(f"  EXIT #{self.trade_count} — {reason}")
        print(f"  {self.direction} {self.strike}  "
              f"{self.entry_time.strftime('%H:%M:%S')}→"
              f"{exit_time.strftime('%H:%M:%S')}  ({duration}m)")
        print(f"  Entry={self.entry_price:.2f}  "
              f"Exit={exit_price:.2f}  Peak={self.peak_price:.2f}")
        print(f"  P&L : {pts_gained:+.2f}pts  =  Rs{pnl_rs:+.0f}  "
              f"Cost={cost:.0f}  Net=Rs{net_rs:+.0f}")
        print(f"  Day P&L: Rs{self.day_pnl_rs:+,.0f}  "
              f"ConsecSL={self.consec_sl}")
        print(f"{'='*55}\n")

        self.telegram.alert_exit(
            direction=self.direction, strike=self.strike,
            entry_price=self.entry_price, exit_price=exit_price,
            pnl_pts=pts_gained, net_rs=net_rs, reason=reason,
        )

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
            "entry_dist"              : round(
                abs(self.futures_engine.ltp - self.entry_vwap), 2),
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
            "signal_type"             : "vwap_touch",
            "session"                 : "NORMAL",
            "sl_pts_used"             : config.SL_PTS,
            "target_pts_used"         : config.TARGET_PTS,
            "sl_price"                : self.entry_price - config.SL_PTS,
            "target_price"            : self.entry_price + config.TARGET_PTS,
            "vwap_trend_at_entry"     : trend_det.get("trend", ""),
            "early_trade_count"       : "",
            "pullback_count_at_entry" : self.trade_count,
            "high_vix"                : self.high_vix,
        })

        if self.option_token:
            self._unsubscribe_option(self.option_token)
            self.option_token = None

        # If the entry window is already closed and this was the last trade,
        # the run-loop will detect in_trade=False on its next iteration and
        # shut down. Print a hint so the user knows what's happening.
        win_end = datetime.time(*map(int, config.ENTRY_WINDOW_END.split(":")))
        if exit_time.time() > win_end:
            print(f"[Main] Trade closed after entry window — "
                  f"shutting down momentarily.")

    # ──────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────

    def _is_market_hours(self, t: datetime.datetime) -> bool:
        open_t  = datetime.time(*map(int, config.MARKET_OPEN.split(":")))
        close_t = datetime.time(*map(int, config.SQUARE_OFF_TIME.split(":")))
        return open_t <= t.time() <= close_t

    def _on_reconnect(self, new_client):
        self.client         = new_client
        self.opt_mgr.client = new_client
        self._setup_websocket()
        time.sleep(2)
        self._resubscribe_all()

    def _square_off_all(self):
        if self.in_trade:
            print(f"\n[SquareOff] {config.SQUARE_OFF_TIME} — "
                  f"closing {self.direction} {self.strike}")
            ltp = self.option_ltp if self.option_ltp > 0 else self.entry_price
            self._exit_trade(ltp, f"Square-off {config.SQUARE_OFF_TIME}")

    def _end_of_day(self):
        print("\n" + "="*60 + "\n  END OF DAY")
        print(f"  Signals fired  : {self.futures_engine.signals_fired}")
        print(f"  Trades taken   : {self.trade_count}")
        print(f"  Day P&L        : Rs {self.day_pnl_rs:+,.0f}")
        report = self.report_mgr.generate_daily_report()
        print(report)
        self.cap_mgr.print_status()
        self.report_mgr.close()
        self.telegram.alert_shutdown(
            trades=self.trade_count, net_pnl=self.day_pnl_rs)

    def _print_status(self):
        t     = now_ist()
        state = self.futures_engine.get_state()
        det   = self.futures_engine.vwap_trend_detail()
        f_ltp = state["ltp"]
        vwap  = state["vwap"]
        dist  = abs(f_ltp - vwap) if f_ltp > 0 and vwap > 0 else 0
        pos   = "^" if state.get("was_above") else "v"
        zone  = det.get("zone", "?")

        # Window status label
        win_start = datetime.time(*map(int, config.ENTRY_WINDOW_START.split(":")))
        win_end   = datetime.time(*map(int, config.ENTRY_WINDOW_END.split(":")))
        if t.time() < win_start:
            window_lbl = f"Window opens {config.ENTRY_WINDOW_START}"
        elif t.time() <= win_end:
            window_lbl = f"WINDOW OPEN [{zone}]"
        else:
            window_lbl = "Window closed"

        print(f"\n[{t.strftime('%H:%M:%S')}] "
              f"F={f_ltp:.2f} VWAP={vwap:.2f}{pos} dist={dist:.1f}pts  "
              f"{window_lbl}  "
              f"Signals={self.futures_engine.signals_fired}  "
              f"Trades={self.trade_count}  "
              f"ConsecSL={self.consec_sl}", end="")

        if self.in_trade:
            unreal     = round((self.option_ltp - self.entry_price) * self.qty, 0)
            trail_tag  = " [TRAIL]" if self.trail_active else ""
            print(f"  |  {self.direction}{self.strike} "
                  f"E={self.entry_price:.0f} L={self.option_ltp:.0f} "
                  f"SL={self.sl_price:.0f} TGT={self.target_price:.0f} "
                  f"Unreal=Rs{unreal:+.0f}{trail_tag}", end="")

        print(f"  |  DayPnL=Rs{self.day_pnl_rs:+,.0f}")

    def _check_no_tick(self):
        t       = now_ist()
        elapsed = (t - self._last_tick_time).total_seconds()
        if elapsed > 300 and self._is_market_hours(t) and not self._circuit_alerted:
            msg = f"No tick for {elapsed/60:.0f} mins — circuit halt?"
            self.logger.warning(f"[Circuit] {msg}")
            self.telegram.alert_risk(msg)
            self._circuit_alerted = True

    def _handle_sigterm(self, signum, frame):
        print(f"\n[Shutdown] Signal {signum} — stopping...")
        self._running = False

    def _graceful_shutdown(self):
        print("\n[Shutdown] Saving and exiting...")
        try:
            self._square_off_all()
        except Exception as e:
            self.logger.error(f"square_off: {e}")
        try:
            self._end_of_day()
        except Exception as e:
            self.logger.error(f"end_of_day: {e}")
        try:
            if self.session_mgr:
                self.session_mgr.stop()
        except Exception:
            pass
        print("[Shutdown] Done.")
        import os as _os
        _os._exit(0)

    # ──────────────────────────────────────────────────────
    # Main loop
    # ──────────────────────────────────────────────────────

    def run(self):
        self.initialize()
        self._subscribe_futures()

        print(f"[Main] Watching VWAP from {config.MARKET_OPEN} IST")
        print(f"[Main] Entry window  : "
              f"{config.ENTRY_WINDOW_START}–{config.ENTRY_WINDOW_END} IST")
        print(f"[Main] Square-off at : {config.SQUARE_OFF_TIME} IST\n")

        sq_done                  = False
        last_status_min          = -1
        last_save_min            = -1
        _window_close_announced  = False   # one-time "window closed" banner

        try:
            while self._running:
                t       = now_ist()
                now_t   = t.time()
                win_end = datetime.time(*map(int, config.ENTRY_WINDOW_END.split(":")))
                sq_time = datetime.time(*map(int, config.SQUARE_OFF_TIME.split(":")))

                # ── Hard auto-shutdown (safety net) ───────────────
                if t >= self._shutdown_time:
                    print(f"\n[Main] Auto-shutdown reached "
                          f"({self._shutdown_time.strftime('%H:%M:%S')}) — exiting")
                    break

                # ── 3:25 PM square-off safety net ─────────────────
                if now_t >= sq_time and not sq_done:
                    self._square_off_all()
                    sq_done = True

                # ── Post-window: entry window closed ──────────────
                # Once 9:45 passes, print a one-time notice.
                # As soon as the last trade exits (or if there was never
                # an open trade), shut down cleanly — no point idling.
                if now_t > win_end:
                    if not _window_close_announced:
                        print(f"\n{'='*55}")
                        print(f"[Main] Entry window closed ({config.ENTRY_WINDOW_END}).")
                        print(f"       VWAP tracked {self.futures_engine.tick_count} ticks  |  "
                              f"Signals fired: {self.futures_engine.signals_fired}")
                        if self.in_trade:
                            print(f"       Trade still open — managing "
                                  f"{self.direction} {self.strike} to SL/Target.")
                        else:
                            print(f"       No open trade — shutting down now.")
                        print(f"{'='*55}")
                        _window_close_announced = True

                    if not self.in_trade:
                        # All done — exit cleanly
                        break

                # ── Status every minute ───────────────────────────
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
            print("\n[Main] Keyboard interrupt")
        finally:
            self._graceful_shutdown()


if __name__ == "__main__":
    algo = FuturesVWAPAlgo()
    algo.run()
