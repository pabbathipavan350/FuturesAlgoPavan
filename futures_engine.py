# ============================================================
# FUTURES_ENGINE.PY — 9:29-9:45 VWAP Touch Engine  v5
# ============================================================
#
# SIGNAL LOGIC
# ─────────────
# Only active between ENTRY_WINDOW_START (9:29) and
# ENTRY_WINDOW_END (9:45).
#
# On every tick:
#   dist = |futures_ltp - vwap|
#
#   If dist <= VWAP_TOUCH_DIST  AND  zone is ARMED:
#     • direction = CE  if ltp > vwap  (price above, buy call)
#     • direction = PE  if ltp < vwap  (price below, buy put)
#     • ltp within VWAP_DIRECTION_MIN of vwap → skip (too close)
#     → emit signal, DISARM zone
#
#   Zone re-arms when price moves >= VWAP_RESET_DIST away from
#   VWAP (either side). This prevents repeated triggers while
#   price is sitting on VWAP.
#
# STATES
# ──────
#   ARMED       — watching, will fire next time price touches zone
#   DISARMED    — just fired; waiting for price to move away
#   WINDOW_OVER — after 9:45, no new signals
# ============================================================

import logging
import datetime
import config

logger = logging.getLogger(__name__)

_ARMED    = "ARMED"
_DISARMED = "DISARMED"
_OVER     = "WINDOW_OVER"


def _now_ist() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)


class OptionVWAPTracker:
    """Kept for backward compat with main.py option VWAP confirm calls."""
    def __init__(self, token: str):
        self.token = token
        self.ltp   = 0.0
        self.vwap  = 0.0
        self.ticks = 0

    def on_tick(self, tick: dict):
        ltp  = float(tick.get("ltp") or tick.get("ltP") or tick.get("lp") or 0)
        vwap = float(tick.get("ap") or 0)
        if ltp > 0:
            self.ltp   = ltp
            self.ticks += 1
        if vwap > 0:
            self.vwap = vwap

    @property
    def is_above_vwap(self):
        if self.vwap <= 0 or self.ltp <= 0:
            return None
        return self.ltp > self.vwap

    @property
    def is_ready(self) -> bool:
        return self.vwap > 0 and self.ltp > 0


