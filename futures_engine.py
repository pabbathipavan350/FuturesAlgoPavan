# ============================================================
# FUTURES_ENGINE.PY — Nifty Futures VWAP Signal Detector v3
# ============================================================
# Changes from v2:
#
# 1. PROXIMITY FILTER on entries:
#    Fresh cross  : only fire if futures is within ±FRESH_CROSS_MAX_DIST of VWAP
#    Pullback     : only fire if futures is within PULLBACK_MIN/MAX_DIST of VWAP
#
# 2. OPTION VWAP tracking per token:
#    Tracks 'ap' field on any subscribed option token.
#    Used by main.py to confirm reversal vs pullback when
#    futures crosses VWAP while a trade is open.
#
# 3. Signal state remains one-shot (cleared after check_signal()).
# ============================================================

import logging
import config

logger = logging.getLogger(__name__)


class OptionVWAPTracker:
    """
    Tracks the 'ap' (session VWAP) field for a single option token.
    Used to confirm whether a mid-trade futures cross is a real
    reversal or just a pullback.
    """
    def __init__(self, token: str):
        self.token    = token
        self.ltp      = 0.0
        self.vwap     = 0.0
        self.ticks    = 0

    def on_tick(self, tick: dict):
        ltp  = float(tick.get("ltp") or tick.get("ltP") or tick.get("lp") or 0)
        vwap = float(tick.get("ap") or 0)
        if ltp > 0:
            self.ltp   = ltp
            self.ticks += 1
        if vwap > 0:
            self.vwap = vwap

    @property
    def is_above_vwap(self) -> bool | None:
        """None if not enough data, else True/False."""
        if self.vwap <= 0 or self.ltp <= 0:
            return None
        return self.ltp > self.vwap

    @property
    def is_ready(self) -> bool:
        return self.vwap > 0 and self.ltp > 0


