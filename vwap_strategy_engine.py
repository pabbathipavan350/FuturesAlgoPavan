# ============================================================
# VWAP_STRATEGY_ENGINE.PY — 4-Scenario VWAP Strategy Engine
# ============================================================
#
# TRACKS:
#   1) Nifty Futures VWAP + price   (primary signal source)
#   2) Nifty Index VWAP + price     (backup — avoids futures gap issues)
#   3) ITM Option VWAP + price      (entry confirmation / mid-trade)
#   4) VWAP trend for both Nifty Index and Futures
#
# SCENARIOS:
#   1) VWAP Trend POSITIVE (rising >20 pts over lookback):
#      → CE entries only
#      → Enter on pullbacks where price moved 40–60 pts ABOVE VWAP then returned to VWAP±5
#      → SL: 10 pts | Target: 40 pts with trailing
#
#   2) VWAP Trend NEGATIVE (falling >20 pts):
#      → PE entries only
#      → Mirror of scenario 1 for downside
#      → SL: 10 pts | Target: 40 pts with trailing
#
#   3) TREND CHANGE (morning trend → reversal detected):
#      → Switch signal direction after confirmed trend flip
#      → Same pullback entry rules as scenario 1/2 post-flip
#      → Trend flip requires VWAP direction change held for TREND_FLIP_CONFIRM_MINS
#
#   4) FLAT VWAP (unchanged ±FLAT_VWAP_MAX_CHANGE over FLAT_VWAP_LOOKBACK mins):
#      → Allow ONLY FIRST pullback after 40–50 pt move above/below VWAP
#      → Target: 25 pts only (not 40)
#      → No trailing — flat market, take quick profit
#
# PULLBACK VALIDITY RULES:
#   - VWAP must be trending (>20 change over lookback) — except flat scenario
#   - Price must have extended 40–60 pts away from VWAP before returning
#   - Price must return to VWAP ± 5 pts zone for entry
#   - Option ITM must also be near its own VWAP (±5 pts) at entry time
# ============================================================

import logging
import datetime
from collections import deque
import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Tunable constants (overrideable via config if added there)
# ─────────────────────────────────────────────────────────────

# Scenario 1/2/3 — trending
TREND_MIN_CHANGE_PTS    = 20.0   # VWAP must have moved ≥20 pts for trend to be "active"
PULLBACK_EXTEND_MIN     = 40.0   # price must have gone ≥40 pts beyond VWAP
PULLBACK_EXTEND_MAX     = 60.0   # price should not be >60 pts from VWAP (overextended)
ENTRY_ZONE_PTS          = 5.0    # price must be within VWAP ± 5 for entry
TREND_SL_PTS            = 10.0   # stoploss for trending scenarios
TREND_TARGET_PTS        = 40.0   # target for trending scenarios
TREND_TRAIL_TRIGGER     = 20.0   # at +20 pts profit, trail starts
TREND_TRAIL_STEP        = 10.0   # trail moves SL every 10 pts

# Scenario 4 — flat
FLAT_VWAP_MAX_CHANGE    = 5.0    # VWAP movement ≤5 pts over lookback = flat
FLAT_VWAP_LOOKBACK_MINS = 15     # how many minutes to look back for flatness
FLAT_PULLBACK_EXTEND    = 40.0   # price extended ≥40 pts from flat VWAP
FLAT_TARGET_PTS         = 25.0   # reduced target for flat scenarios
FLAT_SL_PTS             = 10.0   # same stoploss

# Trend flip (scenario 3)
TREND_FLIP_CONFIRM_MINS = 5      # VWAP must hold new direction for 5 mins before flip confirmed

# Nifty index VWAP construction
INDEX_VWAP_RESET_HOUR   = 9      # reset VWAP at session start
INDEX_VWAP_RESET_MIN    = 15

# Option entry confirmation
OPTION_ENTRY_ZONE_PTS   = 5.0    # option must be within ±5 of option VWAP


# ─────────────────────────────────────────────────────────────
# Index VWAP calculator (built from tick data — no broker field)
# ─────────────────────────────────────────────────────────────