class FuturesVWAPEngine:
    """
    Monitors Nifty Futures ticks during 9:29–9:45.
    Fires a signal each time price freshly enters the VWAP touch zone
    (after having moved away by VWAP_RESET_DIST first).
    """

    def __init__(self):
        self.ltp        = 0.0
        self.vwap       = 0.0
        self.was_above  = None
        self.tick_count = 0

        # Zone state
        self._zone_state = _ARMED   # ARMED | DISARMED | WINDOW_OVER

        # Pending one-shot signal
        # Tuple (direction, 'vwap_touch') or None
        self._pending: tuple | None = None

        # Stats for status display
        self.signals_fired = 0

        # Option VWAP trackers (backward compat)
        self.option_trackers: dict = {}

        # Legacy vwap minute history (used by vwap_trend / vwap_trend_detail)
        from collections import deque
        self._vwap_minute_history = deque(maxlen=10)
        self._last_snapshot_minute = -1

    # ─────────────────────────────────────────────────────
    def on_tick(self, tick: dict):
        ltp  = float(tick.get("ltp") or tick.get("ltP") or tick.get("lp") or 0)
        vwap = float(tick.get("ap") or 0)

        if ltp <= 0 or vwap <= 0:
            return

        self.ltp        = ltp
        self.vwap       = vwap
        self.tick_count += 1
        self.was_above  = (ltp > vwap)

        # ── Per-minute VWAP snapshot — tracked from 9:15 onwards ──
        # Snapshots start from the very first tick so we have a full
        # VWAP picture well before the 9:29 entry window opens.
        now     = _now_ist()
        now_min = now.minute
        if now_min != self._last_snapshot_minute:
            if self._last_snapshot_minute == -1:
                # Very first tick of the day
                logger.info(f"[VWAP] Tracking started at "
                            f"{now.strftime('%H:%M:%S')} — "
                            f"LTP={ltp:.2f} VWAP={vwap:.2f}")
                print(f"[VWAP] Tracking started {now.strftime('%H:%M:%S')}  "
                      f"LTP={ltp:.2f}  VWAP={vwap:.2f}")
            else:
                self._vwap_minute_history.append(vwap)
                logger.debug(f"[VWAP] min={self._last_snapshot_minute:02d} "
                             f"snapshot={vwap:.2f}  "
                             f"history={len(self._vwap_minute_history)}")
            self._last_snapshot_minute = now_min

        if self.tick_count < config.VWAP_MIN_TICKS:
            return

        now_t = _now_ist().time()
        win_start = datetime.time(*map(int, config.ENTRY_WINDOW_START.split(":")))
        win_end   = datetime.time(*map(int, config.ENTRY_WINDOW_END.split(":")))

        # Outside window → no signals
        if not (win_start <= now_t <= win_end):
            if now_t > win_end and self._zone_state != _OVER:
                self._zone_state = _OVER
                logger.info("[Engine] Entry window closed (past 9:45)")
                print(f"\n[Engine] 9:45 reached — entry window closed. "
                      f"Total signals fired today: {self.signals_fired}")
            return

        dist = abs(ltp - vwap)

        # ── Re-arm logic ─────────────────────────────────
        # Once DISARMED, wait for price to move far enough away.
        if self._zone_state == _DISARMED:
            if dist >= config.VWAP_RESET_DIST:
                self._zone_state = _ARMED
                logger.info(f"[Engine] Zone RE-ARMED — dist={dist:.1f}pts >= "
                            f"{config.VWAP_RESET_DIST}pts  LTP={ltp:.2f} VWAP={vwap:.2f}")
                print(f"  [Engine] Zone re-armed (price moved {dist:.1f}pts from VWAP)")
            return   # still disarmed — don't check for entry

        # ── Entry trigger ─────────────────────────────────
        if self._zone_state == _ARMED and dist <= config.VWAP_TOUCH_DIST:
            # Determine direction
            price_diff = ltp - vwap
            if abs(price_diff) < config.VWAP_DIRECTION_MIN:
                # Price is right at VWAP — skip, direction ambiguous
                logger.debug(f"[Engine] At VWAP (diff={price_diff:.2f}pts) — "
                             f"direction unclear, skip")
                return

            direction = "CE" if price_diff > 0 else "PE"

            self._pending    = (direction, "vwap_touch")
            self._zone_state = _DISARMED
            self.signals_fired += 1

            side = "above" if direction == "CE" else "below"
            logger.info(
                f"[Engine] SIGNAL {direction} — LTP={ltp:.2f} is {dist:.1f}pts "
                f"{side} VWAP={vwap:.2f} — zone DISARMED until price moves "
                f"{config.VWAP_RESET_DIST}pts away"
            )
            print(f"\n[Engine] VWAP Touch! LTP={ltp:.2f}  VWAP={vwap:.2f}  "
                  f"dist={dist:.1f}pts  → {direction} signal  "
                  f"(will re-arm after {config.VWAP_RESET_DIST}pts move away)")

    # ─────────────────────────────────────────────────────
    def check_signal(self) -> tuple:
        """
        Returns (direction, signal_type) — one-shot, clears after read.
        Returns (None, None) when no signal pending.
        """
        if self._pending:
            sig, typ   = self._pending
            self._pending = None
            return sig, typ
        return None, None

    def is_window_open(self) -> bool:
        """True if we are still inside the 9:29–9:45 entry window."""
        now_t = _now_ist().time()
        ws = datetime.time(*map(int, config.ENTRY_WINDOW_START.split(":")))
        we = datetime.time(*map(int, config.ENTRY_WINDOW_END.split(":")))
        return ws <= now_t <= we

    # ─────────────────────────────────────────────────────
    # Option VWAP tracking (backward compat)
    # ─────────────────────────────────────────────────────
    def on_option_tick(self, token: str, tick: dict):
        tracker = self.option_trackers.get(token)
        if tracker:
            tracker.on_tick(tick)

    def register_option_token(self, token: str):
        if token not in self.option_trackers:
            self.option_trackers[token] = OptionVWAPTracker(token)

    def unregister_option_token(self, token: str):
        self.option_trackers.pop(token, None)

    def get_option_vwap_position(self, token: str):
        tracker = self.option_trackers.get(token)
        return tracker.is_above_vwap if tracker else None

    # ─────────────────────────────────────────────────────
    # Legacy compat helpers
    # ─────────────────────────────────────────────────────
    def get_state(self) -> dict:
        return {
            "ltp"      : self.ltp,
            "vwap"     : self.vwap,
            "was_above": self.was_above,
            "ticks"    : self.tick_count,
        }

    def vwap_trend(self) -> str:
        hist = self._vwap_minute_history
        if len(hist) < 2:
            return "flat"
        change = hist[-1] - hist[0]
        if change >= 0.5:
            return "rising"
        if change <= -0.5:
            return "falling"
        return "flat"

    def vwap_trend_detail(self) -> dict:
        hist = list(self._vwap_minute_history)
        return {
            "trend"      : self.vwap_trend(),
            "snapshots"  : len(hist),
            "oldest_vwap": hist[0]  if hist else 0.0,
            "newest_vwap": hist[-1] if hist else 0.0,
            "change"     : (hist[-1] - hist[0]) if len(hist) >= 2 else 0.0,
            "zone"       : self._zone_state,
        }

    @property
    def is_ready(self) -> bool:
        return self.tick_count >= config.VWAP_MIN_TICKS