class FuturesVWAPEngine:
    """
    Tracks a single futures token.
    Detects fresh VWAP crosses and pullback entries with proximity filter.

    Call on_tick() for every incoming WS tick on the futures token.
    Call check_signal() to get the pending signal (one-shot, clears after read).
    """

    def __init__(self):
        self.ltp         = 0.0
        self.vwap        = 0.0
        self.was_above   = None   # True/False/None
        self.tick_count  = 0
        self.signal      = None   # 'CE' | 'PE' | None
        self.signal_type = None   # 'cross' | 'pullback' | None

        # Pullback one-shot flags
        self._ce_pullback_fired = False
        self._pe_pullback_fired = False

        # Cross confirmation counters
        # A cross is only confirmed after the price stays on the new side
        # for CROSS_CONFIRM_TICKS consecutive ticks — eliminates fake wicks.
        self._cross_pending    = None   # 'CE' | 'PE' | None  — direction being confirmed
        self._cross_ticks      = 0      # consecutive ticks on new side so far

        # VWAP trend: one snapshot per completed minute
        # Tracks from first tick regardless of time, so by 9:30 there are
        # already ~15 minutes of history to assess trend direction.
        from collections import deque
        import datetime as _dt
        self._vwap_minute_history: deque = deque(maxlen=config.VWAP_TREND_LOOKBACK + 2)
        self._last_snapshot_minute: int  = -1   # minute of last snapshot taken

        # Option VWAP trackers — keyed by token string
        # Populated by main.py when option is subscribed
        self.option_trackers: dict[str, OptionVWAPTracker] = {}

    def on_tick(self, tick: dict):
        """Feed a Kotak Neo v2 WS tick for the futures token."""
        ltp  = float(tick.get("ltp") or tick.get("ltP") or tick.get("lp") or 0)
        vwap = float(tick.get("ap") or 0)

        if ltp <= 0 or vwap <= 0:
            return

        self.ltp        = ltp
        self.vwap       = vwap
        self.tick_count += 1

        # ── Per-minute VWAP snapshot ──────────────────────
        # On every tick, check if we've moved into a new minute.
        # If yes, snapshot the last VWAP value for that completed minute.
        import datetime as _dt
        now_min = _dt.datetime.now().minute
        if now_min != self._last_snapshot_minute:
            # New minute has started — snapshot the VWAP that just closed
            if self._last_snapshot_minute != -1:
                # Only record if we had a previous minute (not the very first tick)
                self._vwap_minute_history.append(vwap)
                logger.debug(
                    f"[TrendSnap] min={self._last_snapshot_minute} VWAP={vwap:.2f} "
                    f"history_len={len(self._vwap_minute_history)}")
            self._last_snapshot_minute = now_min

        if self.tick_count < config.VWAP_MIN_TICKS:
            self.was_above = (ltp > vwap)
            return

        if self.was_above is None:
            self.was_above = (ltp > vwap)
            logger.info(f"[FuturesEngine] Init: LTP={ltp:.2f} VWAP={vwap:.2f} "
                        f"pos={'above' if self.was_above else 'below'}")
            return

        currently_above = (ltp > vwap)
        dist_above      = ltp - vwap   # positive=above, negative=below

        # ── Fresh cross detection — with tick confirmation ──
        # On the first tick that crosses VWAP, start a pending counter.
        # The cross signal only fires after CROSS_CONFIRM_TICKS consecutive
        # ticks remain on the new side. A dip back resets the counter.
        if not self.was_above and currently_above:
            # Price just moved above VWAP — start / continue CE confirmation
            if self._cross_pending == "CE":
                self._cross_ticks += 1
            else:
                self._cross_pending = "CE"
                self._cross_ticks   = 1
                logger.info(f"[FuturesEngine] CROSS UP pending — LTP={ltp:.2f} "
                            f"VWAP={vwap:.2f} (tick 1/{config.CROSS_CONFIRM_TICKS})")
            self._ce_pullback_fired = False

        elif self.was_above and not currently_above:
            # Price just moved below VWAP — start / continue PE confirmation
            if self._cross_pending == "PE":
                self._cross_ticks += 1
            else:
                self._cross_pending = "PE"
                self._cross_ticks   = 1
                logger.info(f"[FuturesEngine] CROSS DOWN pending — LTP={ltp:.2f} "
                            f"VWAP={vwap:.2f} (tick 1/{config.CROSS_CONFIRM_TICKS})")
            self._pe_pullback_fired = False

        else:
            # Price is staying on the same side as was_above
            if self._cross_pending is not None:
                if currently_above and self._cross_pending == "CE":
                    # Still above — keep accumulating
                    self._cross_ticks += 1
                elif not currently_above and self._cross_pending == "PE":
                    # Still below — keep accumulating
                    self._cross_ticks += 1
                else:
                    # Flipped back — cross failed, reset
                    logger.info(f"[FuturesEngine] CROSS {self._cross_pending} CANCELLED "
                                f"— price flipped back after {self._cross_ticks} tick(s)")
                    self._cross_pending = None
                    self._cross_ticks   = 0

        # Fire signal once confirmation threshold is met
        if (self._cross_pending is not None and
                self._cross_ticks >= config.CROSS_CONFIRM_TICKS):
            direction = self._cross_pending
            dist      = abs(dist_above)
            if dist <= config.FRESH_CROSS_MAX_DIST:
                if not self.signal:
                    self.signal      = direction
                    self.signal_type = "cross"
                    logger.info(
                        f"[FuturesEngine] CROSS {direction} CONFIRMED "
                        f"LTP={ltp:.2f} VWAP={vwap:.2f} dist={dist:.1f}pts "
                        f"after {self._cross_ticks} ticks → signal {direction}")
                    print(f"  [Cross] {direction} confirmed after "
                          f"{self._cross_ticks} ticks above/below VWAP")
            else:
                logger.info(
                    f"[FuturesEngine] CROSS {direction} confirmed but dist={dist:.1f}pts "
                    f"> {config.FRESH_CROSS_MAX_DIST}pts — skipped")
            # Reset after firing (or distance skip)
            self._cross_pending = None
            self._cross_ticks   = 0

        # ── Pullback detection ─────────────────────────────
        # CE pullback: above VWAP, within [PULLBACK_MIN, PULLBACK_MAX]
        if (currently_above and
                config.PULLBACK_MIN_DIST < dist_above <= config.PULLBACK_MAX_DIST):
            if not self._ce_pullback_fired and not self.signal:
                self.signal             = "CE"
                self.signal_type        = "pullback"
                self._ce_pullback_fired = True
                logger.info(f"[FuturesEngine] PULLBACK CE  LTP={ltp:.2f} "
                            f"VWAP={vwap:.2f} dist={dist_above:.1f}pts")
        else:
            # Out of CE pullback zone — reset for next visit
            if not currently_above or dist_above > config.PULLBACK_MAX_DIST:
                self._ce_pullback_fired = False

        # PE pullback: below VWAP, within [PULLBACK_MIN, PULLBACK_MAX]
        dist_below = vwap - ltp
        if (not currently_above and
                config.PULLBACK_MIN_DIST < dist_below <= config.PULLBACK_MAX_DIST):
            if not self._pe_pullback_fired and not self.signal:
                self.signal             = "PE"
                self.signal_type        = "pullback"
                self._pe_pullback_fired = True
                logger.info(f"[FuturesEngine] PULLBACK PE  LTP={ltp:.2f} "
                            f"VWAP={vwap:.2f} dist={dist_below:.1f}pts")
        else:
            if currently_above or dist_below > config.PULLBACK_MAX_DIST:
                self._pe_pullback_fired = False

        self.was_above = currently_above

    def on_option_tick(self, token: str, tick: dict):
        """Feed a tick for any subscribed option token (for VWAP confirmation)."""
        tracker = self.option_trackers.get(token)
        if tracker:
            tracker.on_tick(tick)

    def register_option_token(self, token: str):
        """Register an option token to track its VWAP."""
        if token not in self.option_trackers:
            self.option_trackers[token] = OptionVWAPTracker(token)

    def unregister_option_token(self, token: str):
        """Remove tracker when option is unsubscribed."""
        self.option_trackers.pop(token, None)

    def get_option_vwap_position(self, token: str) -> bool | None:
        """
        Returns True if option LTP > option VWAP (above = pullback = hold).
        Returns False if option LTP < option VWAP (below = reversal = flip).
        Returns None if not enough data.
        """
        tracker = self.option_trackers.get(token)
        if tracker:
            return tracker.is_above_vwap
        return None

    def vwap_trend(self) -> str:
        """
        Returns 'rising', 'falling', or 'flat'.

        Uses per-minute VWAP snapshots (recorded at each minute boundary).
        Compares the oldest snapshot in the window against the newest.
        Needs at least VWAP_TREND_LOOKBACK minutes of history.

        'rising'  → VWAP up >= VWAP_TREND_MIN_CHANGE over window → CE only
        'falling' → VWAP down >= VWAP_TREND_MIN_CHANGE            → PE only
        'flat'    → not enough history or insufficient change      → both ok

        Tracking starts from first tick so history builds from ~9:15 AM,
        giving a reliable trend picture by 9:30 AM.
        """
        hist = self._vwap_minute_history
        if len(hist) < config.VWAP_TREND_LOOKBACK:
            return "flat"   # not enough minutes yet — allow both directions

        oldest  = hist[0]
        newest  = hist[-1]
        change  = newest - oldest
        min_chg = config.VWAP_TREND_MIN_CHANGE

        if change >= min_chg:
            return "rising"
        if change <= -min_chg:
            return "falling"
        return "flat"

    def vwap_trend_detail(self) -> dict:
        """Returns detailed trend state for logging/status display."""
        hist = self._vwap_minute_history
        return {
            "trend"       : self.vwap_trend(),
            "snapshots"   : len(hist),
            "oldest_vwap" : hist[0]  if hist else 0.0,
            "newest_vwap" : hist[-1] if hist else 0.0,
            "change"      : (hist[-1] - hist[0]) if len(hist) >= 2 else 0.0,
        }

    def check_signal(self) -> tuple:
        """
        Returns (signal, signal_type).
        signal      : 'CE' | 'PE' | None
        signal_type : 'cross' | 'pullback' | None
        Clears after read — one-shot.
        """
        sig, typ        = self.signal, self.signal_type
        self.signal     = None
        self.signal_type = None
        return sig, typ

    def get_state(self) -> dict:
        return {
            "ltp"      : self.ltp,
            "vwap"     : self.vwap,
            "was_above": self.was_above,
            "ticks"    : self.tick_count,
        }

    @property
    def is_ready(self) -> bool:
        return self.tick_count >= config.VWAP_MIN_TICKS and self.was_above is not None