class IndexVWAPCalculator:
    """
    Constructs VWAP for Nifty Index from raw ticks.
    Since the index itself is not traded, we approximate volume-weighted price
    using the tick price stream. When volume is not available (index ticks),
    we use a simple cumulative average (equal-weighted VWAP approximation).

    For a true VWAP with volume, pass vol > 0 in on_tick().
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self._cum_pv   = 0.0   # cumulative (price × volume)
        self._cum_vol  = 0.0   # cumulative volume
        self._tick_n   = 0     # tick count (used when vol not available)
        self._cum_sum  = 0.0   # cumulative price sum (equal-weight fallback)
        self.ltp       = 0.0
        self.vwap      = 0.0
        self._session_start = None

        # Per-minute snapshots for trend calculation
        self._minute_history: deque = deque(maxlen=FLAT_VWAP_LOOKBACK_MINS + 5)
        self._last_snap_min: int = -1

        # Peak distance tracking (for pullback extent measurement)
        self._session_high_dist = 0.0   # max distance above VWAP seen today
        self._session_low_dist  = 0.0   # max distance below VWAP seen today

    def on_tick(self, tick: dict):
        """
        Feed a Nifty index tick.
        tick keys expected: 'ltp'/'ltP', optionally 'vol'/'volume'/'tv'
        """
        ltp = float(tick.get("ltp") or tick.get("ltP") or tick.get("lp") or 0)
        if ltp <= 0:
            return

        # Reset at session start
        now = datetime.datetime.now()
        if self._session_start is None:
            self._session_start = now
            logger.debug(f"[IndexVWAP] Session start at {now.strftime('%H:%M:%S')}")

        vol = float(tick.get("vol") or tick.get("volume") or tick.get("tv") or 0)

        self.ltp    = ltp
        self._tick_n += 1

        if vol > 0:
            self._cum_pv  += ltp * vol
            self._cum_vol += vol
            self.vwap = self._cum_pv / self._cum_vol
        else:
            # Equal-weight when no volume
            self._cum_sum += ltp
            self.vwap = self._cum_sum / self._tick_n

        # Per-minute snapshot
        now_min = now.minute
        if now_min != self._last_snap_min:
            if self._last_snap_min != -1:
                self._minute_history.append(self.vwap)
                logger.debug(f"[IndexVWAP] Snap min={self._last_snap_min} VWAP={self.vwap:.2f}")
            self._last_snap_min = now_min

        # Track peak distances from VWAP
        dist = ltp - self.vwap
        if dist > self._session_high_dist:
            self._session_high_dist = dist
        if dist < self._session_low_dist:
            self._session_low_dist = dist

    @property
    def is_ready(self) -> bool:
        return self._tick_n >= 5 and self.vwap > 0

    @property
    def distance_from_vwap(self) -> float:
        """Positive = above VWAP, negative = below."""
        return self.ltp - self.vwap

    def trend(self, lookback_mins: int | None = None) -> str:
        """
        Returns 'rising', 'falling', or 'flat' based on VWAP minute snapshots.
        Uses TREND_MIN_CHANGE_PTS threshold.
        """
        lb   = lookback_mins or config.VWAP_TREND_LOOKBACK
        hist = self._minute_history
        if len(hist) < lb:
            return "flat"
        oldest = list(hist)[-lb]
        newest = hist[-1]
        change = newest - oldest
        if change >= TREND_MIN_CHANGE_PTS:
            return "rising"
        if change <= -TREND_MIN_CHANGE_PTS:
            return "falling"
        return "flat"

    def flat_vwap_check(self) -> bool:
        """
        Returns True if VWAP has been essentially flat for FLAT_VWAP_LOOKBACK_MINS.
        """
        hist = self._minute_history
        lb   = FLAT_VWAP_LOOKBACK_MINS
        if len(hist) < lb:
            return False
        window = list(hist)[-lb:]
        change = abs(window[-1] - window[0])
        return change <= FLAT_VWAP_MAX_CHANGE

    def trend_detail(self) -> dict:
        hist = self._minute_history
        lb   = config.VWAP_TREND_LOOKBACK
        if len(hist) < 2:
            return {"trend": "flat", "change": 0.0, "snapshots": len(hist),
                    "oldest": 0.0, "newest": 0.0}
        oldest = list(hist)[-lb] if len(hist) >= lb else hist[0]
        newest = hist[-1]
        return {
            "trend"    : self.trend(),
            "change"   : newest - oldest,
            "snapshots": len(hist),
            "oldest"   : oldest,
            "newest"   : newest,
        }

    def get_state(self) -> dict:
        return {
            "ltp"          : self.ltp,
            "vwap"         : self.vwap,
            "distance"     : self.distance_from_vwap,
            "ticks"        : self._tick_n,
            "trend"        : self.trend(),
            "flat"         : self.flat_vwap_check(),
            "session_high_dist": self._session_high_dist,
            "session_low_dist" : self._session_low_dist,
        }


# ─────────────────────────────────────────────────────────────
# Option VWAP tracker (enhanced — tracks peak distance)
# ─────────────────────────────────────────────────────────────

class OptionVWAPState:
    """
    Enhanced option VWAP tracker.
    Tracks price, VWAP, distance, and peak extension for entry confirmation.
    """

    def __init__(self, token: str):
        self.token   = token
        self.ltp     = 0.0
        self.vwap    = 0.0
        self.ticks   = 0
        self._peak_dist_above = 0.0  # max distance above option VWAP
        self._peak_dist_below = 0.0  # max distance below option VWAP

    def on_tick(self, tick: dict):
        ltp  = float(tick.get("ltp") or tick.get("ltP") or tick.get("lp") or 0)
        vwap = float(tick.get("ap") or 0)
        if ltp > 0:
            self.ltp    = ltp
            self.ticks += 1
        if vwap > 0:
            self.vwap = vwap
        if self.vwap > 0 and self.ltp > 0:
            dist = self.ltp - self.vwap
            if dist > self._peak_dist_above:
                self._peak_dist_above = dist
            if dist < self._peak_dist_below:
                self._peak_dist_below = dist

    @property
    def distance(self) -> float:
        return self.ltp - self.vwap

    @property
    def is_in_entry_zone(self) -> bool:
        """True if option is within ±OPTION_ENTRY_ZONE_PTS of its VWAP."""
        if self.vwap <= 0 or self.ltp <= 0:
            return True   # no data → don't block entry
        return abs(self.distance) <= OPTION_ENTRY_ZONE_PTS

    @property
    def had_sufficient_extension(self) -> bool:
        """
        Returns True if the option extended ≥40 pts away from its VWAP before
        pulling back (for CE: check above; for PE: check below).
        """
        return (self._peak_dist_above >= PULLBACK_EXTEND_MIN or
                abs(self._peak_dist_below) >= PULLBACK_EXTEND_MIN)

    @property
    def is_ready(self) -> bool:
        return self.vwap > 0 and self.ltp > 0

    def get_state(self) -> dict:
        return {
            "ltp"            : self.ltp,
            "vwap"           : self.vwap,
            "distance"       : self.distance,
            "in_entry_zone"  : self.is_in_entry_zone,
            "peak_above"     : self._peak_dist_above,
            "peak_below"     : self._peak_dist_below,
        }


# ─────────────────────────────────────────────────────────────
# 4-Scenario VWAP Strategy Engine
# ─────────────────────────────────────────────────────────────

class VWAPStrategyEngine:
    """
    Core logic engine for the 4-scenario VWAP pullback strategy.

    Sits on top of FuturesVWAPEngine and adds:
      - Nifty Index VWAP construction (backup)
      - Scenario classification (trending/flat/flip)
      - Pullback validity checks (40–60 pt extension, return to VWAP±5)
      - Option VWAP entry confirmation
      - Correct SL/Target assignment per scenario

    Usage:
        engine = VWAPStrategyEngine()
        # On every futures tick:
        engine.on_futures_tick(tick)
        # On every index tick:
        engine.on_index_tick(tick)
        # On every option tick:
        engine.on_option_tick(token, tick)
        # Check for valid entry:
        signal = engine.evaluate_entry(option_token)
    """

    def __init__(self, futures_engine):
        """
        futures_engine: existing FuturesVWAPEngine instance
        """
        self.futures_engine  = futures_engine
        self.index_vwap      = IndexVWAPCalculator()
        self.option_states   : dict[str, OptionVWAPState] = {}

        # Scenario tracking
        self._scenario            = "unknown"   # trending_ce | trending_pe | flat | flip
        self._morning_trend       = None        # initial trend of the day
        self._flip_pending        = False
        self._flip_pending_dir    = None
        self._flip_pending_since  = None

        # Per-direction pullback tracking for the 4 scenarios
        # Tracks: when price last extended 40–60 pts away from VWAP
        self._extended_ce = False   # True when price was 40–60 pts above VWAP
        self._extended_pe = False   # True when price was 40–60 pts below VWAP
        self._flat_first_pullback_taken = {"CE": False, "PE": False}

        # Futures state snapshot
        self._futures_ltp  = 0.0
        self._futures_vwap = 0.0

        logger.info("[VWAPStrategy] Engine initialized")

    # ── Tick feeds ──────────────────────────────────────────

    def on_futures_tick(self, tick: dict):
        """Forward to futures engine — also update local state snapshot."""
        self.futures_engine.on_tick(tick)
        state = self.futures_engine.get_state()
        self._futures_ltp  = state["ltp"]
        self._futures_vwap = state["vwap"]
        self._update_extension_flags()

    def on_index_tick(self, tick: dict):
        """Feed Nifty Index tick to the Index VWAP calculator."""
        self.index_vwap.on_tick(tick)

    def on_option_tick(self, token: str, tick: dict):
        """Feed option tick to the relevant option tracker."""
        if token not in self.option_states:
            self.option_states[token] = OptionVWAPState(token)
        self.option_states[token].on_tick(tick)
        self.futures_engine.on_option_tick(token, tick)

    def register_option(self, token: str):
        if token not in self.option_states:
            self.option_states[token] = OptionVWAPState(token)
            self.futures_engine.register_option_token(token)

    def unregister_option(self, token: str):
        self.option_states.pop(token, None)
        self.futures_engine.unregister_option_token(token)

    # ── Internal helpers ────────────────────────────────────

    def _update_extension_flags(self):
        """
        Track whether price has recently been 40–60 pts beyond VWAP.
        This validates pullback entries (price must have extended before returning).
        Uses futures VWAP as primary; falls back to index VWAP.
        """
        ltp  = self._futures_ltp
        vwap = self._futures_vwap

        if ltp <= 0 or vwap <= 0:
            return

        dist_above = ltp - vwap
        dist_below = vwap - ltp

        # CE extension: price went 40–60 pts above VWAP
        if PULLBACK_EXTEND_MIN <= dist_above <= PULLBACK_EXTEND_MAX:
            if not self._extended_ce:
                logger.info(f"[VWAPStrategy] CE extension set — dist_above={dist_above:.1f}pts")
            self._extended_ce = True
        elif dist_above > PULLBACK_EXTEND_MAX:
            # Overextended — reset, wait for a fresh pullback after it returns
            if self._extended_ce:
                logger.info(f"[VWAPStrategy] CE extension reset — price overextended {dist_above:.1f}pts")
            self._extended_ce = False
        elif dist_above < 0:
            # Price crossed below VWAP — reset CE extension
            self._extended_ce = False

        # PE extension: price went 40–60 pts below VWAP
        if PULLBACK_EXTEND_MIN <= dist_below <= PULLBACK_EXTEND_MAX:
            if not self._extended_pe:
                logger.info(f"[VWAPStrategy] PE extension set — dist_below={dist_below:.1f}pts")
            self._extended_pe = True
        elif dist_below > PULLBACK_EXTEND_MAX:
            if self._extended_pe:
                logger.info(f"[VWAPStrategy] PE extension reset — overextended {dist_below:.1f}pts")
            self._extended_pe = False
        elif dist_below < 0:
            self._extended_pe = False

    def _futures_vwap_trend(self) -> str:
        return self.futures_engine.vwap_trend()

    def _index_vwap_trend(self) -> str:
        return self.index_vwap.trend()

    def _combined_trend(self) -> str:
        """
        Returns consensus trend using both futures VWAP and index VWAP.
        If they agree → use that. If they disagree → use futures (primary).
        Flat only if BOTH are flat.
        """
        ft = self._futures_vwap_trend()
        it = self._index_vwap_trend()

        if ft == it:
            return ft
        if ft != "flat":
            return ft   # futures is primary signal
        return it       # futures is flat but index has direction

    def _classify_scenario(self) -> str:
        """
        Classifies current market into one of 4 scenarios:
          'trending_ce'  → Scenario 1: VWAP rising, look for CE pullbacks
          'trending_pe'  → Scenario 2: VWAP falling, look for PE pullbacks
          'flat'         → Scenario 4: VWAP flat for 15+ mins
          'flip_ce'      → Scenario 3: Trend changed → CE (was PE → now rising)
          'flip_pe'      → Scenario 3: Trend changed → PE (was CE → now falling)
        """
        trend = self._combined_trend()

        # Morning trend initialisation
        if self._morning_trend is None and trend != "flat":
            self._morning_trend = trend
            logger.info(f"[VWAPStrategy] Morning trend established: {trend}")

        # Flat check
        if self.index_vwap.flat_vwap_check():
            return "flat"

        # Trend flip detection (Scenario 3)
        if (self._morning_trend is not None and
                trend != "flat" and
                trend != self._morning_trend):
            # Trend has changed from morning — check if confirmed
            if not self._flip_pending:
                self._flip_pending      = True
                self._flip_pending_dir  = trend
                self._flip_pending_since = datetime.datetime.now()
                logger.info(f"[VWAPStrategy] Potential trend flip: {self._morning_trend}→{trend} — monitoring")

            # Check if held for TREND_FLIP_CONFIRM_MINS
            if self._flip_pending and self._flip_pending_dir == trend:
                elapsed = (datetime.datetime.now() - self._flip_pending_since).total_seconds() / 60
                if elapsed >= TREND_FLIP_CONFIRM_MINS:
                    # Flip confirmed
                    old = self._morning_trend
                    self._morning_trend = trend
                    self._flip_pending  = False
                    logger.info(f"[VWAPStrategy] Trend flip CONFIRMED: {old}→{trend} "
                                f"after {elapsed:.1f}min")
                    # Reset flat pullback flags after flip
                    self._flat_first_pullback_taken = {"CE": False, "PE": False}
        else:
            # Trend consistent — clear any pending flip
            self._flip_pending = False

        if trend == "rising":
            return "trending_ce"
        if trend == "falling":
            return "trending_pe"
        return "flat"

    # ── Entry evaluation ────────────────────────────────────

    def evaluate_entry(self, direction: str, option_token: str | None = None) -> dict | None:
        """
        Main entry gate. Checks all 4 scenario rules and returns entry info or None.

        Returns dict with:
          {
            'allowed': True/False,
            'scenario': 'trending_ce'/'trending_pe'/'flat'/'flip_ce'/'flip_pe',
            'direction': 'CE'/'PE',
            'sl_pts': float,
            'target_pts': float,
            'use_trailing': bool,
            'reason': str,            # why allowed or blocked
            'futures_ltp': float,
            'futures_vwap': float,
            'futures_dist': float,
            'index_ltp': float,
            'index_vwap': float,
            'option_in_zone': bool,
          }
        """
        ltp   = self._futures_ltp
        vwap  = self._futures_vwap
        if ltp <= 0 or vwap <= 0:
            return {"allowed": False, "reason": "No futures data"}

        scenario = self._classify_scenario()
        dist     = ltp - vwap          # positive = above VWAP

        # Which VWAP source to use for entry — prefer futures but fall back to index
        use_index_vwap = False
        if abs(vwap - self.index_vwap.vwap) > 20 and self.index_vwap.is_ready:
            # Large gap between futures and index VWAP — use index (futures has a gap)
            use_index_vwap = True
            effective_vwap = self.index_vwap.vwap
            effective_dist = self.index_vwap.ltp - effective_vwap
            logger.info(f"[VWAPStrategy] Using INDEX VWAP (gap={abs(vwap - self.index_vwap.vwap):.1f}pts)")
        else:
            effective_vwap = vwap
            effective_dist = dist

        base = {
            "scenario"       : scenario,
            "direction"      : direction,
            "futures_ltp"    : ltp,
            "futures_vwap"   : vwap,
            "futures_dist"   : dist,
            "index_ltp"      : self.index_vwap.ltp,
            "index_vwap"     : self.index_vwap.vwap,
            "index_dist"     : self.index_vwap.distance_from_vwap,
            "used_index_vwap": use_index_vwap,
            "effective_vwap" : effective_vwap,
            "effective_dist" : effective_dist,
        }

        # ── Scenario 4: Flat VWAP ───────────────────────────────────────────
        if scenario == "flat":
            return self._evaluate_flat_scenario(direction, effective_dist, option_token, base)

        # ── Scenario 1/2: Trending (or 3: post-flip trending) ──────────────
        if scenario in ("trending_ce", "trending_pe", "flip_ce", "flip_pe"):
            return self._evaluate_trending_scenario(direction, effective_dist, option_token, base, scenario)

        return {"allowed": False, "reason": f"Unknown scenario: {scenario}", **base}

    def _evaluate_trending_scenario(self, direction: str, eff_dist: float,
                                     option_token: str | None, base: dict,
                                     scenario: str) -> dict:
        """Scenarios 1, 2, 3 — trending VWAP pullback entries."""

        # Direction must match trend
        expected_dir = "CE" if "ce" in scenario else "PE"
        if direction != expected_dir:
            return {**base, "allowed": False,
                    "reason": f"Trend is {scenario} — only {expected_dir} entries allowed"}

        # Price must be in VWAP ± 5 zone (pullback has returned to VWAP)
        if abs(eff_dist) > ENTRY_ZONE_PTS:
            return {**base, "allowed": False,
                    "reason": f"Price not in entry zone: dist={eff_dist:+.1f}pts (need ±{ENTRY_ZONE_PTS})"}

        # Price must have had prior 40–60 pt extension (valid pullback)
        if direction == "CE" and not self._extended_ce:
            return {**base, "allowed": False,
                    "reason": "CE: No prior 40–60pt extension above VWAP — not a valid pullback"}
        if direction == "PE" and not self._extended_pe:
            return {**base, "allowed": False,
                    "reason": "PE: No prior 40–60pt extension below VWAP — not a valid pullback"}

        # Option confirmation (if token provided)
        opt_in_zone = True
        if option_token and option_token in self.option_states:
            opt_state = self.option_states[option_token]
            opt_in_zone = opt_state.is_in_entry_zone
            if not opt_in_zone:
                return {**base, "allowed": False,
                        "reason": f"Option not in entry zone: dist={opt_state.distance:+.1f}pts "
                                  f"(need ±{OPTION_ENTRY_ZONE_PTS})"}

        # All checks passed — reset extension flag (used, new pullback needed)
        if direction == "CE":
            self._extended_ce = False
        else:
            self._extended_pe = False

        logger.info(f"[VWAPStrategy] ✅ {scenario.upper()} ENTRY {direction} "
                    f"dist={eff_dist:+.1f}pts")

        return {
            **base,
            "allowed"      : True,
            "reason"       : f"{scenario} pullback entry",
            "sl_pts"       : TREND_SL_PTS,
            "target_pts"   : TREND_TARGET_PTS,
            "use_trailing" : True,
            "trail_trigger": TREND_TRAIL_TRIGGER,
            "trail_step"   : TREND_TRAIL_STEP,
            "option_in_zone": opt_in_zone,
        }

    def _evaluate_flat_scenario(self, direction: str, eff_dist: float,
                                 option_token: str | None, base: dict) -> dict:
        """Scenario 4 — flat VWAP, first pullback only, target 25 pts."""

        # Flat scenario — both CE and PE pullbacks allowed (whichever fires first)

        # Price must have extended ≥40 pts away from flat VWAP
        if direction == "CE":
            extended = self._extended_ce
        else:
            extended = self._extended_pe

        if not extended:
            return {**base, "allowed": False,
                    "reason": f"Flat VWAP: No ≥{FLAT_PULLBACK_EXTEND}pt extension seen — wait for move"}

        # Price must now be back near VWAP (pulled back)
        if abs(eff_dist) > ENTRY_ZONE_PTS:
            return {**base, "allowed": False,
                    "reason": f"Flat VWAP: Price not back in zone dist={eff_dist:+.1f}pts "
                               f"(need ±{ENTRY_ZONE_PTS})"}

        # Only FIRST pullback in flat scenario
        if self._flat_first_pullback_taken.get(direction, False):
            return {**base, "allowed": False,
                    "reason": f"Flat VWAP: First {direction} pullback already taken — no more entries in flat"}

        # Option confirmation (if token provided)
        opt_in_zone = True
        if option_token and option_token in self.option_states:
            opt_state = self.option_states[option_token]
            opt_in_zone = opt_state.is_in_entry_zone
            if not opt_in_zone:
                return {**base, "allowed": False,
                        "reason": f"Flat VWAP: Option not in zone dist={opt_state.distance:+.1f}pts"}

        # Mark first pullback taken
        self._flat_first_pullback_taken[direction] = True

        # Reset extension flag
        if direction == "CE":
            self._extended_ce = False
        else:
            self._extended_pe = False

        logger.info(f"[VWAPStrategy] ✅ FLAT VWAP ENTRY {direction} "
                    f"dist={eff_dist:+.1f}pts target={FLAT_TARGET_PTS}pts")

        return {
            **base,
            "allowed"      : True,
            "reason"       : "flat_vwap first pullback",
            "sl_pts"       : FLAT_SL_PTS,
            "target_pts"   : FLAT_TARGET_PTS,
            "use_trailing" : False,   # no trailing in flat — take quick profit
            "trail_trigger": 0.0,
            "trail_step"   : 0.0,
            "option_in_zone": opt_in_zone,
        }

    def reset_flat_pullbacks(self):
        """Reset flat scenario pullback flags (call at start of each session or after trend change)."""
        self._flat_first_pullback_taken = {"CE": False, "PE": False}
        logger.info("[VWAPStrategy] Flat pullback flags reset")

    # ── Status display ──────────────────────────────────────

    def get_full_status(self) -> dict:
        """Returns a complete status dict for logging/dashboard display."""
        scenario = self._classify_scenario()
        fut_det  = self.futures_engine.vwap_trend_detail()
        idx_det  = self.index_vwap.trend_detail()

        return {
            "scenario"        : scenario,
            "morning_trend"   : self._morning_trend,
            "flip_pending"    : self._flip_pending,
            "flip_pending_dir": self._flip_pending_dir,

            # Futures VWAP
            "futures_ltp"     : self._futures_ltp,
            "futures_vwap"    : self._futures_vwap,
            "futures_dist"    : self._futures_ltp - self._futures_vwap,
            "futures_trend"   : self._futures_vwap_trend(),
            "futures_trend_detail": fut_det,

            # Index VWAP
            "index_ltp"       : self.index_vwap.ltp,
            "index_vwap"      : self.index_vwap.vwap,
            "index_dist"      : self.index_vwap.distance_from_vwap,
            "index_trend"     : self._index_vwap_trend(),
            "index_trend_detail": idx_det,
            "index_flat"      : self.index_vwap.flat_vwap_check(),

            # Extension flags
            "ce_extended"     : self._extended_ce,
            "pe_extended"     : self._extended_pe,

            # Flat pullback taken
            "flat_ce_taken"   : self._flat_first_pullback_taken.get("CE", False),
            "flat_pe_taken"   : self._flat_first_pullback_taken.get("PE", False),
        }

    def print_status(self):
        """Print a clean status summary to console."""
        s = self.get_full_status()
        print(f"\n{'─'*60}")
        print(f"  VWAP Strategy Status — {datetime.datetime.now().strftime('%H:%M:%S')}")
        print(f"{'─'*60}")
        print(f"  Scenario        : {s['scenario'].upper()}")
        print(f"  Morning Trend   : {s['morning_trend'] or 'not set'}")
        print(f"  Flip Pending    : {s['flip_pending']} ({s['flip_pending_dir'] or '-'})")
        print(f"")
        print(f"  Futures LTP     : {s['futures_ltp']:.2f}")
        print(f"  Futures VWAP    : {s['futures_vwap']:.2f}  "
              f"dist={s['futures_dist']:+.1f}  trend={s['futures_trend']}")
        print(f"  Index LTP       : {s['index_ltp']:.2f}")
        print(f"  Index VWAP      : {s['index_vwap']:.2f}  "
              f"dist={s['index_dist']:+.1f}  trend={s['index_trend']}  "
              f"flat={s['index_flat']}")
        print(f"")
        print(f"  CE extended 40–60: {s['ce_extended']}")
        print(f"  PE extended 40–60: {s['pe_extended']}")
        print(f"  Flat CE taken   : {s['flat_ce_taken']}")
        print(f"  Flat PE taken   : {s['flat_pe_taken']}")
        print(f"{'─'*60}\n")
