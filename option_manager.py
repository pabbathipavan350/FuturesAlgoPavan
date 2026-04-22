# ============================================================
# OPTION_MANAGER.PY — Strike Pre-loader + Order Placement v3
# ============================================================
# KEY CHANGE vs v2:
#   All strike tokens are resolved and OI-checked at STARTUP,
#   not when a signal fires. When a signal fires, pick_strike()
#   reads from the pre-loaded cache — zero HTTP delay.
#
# Pre-load flow (called once after auth):
#   1. Download scrip master → cache in memory
#   2. Fetch current Nifty spot (from futures LTP or index)
#   3. Round ATM, compute candidate strikes at various ITM depths
#   4. Resolve token for each candidate (from scrip master)
#   5. Fetch OI for all resolved tokens via quotes API
#   6. Store: {direction: [{strike, token, delta, oi}]} sorted by delta
#
# pick_strike() at signal time:
#   1. Read current spot
#   2. Find best pre-cached entry that passes delta>=0.85 and OI>=12L
#   3. If nothing in cache passes, fall back to live scan
# ============================================================

import math
import logging
import time
import datetime
import config

logger = logging.getLogger(__name__)


# ── Scrip master cache ────────────────────────────────────
_scrip_cache: list = []

def _get_scrip_master(client) -> list:
    global _scrip_cache
    if _scrip_cache:
        return _scrip_cache
    try:
        import requests, csv, io
        url  = client.scrip_master(exchange_segment=config.FO_SEGMENT)
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            _scrip_cache = list(csv.DictReader(io.StringIO(resp.text)))
            logger.info(f"Scrip master cached: {len(_scrip_cache)} rows")
        else:
            logger.error(f"Scrip master HTTP {resp.status_code}")
    except Exception as e:
        logger.error(f"Scrip master download error: {e}")
    return _scrip_cache


# ── Black-Scholes delta ───────────────────────────────────

def _bs_delta(S, K, T, r, sigma, option_type):
    if T <= 0 or sigma <= 0:
        # Expiry-day boundary: use intrinsic value only
        if option_type == 'CE':
            return 1.0 if S > K else 0.0
        else:  # PE
            return 1.0 if S < K else 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        from statistics import NormalDist
        nd = NormalDist()
        return nd.cdf(d1) if option_type == 'CE' else nd.cdf(-d1)
    except Exception:
        return 0.0

def _days_to_expiry(expiry_date: datetime.date) -> float:
    days = (expiry_date - datetime.date.today()).days
    if days < 0:
        return 0.0
    if days == 0:
        # Expiry day: use remaining trading time so BS formula works correctly
        # This prevents T=0 which breaks delta calculation for PEs
        now = datetime.datetime.now()
        market_close = datetime.datetime(now.year, now.month, now.day, 15, 30)
        secs_left = max((market_close - now).total_seconds(), 900)  # min 15 min
        return secs_left / (365 * 24 * 3600)
    return days / 365.0

def round_to_strike(price: float, step: int = 50) -> int:
    return int(round(price / step) * step)


# ── Quote helper ─────────────────────────────────────────

def _unwrap_quotes_resp(resp) -> list:
    """
    Kotak Neo v2 quotes() wraps the list inside resp['data'] or
    resp['message']. Handle all known shapes.
    """
    if resp is None:
        return []
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        # Try every known wrapper key
        for key in ("data", "message", "result", "quotes", "success"):
            val = resp.get(key)
            if isinstance(val, list) and val:
                return val
            if isinstance(val, dict):
                return [val]
    return []


def _raw_quote(client, token: str, quote_type: str) -> dict:
    """
    Call client.quotes() for a single token and return the first
    record as a plain dict, or {} on failure.
    Tries both nse_fo and nse_cm segments if first fails.
    """
    for segment in (config.FO_SEGMENT, config.CM_SEGMENT):
        try:
            resp = client.quotes(
                instrument_tokens=[{"instrument_token": token,
                                     "exchange_segment": segment}],
                quote_type=quote_type,
            )
            data = _unwrap_quotes_resp(resp)
            if data:
                return data[0]
        except Exception as e:
            logger.debug(f"_raw_quote {quote_type} seg={segment} tok={token}: {e}")
    return {}


# ── OI fetch ─────────────────────────────────────────────
# Kotak Neo v2 returns OI only in the "depth" or full quote response.
# "ohlc" quote_type omits OI in many cases.
# We try multiple quote_types and all known OI field names.

_OI_FIELDS = ("oi", "open_interest", "openInterest", "OI",
              "tot_buy_qty", "totBuyQty")   # last two are fallbacks

# One-time debug flag — dumps full quote response for first token
_OI_DEBUG_DONE = False

def fetch_oi(client, token: str) -> int:
    global _OI_DEBUG_DONE

    # Try quote_types in priority order
    for qt in ("depth", "ohlc", "ltp", ""):
        try:
            resp = client.quotes(
                instrument_tokens=[{"instrument_token": token,
                                     "exchange_segment": config.FO_SEGMENT}],
                quote_type=qt,
            )
            data = _unwrap_quotes_resp(resp)

            # Debug dump: print full response for first token so we can see
            # exactly what fields Kotak returns — only once per session
            if not _OI_DEBUG_DONE and data:
                print(f"\n[OI_DEBUG] quote_type={qt!r} full response for token={token}:")
                print(f"  {data[0]}")
                _OI_DEBUG_DONE = True

            if data:
                rec = data[0]
                for field in _OI_FIELDS:
                    val = rec.get(field)
                    if val and int(float(val)) > 0:
                        return int(float(val))
        except Exception as e:
            logger.debug(f"fetch_oi qt={qt} token={token}: {e}")

    return 0


def fetch_ltp(client, token: str) -> float:
    """Fetch current LTP for a token via quotes API."""
    _LTP_FIELDS = ("ltp", "ltP", "last_price", "lastPrice", "close", "lc")

    for qt in ("ltp", "ohlc", "depth", ""):
        try:
            resp = client.quotes(
                instrument_tokens=[{"instrument_token": token,
                                     "exchange_segment": config.FO_SEGMENT}],
                quote_type=qt,
            )
            data = _unwrap_quotes_resp(resp)
            if data:
                rec = data[0]
                for field in _LTP_FIELDS:
                    val = rec.get(field)
                    if val and float(val) > 0:
                        return float(val)
        except Exception as e:
            logger.debug(f"fetch_ltp qt={qt} token={token}: {e}")

    return 0.0


def fetch_oi_and_ltp(client, token: str) -> tuple[int, float]:
    """
    Fetch OI and LTP for a single option token via quotes API.
    Kotak scrip master dOpenInterest is always 0 — must use live API.
    Tries quote_type='ohlc' first (usually has OI), then 'depth'.
    Returns (oi, ltp).
    """
    global _OI_DEBUG_DONE

    _LTP_FIELDS = ("ltp", "ltP", "last_price", "lastPrice", "close", "lc")
    _OI_FIELDS  = ("oi", "open_interest", "openInterest", "OI",
                   "openInt", "tot_buy_qty")

    oi  = 0
    ltp = 0.0

    for qt in ("ohlc", "depth", "ltp", ""):
        try:
            resp = client.quotes(
                instrument_tokens=[{"instrument_token": token,
                                     "exchange_segment": config.FO_SEGMENT}],
                quote_type=qt,
            )
            data = _unwrap_quotes_resp(resp)

            # One-time full dump
            if not _OI_DEBUG_DONE and data:
                print(f"\n[OI_DEBUG] quote_type={qt!r} full response (token={token}):")
                print(f"  {data[0]}")
                _OI_DEBUG_DONE = True

            if data:
                rec = data[0]
                if oi == 0:
                    for f in _OI_FIELDS:
                        v = rec.get(f)
                        if v and int(float(v)) > 0:
                            oi = int(float(v))
                            break
                if ltp == 0:
                    for f in _LTP_FIELDS:
                        v = rec.get(f)
                        if v and float(v) > 0:
                            ltp = float(v)
                            break
                if oi > 0 and ltp > 0:
                    break
        except Exception as e:
            logger.debug(f"fetch_oi_and_ltp qt={qt} token={token}: {e}")

    return oi, ltp


# ── OI from scrip master (fast, no HTTP) ─────────────────
# Kotak scrip master has OI in dOpenInt or similar field.
# We build a token→OI dict once from the cached rows.
_scrip_oi_cache: dict[str, int] = {}
_scrip_oi_built = False
_OI_SM_FIELD    = None   # discovered field name

def _get_oi_from_scrip_master(token: str) -> int:
    """Return OI for a token from scrip master cache. 0 if not found."""
    global _scrip_oi_built, _OI_SM_FIELD, _scrip_oi_cache

    if not _scrip_oi_built:
        _build_scrip_oi_cache()

    return _scrip_oi_cache.get(str(token), 0)

def _build_scrip_oi_cache():
    """
    Kotak scrip master dOpenInterest is always 0 for F&O.
    OI must be fetched live via quotes API in fetch_oi_and_ltp().
    This function is kept as a no-op so call sites don't break.
    """
    global _scrip_oi_built
    _scrip_oi_built = True
    print("[PreLoad] OI will be fetched live via quotes API (scrip master has no OI data)")



# ── Token resolution ─────────────────────────────────────


# ── One-time dump of Nifty option rows ───────────────────
_OPTION_FORMAT_DUMPED = False

def _dump_nifty_option_format(rows: list, expiry_str: str):
    """Print sample NIFTY option rows from scrip master for debugging."""
    global _OPTION_FORMAT_DUMPED
    if _OPTION_FORMAT_DUMPED:
        return
    _OPTION_FORMAT_DUMPED = True
    # expiry_str is now like "26APR"
    sample = [
        r for r in rows
        if (r.get("pTrdSymbol") or "").upper().startswith("NIFTY")
        and ("CE" in (r.get("pTrdSymbol") or "").upper()
             or "PE" in (r.get("pTrdSymbol") or "").upper())
        and expiry_str.upper() in (r.get("pTrdSymbol") or "").upper()
    ]
    print(f"\n[OptionFormat] NIFTY options with expiry '{expiry_str}' "
          f"({len(sample)} found in scrip master):")
    for r in sample[:10]:
        print(f"  {r.get('pTrdSymbol',''):35s}  token={r.get('pSymbol','')}")
    if not sample:
        # Show first 5 NIFTY option rows regardless so we see the format
        any_nifty = [
            r for r in rows
            if (r.get("pTrdSymbol") or "").upper().startswith("NIFTY")
            and "CE" in (r.get("pTrdSymbol") or "").upper()
        ][:5]
        print(f"  No rows matched. Sample NIFTY CE rows:")
        for r in any_nifty:
            print(f"    {r.get('pTrdSymbol','')}")
    print()


# Module-level cache for working prefix format (discovered on first match)
_working_prefix: str | None = None

def find_option_token(client, symbol_prefix: str,
                      expiry_date_or_str,
                      strike: int,
                      option_type: str) -> str | None:
    """
    Resolve Kotak token for a Nifty weekly option.

    Tries 5 prefix formats in order (A→E) since Kotak changes format
    between API versions. Locks in the working format after first match.

    expiry_date_or_str: pass datetime.date (preferred) or legacy str
    """
    global _working_prefix
    rows     = _get_scrip_master(client)
    strike_s = str(int(strike))
    suffix   = f"{strike_s}{option_type}"   # e.g. "22900CE"

    # Build prefix list from date if possible
    if isinstance(expiry_date_or_str, datetime.date):
        prefixes = _build_expiry_prefixes(expiry_date_or_str)
        _dump_nifty_option_format(rows, prefixes[0][5:])  # strip NIFTY
    else:
        # Legacy string path — build prefixes from string
        exp_up = str(expiry_date_or_str).upper()
        prefixes = [f"NIFTY{exp_up}"]
        _dump_nifty_option_format(rows, exp_up)

    # If we already know the working prefix, try it first
    if _working_prefix:
        prefixes = [_working_prefix] + [p for p in prefixes if p != _working_prefix]

    for prefix in prefixes:
        for row in rows:
            trd = (row.get("pTrdSymbol") or "").strip().upper()
            tok = (row.get("pSymbol") or "").strip()
            if not trd or not tok:
                continue
            if (trd.startswith(prefix) and
                    trd.endswith(suffix) and
                    "BANK" not in trd and
                    "FIN" not in trd):
                if not _working_prefix:
                    _working_prefix = prefix
                    print(f"[OptionToken] Format locked: {prefix} ✅")
                logger.debug(f"find_option_token: '{trd}' → {tok}")
                return tok

    logger.debug(f"find_option_token: no match for {suffix} in {prefixes}")
    return None


def find_futures_token(client, expiry_str: str) -> str | None:
    rows = _get_scrip_master(client)
    if not rows:
        return None

    # Futures are MONTHLY — convert weekly expiry date to monthly format
    # e.g. weekly Apr 14 2026 → futures "26APR"
    if isinstance(expiry_str, datetime.date):
        year2  = expiry_str.strftime("%y")
        month3 = expiry_str.strftime("%b").upper()
        expiry_str = f"{year2}{month3}"
    else:
        year2  = str(expiry_str)[:2]
        month3 = str(expiry_str)[2:]

    candidates = [
        f"NIFTY{expiry_str}FUT",        # NIFTY26APRFUT  ← confirmed working
        f"NIFTY{year2}{month3}FUT",     # same
        f"NIFTY{month3}{year2}FUT",     # NIFTYAPR26FUT
        "NIFTY-I",
        "NIFTYFUT",
    ]

    fut_rows = [r for r in rows
                if "NIFTY" in (r.get("pTrdSymbol") or "").upper()
                and "FUT" in (r.get("pTrdSymbol") or "").upper()]
    print(f"[FuturesToken] {len(fut_rows)} NIFTY FUT rows in scrip master:")
    for r in fut_rows[:15]:
        print(f"  {r.get('pTrdSymbol',''):30s}  token={r.get('pSymbol','')}")

    for candidate in candidates:
        for row in rows:
            trd = (row.get("pTrdSymbol") or "").strip().upper()
            if trd == candidate.upper():
                tok = (row.get("pSymbol") or "").strip()
                if tok:
                    print(f"[FuturesToken] ✅ '{candidate}' → {tok}")
                    return tok

    month_year = f"{month3}{year2}"
    for row in rows:
        trd = (row.get("pTrdSymbol") or "").strip().upper()
        if "NIFTY" in trd and "FUT" in trd and month_year in trd:
            tok = (row.get("pSymbol") or "").strip()
            if tok:
                print(f"[FuturesToken] ✅ Partial '{trd}' → {tok}")
                return tok

    print(f"[FuturesToken] ❌ Not found for {expiry_str}. Tried: {candidates}")
    return None


# ── NSE holidays ─────────────────────────────────────────

NSE_WEEKLY_EXPIRY_HOLIDAYS = {
    datetime.date(2026, 3, 31),
    datetime.date(2026, 4, 14),
    datetime.date(2026, 6,  2),
}

_PRINTED_EXPIRY_MSGS = set()

def _resolve_weekly_expiry_for_date(base_date, verbose=False):
    days_ahead = (1 - base_date.weekday()) % 7
    tuesday    = base_date + datetime.timedelta(days=days_ahead)
    expiry     = (tuesday - datetime.timedelta(days=1)
                  if tuesday in NSE_WEEKLY_EXPIRY_HOLIDAYS else tuesday)
    if expiry < base_date:
        return _resolve_weekly_expiry_for_date(
            tuesday + datetime.timedelta(days=1), verbose=verbose)
    if verbose and tuesday in NSE_WEEKLY_EXPIRY_HOLIDAYS:
        key = (tuesday, expiry)
        if key not in _PRINTED_EXPIRY_MSGS:
            print(f"  [Expiry] Tuesday {tuesday} holiday → Monday {expiry}")
            _PRINTED_EXPIRY_MSGS.add(key)
    return expiry

# ── NSE Weekly Expiry Holidays ────────────────────────────
NSE_WEEKLY_EXPIRY_HOLIDAYS = {
    datetime.date(2026, 3, 31),
    datetime.date(2026, 4, 14),
    datetime.date(2026, 6,  2),
}

_PRINTED_EXPIRY = set()

def _resolve_weekly_expiry(base_date, verbose=False):
    """Return next valid Nifty weekly expiry (Tuesday) on or after base_date."""
    days_ahead = (1 - base_date.weekday()) % 7
    tuesday    = base_date + datetime.timedelta(days=days_ahead)
    expiry     = (tuesday - datetime.timedelta(days=1)
                  if tuesday in NSE_WEEKLY_EXPIRY_HOLIDAYS else tuesday)
    if expiry < base_date:
        return _resolve_weekly_expiry(tuesday + datetime.timedelta(days=1), verbose)
    if verbose and tuesday in NSE_WEEKLY_EXPIRY_HOLIDAYS:
        key = (tuesday, expiry)
        if key not in _PRINTED_EXPIRY:
            print(f"  [Expiry] Tuesday {tuesday} holiday → Monday {expiry}")
            _PRINTED_EXPIRY.add(key)
    return expiry

def get_next_weekly_expiry(from_date=None) -> datetime.date:
    """
    Returns the next Nifty WEEKLY expiry (Tuesday).
    If called after market close on expiry day, moves to the following week.
    Nifty weekly options expire every Tuesday (or Monday if Tuesday is holiday).
    """
    today = from_date or datetime.date.today()
    if isinstance(today, datetime.datetime):
        today = today.date()

    # If today is expiry day and after 3:30 PM, move to next week
    ref_date = today
    market_close = datetime.time(15, 30)
    now_time = datetime.datetime.now().time()

    expiry = _resolve_weekly_expiry(ref_date)

    # If today is expiry day and market is closed, use next week
    while ref_date > expiry or (ref_date == expiry and now_time >= market_close):
        ref_date = expiry + datetime.timedelta(days=1)
        expiry   = _resolve_weekly_expiry(ref_date)

    return expiry

def get_current_month_expiry() -> datetime.date:
    """Alias kept for compatibility."""
    return get_next_weekly_expiry()

def _build_expiry_prefixes(expiry_date: datetime.date) -> list:
    """
    Return all NIFTY prefix formats Kotak may use for weekly options.
    Tries formats in order of likelihood:
      A: NIFTY{YY}{M}{DD}   e.g. NIFTY26414   (most common weekly)
      B: NIFTY{YY}{MM}{DD}  e.g. NIFTY260414  (zero-padded month)
      C: NIFTY{YY}{MON}     e.g. NIFTY26APR   (monthly format fallback)
      D: NIFTY{YY}{M}       e.g. NIFTY264
      E: NIFTY{YY}{MM}      e.g. NIFTY2604
    """
    yy  = expiry_date.strftime("%y")
    m   = str(expiry_date.month)
    mm  = expiry_date.strftime("%m")
    dd  = expiry_date.strftime("%d")
    mon = expiry_date.strftime("%b").upper()
    return [
        f"NIFTY{yy}{m}{dd}",   # A: NIFTY26414
        f"NIFTY{yy}{mm}{dd}",  # B: NIFTY260414
        f"NIFTY{yy}{mon}",     # C: NIFTY26APR  (monthly fallback)
        f"NIFTY{yy}{m}",       # D: NIFTY264
        f"NIFTY{yy}{mm}",      # E: NIFTY2604
    ]

def expiry_to_kotak_str(expiry_date: datetime.date) -> str:
    """
    Primary Kotak weekly options format: YYMDD (no zero-pad on month).
    e.g. Apr 14, 2026 → '26414'
    Used only for futures token lookup. Options use _build_expiry_prefixes().
    """
    return expiry_date.strftime("%y") + str(expiry_date.month) + expiry_date.strftime("%d")

def expiry_to_kotak_monthly_str(expiry_date: datetime.date) -> str:
    """Monthly format for futures: YYMON e.g. 26APR"""
    return expiry_date.strftime("%y%b").upper()

def expiry_to_full_str(expiry_date: datetime.date) -> str:
    """Full DDMMMYY format — kept for reference."""
    return expiry_date.strftime("%d%b%y").upper()



# ── Pre-loaded strike cache ───────────────────────────────
# Structure:
# {
#   'CE': [{'strike': 23000, 'token': 'XXXX', 'delta': 0.92, 'oi': 1500000}, ...],
#   'PE': [{'strike': 24000, 'token': 'YYYY', 'delta': 0.90, 'oi': 1300000}, ...],
# }
# Sorted best-first (highest delta first for CE, same for PE).

class OptionManager:

    def __init__(self, client):
        self.client       = client
        self.expiry_date  = get_next_weekly_expiry()
        self.expiry_str   = expiry_to_kotak_str(self.expiry_date)
        self.dte          = _days_to_expiry(self.expiry_date)

        # Pre-loaded strike cache — populated by preload_strikes()
        self._strike_cache: dict[str, list] = {"CE": [], "PE": []}

        print(f"[OptionManager] Expiry: {self.expiry_date} "
              f"(Kotak symbol prefix: NIFTY{self.expiry_str}) "
              f"DTE={self.dte*365:.0f}d")

    # ── STARTUP PRE-LOAD ──────────────────────────────────

    def preload_strikes(self, spot: float):
        """
        Called ONCE at startup. Caches every strike from ATM±PRELOAD_ITM_MIN
        to ATM±PRELOAD_ITM_MAX in PRELOAD_STEP increments for both CE and PE.

        This covers market moves up to PRELOAD_ITM_MAX pts from open ATM —
        so even if Nifty runs 400 pts, we still have ITM options pre-cached.

        OI is fetched in a SINGLE BATCH CALL per direction (not per-token),
        which is much faster and avoids rate-limiting.
        """
        atm   = round_to_strike(spot, config.STRIKE_STEP)
        sigma = config.IV_PCT / 100.0
        r     = config.RISK_FREE_RATE
        T     = self.dte

        depths = list(range(config.PRELOAD_ITM_MIN,
                            config.PRELOAD_ITM_MAX + config.PRELOAD_STEP,
                            config.PRELOAD_STEP))

        print(f"\n[PreLoad] Starting pre-load: spot={spot:.0f} ATM={atm} "
              f"expiry={self.expiry_str}")
        print(f"[PreLoad] Caching {len(depths)} ITM depths per direction "
              f"({depths[0]}–{depths[-1]}pts) = up to {len(depths)*2} strikes total")

        for direction in ["CE", "PE"]:
            # Build continuous strike list from close ITM to deep ITM
            if direction == "CE":
                # CE: strikes below ATM (lower strike = more ITM for calls)
                strikes = sorted([atm - d for d in depths])
            else:
                # PE: strikes above ATM (higher strike = more ITM for puts)
                strikes = sorted([atm + d for d in depths], reverse=True)

            # ── Step 1: Resolve tokens from cached scrip master ──────────
            token_map: dict[int, str] = {}   # strike → token
            sym_map:   dict[int, str] = {}   # strike → symbol (for display)
            for strike in strikes:
                tok = find_option_token(
                    self.client, "NIFTY", self.expiry_date, strike, direction)
                if tok:
                    token_map[strike] = tok
                    sym_map[strike]   = next(
                        (row.get("pTrdSymbol", "") for row in _scrip_cache
                         if row.get("pSymbol", "") == tok), "?")
                else:
                    logger.debug(f"[PreLoad] {direction} {strike}: no token in scrip master")

            if not token_map:
                print(f"[PreLoad] {direction}: no tokens resolved — check expiry format")
                continue

            print(f"[PreLoad] {direction}: {len(token_map)}/{len(strikes)} tokens resolved "
                  f"— fetching OI+LTP in batch...")

            # ── Step 2: Batch OI + LTP fetch ────────────────────────────
            # Kotak Neo v2 ohlc response (confirmed from debug output):
            #   - Token id field : 'exchange_token'  (NOT 'tk')
            #   - LTP            : nested inside ohlc['close']  (NOT flat)
            #   - OI             : NOT present in ohlc at all
            #
            # Strategy:
            #   Pass 1 — quote_type="ohlc"  → get LTP from ohlc['close']
            #   Pass 2 — quote_type=""       → get OI from full quote response
            #
            # Build exchange_token → pSymbol reverse map so we can match records.
            batch_oi:  dict[str, int]   = {}
            batch_ltp: dict[str, float] = {}

            all_tokens = list(token_map.values())

            # exchange_token (REST id) → pSymbol (WS subscribe id)
            extok_to_psym: dict[str, str] = {}
            for row in _scrip_cache:
                psym  = str(row.get("pSymbol", "")).strip()
                extok = str(row.get("pExchSym", "") or
                            row.get("exchange_token", "") or
                            row.get("pToken", "")).strip()
                if psym in all_tokens and extok:
                    extok_to_psym[extok] = psym
            # Also map display_symbol → pSymbol as fallback
            dsym_to_psym: dict[str, str] = {
                str(row.get("pTrdSymbol", "")).upper(): str(row.get("pSymbol", ""))
                for row in _scrip_cache
                if str(row.get("pSymbol", "")) in all_tokens
            }

            def _resolve_psym(rec: dict) -> str:
                """Find the pSymbol for a quote record using all known id fields."""
                # Direct token fields
                for fld in ("tk", "token", "instrument_token"):
                    v = str(rec.get(fld, "")).strip()
                    if v in all_tokens:
                        return v
                # exchange_token reverse lookup
                extok = str(rec.get("exchange_token", "")).strip()
                if extok in extok_to_psym:
                    return extok_to_psym[extok]
                # display_symbol reverse lookup
                dsym = str(rec.get("display_symbol", "")).upper().strip()
                if dsym in dsym_to_psym:
                    return dsym_to_psym[dsym]
                return ""

            def _extract_ltp(rec: dict) -> float:
                """Extract LTP — handles both flat and nested ohlc dict."""
                # Flat fields first
                for fld in ("ltp", "ltP", "last_price", "lastPrice", "lc"):
                    v = rec.get(fld)
                    if v is not None:
                        try:
                            fv = float(v)
                            if fv > 0:
                                return fv
                        except (ValueError, TypeError):
                            pass
                # Kotak ohlc response: prices nested in ohlc sub-dict
                ohlc = rec.get("ohlc")
                if isinstance(ohlc, dict):
                    for fld in ("close", "ltp", "high", "open"):
                        v = ohlc.get(fld)
                        if v is not None:
                            try:
                                fv = float(v)
                                if fv > 0:
                                    return fv
                            except (ValueError, TypeError):
                                pass
                return 0.0

            def _extract_oi(rec: dict) -> int:
                """Extract OI — check all known field names."""
                for fld in ("oi", "open_interest", "openInterest", "OI",
                            "openInt", "dOpenInt", "openint", "open_int",
                            "tot_buy_qty", "totBuyQty"):
                    v = rec.get(fld)
                    if v is not None:
                        try:
                            iv = int(float(v))
                            if iv > 0:
                                return iv
                        except (ValueError, TypeError):
                            pass
                return 0

            # Pass 1: ohlc → LTP
            try:
                resp = self.client.quotes(
                    instrument_tokens=[{"instrument_token": t,
                                        "exchange_segment": config.FO_SEGMENT}
                                       for t in all_tokens],
                    quote_type="ohlc",
                )
                data = _unwrap_quotes_resp(resp)
                if data:
                    print(f"\n[OI_DEBUG] ohlc fields   : {list(data[0].keys())}")
                    print(f"[OI_DEBUG] ohlc sample   : {dict(list(data[0].items())[:20])}")
                for rec in (data or []):
                    psym = _resolve_psym(rec)
                    if psym:
                        lv = _extract_ltp(rec)
                        if lv > 0:
                            batch_ltp[psym] = lv
            except Exception as e:
                logger.debug(f"[PreLoad] ohlc batch: {e}")

            # Pass 2: full quote (quote_type="") → OI + LTP fallback
            try:
                resp = self.client.quotes(
                    instrument_tokens=[{"instrument_token": t,
                                        "exchange_segment": config.FO_SEGMENT}
                                       for t in all_tokens],
                    quote_type="",
                )
                data = _unwrap_quotes_resp(resp)
                if data:
                    print(f"[OI_DEBUG] full qt fields: {list(data[0].keys())}")
                    print(f"[OI_DEBUG] full qt sample: {dict(list(data[0].items())[:20])}")
                for rec in (data or []):
                    psym = _resolve_psym(rec)
                    if not psym:
                        continue
                    ov = _extract_oi(rec)
                    if ov > 0:
                        batch_oi[psym] = ov
                    if psym not in batch_ltp or batch_ltp[psym] == 0:
                        lv = _extract_ltp(rec)
                        if lv > 0:
                            batch_ltp[psym] = lv
            except Exception as e:
                logger.debug(f"[PreLoad] full quote batch: {e}")

            ltp_found = sum(1 for v in batch_ltp.values() if v > 0)
            oi_found  = sum(1 for v in batch_oi.values()  if v > 0)
            print(f"[PreLoad] Batch result: LTP={ltp_found}/{len(all_tokens)} "
                  f"OI={oi_found}/{len(all_tokens)}")

            # ── Step 3: Build cache entries ──────────────────────────────
            entries = []
            for strike in strikes:
                tok   = token_map.get(strike)
                if not tok:
                    continue

                delta = _bs_delta(spot, strike, T, r, sigma, direction)
                oi    = batch_oi.get(tok, 0)
                ltp   = batch_ltp.get(tok, 0.0)
                sym   = sym_map.get(strike, "?")

                entry = {
                    "strike"     : strike,
                    "token"      : tok,
                    "delta"      : round(delta, 4),
                    "oi"         : oi,
                    "ltp"        : ltp,
                    "expiry_str" : self.expiry_str,
                    "expiry_date": self.expiry_date,
                }
                entries.append(entry)

                oi_ok = "✅" if oi >= config.MIN_OI else ("⚠️ " if oi > 0 else "❌ 0")
                dl_ok = "✅" if delta >= config.MIN_DELTA else ("⚠️ " if delta >= 0.5 else "  ")
                print(f"[PreLoad]   {direction} {strike:>6}: "
                      f"delta={delta:.2f}{dl_ok}  OI={oi:>12,}{oi_ok}  "
                      f"LTP={ltp:>7.1f}  {sym}")

            # Sort: best delta first, then OI as tiebreak
            entries.sort(key=lambda x: (x["delta"], x["oi"]), reverse=True)
            self._strike_cache[direction] = entries

            good = sum(1 for e in entries
                       if e["delta"] >= config.MIN_DELTA and e["oi"] >= config.MIN_OI)
            print(f"[PreLoad] {direction}: {len(entries)} strikes cached "
                  f"({good} pass delta+OI filter) ✅")

        ce_n = len(self._strike_cache["CE"])
        pe_n = len(self._strike_cache["PE"])
        print(f"\n[PreLoad] ✅ Done. CE={ce_n} strikes, PE={pe_n} strikes cached.\n")

    def refresh_oi(self):
        """
        Refresh OI values in the cache (called every ~30 min if needed).
        Token resolution is NOT re-done — only OI values updated.
        """
        for direction in ["CE", "PE"]:
            for entry in self._strike_cache[direction]:
                entry["oi"] = fetch_oi(self.client, entry["token"])
        print("[OptionManager] OI refreshed in cache")

    # ── SIGNAL-TIME PICK (instant — reads from cache) ─────

    def pick_strike(self, spot: float, direction: str) -> dict | None:
        """
        Pick best pre-cached ITM strike for direction at signal time.
        Re-computes delta at current spot (market may have moved from open ATM).

        OI strategy:
          - If preload captured OI > 0: filter delta >= MIN_DELTA AND OI >= MIN_OI
          - If preload OI = 0 (API limitation): filter on delta only, log warning
            and do a live OI fetch for the chosen strike only (1 API call)
          - If nothing in cache passes delta: live scan fallback

        Strike ITM check uses delta alone (not ATM position comparison)
        so it still works correctly after big market moves.
        """
        sigma = config.IV_PCT / 100.0
        r     = config.RISK_FREE_RATE
        T     = self.dte

        candidates = self._strike_cache.get(direction, [])

        if not candidates:
            print(f"[Strike] Cache empty for {direction} — live scan")
            return self._live_scan(spot, direction)

        # Re-compute delta at current spot for every cached entry
        for c in candidates:
            c["delta"] = round(_bs_delta(spot, c["strike"], T, r, sigma, direction), 4)

        # Sort by delta descending (highest delta = most ITM = most reliable)
        candidates.sort(key=lambda x: x["delta"], reverse=True)

        # ── ITM depth cap: never go deeper than MAX_ITM_DEPTH_PTS from spot ──
        if direction == "CE":
            candidates = [c for c in candidates
                          if (spot - c["strike"]) <= config.MAX_ITM_DEPTH_PTS]
        else:
            candidates = [c for c in candidates
                          if (c["strike"] - spot) <= config.MAX_ITM_DEPTH_PTS]

        if not candidates:
            print(f"[Strike] All cached strikes deeper than {config.MAX_ITM_DEPTH_PTS}pts "
                  f"from spot {spot:.0f} — live scan")
            return self._live_scan(spot, direction)

        # ── Budget filter: drop strikes whose cached LTP exceeds per-lot limit ──
        # max_ltp = MAX_OPTION_COST_PER_LOT_RS / LOT_SIZE
        max_ltp = config.MAX_OPTION_COST_PER_LOT_RS / config.LOT_SIZE
        before  = len(candidates)
        candidates = [c for c in candidates
                      if c.get("ltp", 0) <= 0          # ltp unknown — keep, check later
                      or c["ltp"] <= max_ltp]
        dropped = before - len(candidates)
        if dropped:
            print(f"[Strike] Budget filter (≤Rs{config.MAX_OPTION_COST_PER_LOT_RS:,}/lot "
                  f"= LTP≤{max_ltp:.0f}pts): dropped {dropped} expensive strike(s)")
        if not candidates:
            print(f"[Strike] No {direction} strikes within budget — live scan")
            return self._live_scan(spot, direction)

        if any_oi:
            # ── Full filter: delta + OI ──────────────────────
            valid = [c for c in candidates
                     if c["delta"] >= config.MIN_DELTA and c["oi"] >= config.MIN_OI]
            if valid:
                best = valid[0]
                print(f"[Strike] ✅ {direction} {best['strike']} "
                      f"delta={best['delta']:.2f} OI={best['oi']:,}")
                return best

            # OI filter too strict — try delta only
            delta_ok = [c for c in candidates if c["delta"] >= config.MIN_DELTA]
            if delta_ok:
                best = delta_ok[0]
                print(f"[Strike] ⚠️  {direction} {best['strike']} OI below threshold "
                      f"delta={best['delta']:.2f} OI={best['oi']:,} — using anyway")
                return best

        else:
            # ── OI came back 0 from preload (API limitation) ─
            # Filter on delta only, then do ONE live OI fetch for top candidate
            delta_ok = [c for c in candidates if c["delta"] >= config.MIN_DELTA]
            if delta_ok:
                # Fetch OI live for the top few candidates to find one with good OI
                print(f"[Strike] OI=0 in cache — fetching live OI for top candidates...")
                for candidate in delta_ok[:5]:  # check top 5 delta-wise
                    tok = candidate["token"]
                    live_oi, live_ltp = fetch_oi_and_ltp(self.client, tok)
                    candidate["oi"]  = live_oi
                    if live_ltp > 0:
                        candidate["ltp"] = live_ltp
                    oi_tag = f"OI={live_oi:,}" if live_oi > 0 else "OI=? (API no data)"
                    print(f"[Strike]   {direction} {candidate['strike']}: "
                          f"delta={candidate['delta']:.2f}  {oi_tag}  LTP={live_ltp:.1f}")
                    if live_oi >= config.MIN_OI:
                        print(f"[Strike] ✅ {direction} {candidate['strike']} "
                              f"delta={candidate['delta']:.2f} OI={live_oi:,}")
                        return candidate

                # Nothing passed OI — use highest delta anyway (OI unknown/low)
                best = delta_ok[0]
                print(f"[Strike] ⚠️  {direction} {best['strike']} using best delta "
                      f"(OI API returned 0 — check OI_DEBUG log above) "
                      f"delta={best['delta']:.2f}")
                return best

        # ── Guaranteed fallback: best available ITM candidate ─────────────
        # Never return None from cache — always trade something sensible.
        # Pick highest delta >= 0.50 (at least somewhat ITM).
        all_itm = [c for c in candidates if c["delta"] >= 0.50]
        if all_itm:
            best = all_itm[0]
            print(f"[Strike] ⚠️  Fallback {direction} {best['strike']} "
                  f"delta={best['delta']:.2f} OI={best['oi']:,} — best available ITM")
            return best

        # Absolutely nothing in cache — live scan as last resort
        print(f"[Strike] No cached strike passes delta for {direction} "
              f"(market moved?) — live scan")
        return self._live_scan(spot, direction)

    def _live_scan(self, spot: float, direction: str) -> dict | None:
        """Fallback live scan — same logic as v2 pick_strike."""
        atm   = round_to_strike(spot, config.STRIKE_STEP)
        sigma = config.IV_PCT / 100.0
        r     = config.RISK_FREE_RATE
        T     = self.dte

        if direction == "CE":
            candidates = range(atm - 300, atm + config.STRIKE_STEP, config.STRIKE_STEP)
        else:
            candidates = range(atm + 300, atm - config.STRIKE_STEP, -config.STRIKE_STEP)

        best = None
        steps = 0
        for strike in candidates:
            delta = _bs_delta(spot, strike, T, r, sigma, direction)
            if delta < 0.50:
                continue
            token = find_option_token(self.client, "NIFTY", self.expiry_date,
                                      strike, direction)
            if not token:
                continue
            oi = fetch_oi(self.client, token)
            if oi >= config.MIN_OI and delta >= config.MIN_DELTA:
                return {"strike": strike, "token": token, "delta": delta,
                        "oi": oi, "expiry_str": self.expiry_str,
                        "expiry_date": self.expiry_date}
            if best is None or delta > best["delta"]:
                best = {"strike": strike, "token": token, "delta": delta,
                        "oi": oi, "expiry_str": self.expiry_str,
                        "expiry_date": self.expiry_date}
            steps += 1
            if steps > config.MAX_OI_WALK_STEPS:
                break
        return best

    # ── Order placement ───────────────────────────────────

    def place_buy_order(self, token: str, strike: int, direction: str,
                        ltp: float) -> dict | None:
        qty      = config.LOTS * config.LOT_SIZE
        limit_px = round(ltp + config.BUY_LIMIT_BUFFER, 2)
        print(f"\n[Order] BUY {direction} {strike} | qty={qty} "
              f"ltp={ltp:.2f} limit={limit_px:.2f}")

        if config.PAPER_TRADE:
            print(f"[Order] PAPER FILL @ {limit_px:.2f}")
            return {"fill_price": limit_px, "qty": qty, "order_id": "PAPER"}

        try:
            resp = self.client.place_order(
                exchange_segment="nse_fo", product="NRML",
                price=str(limit_px), order_type="L",
                quantity=str(qty), validity="DAY",
                trading_symbol=self._build_symbol(strike, direction),
                transaction_type="B", amo="NO",
                disclosed_quantity="0", market_protection="0",
                pf="N", trigger_price="0", tag=None,
            )
            order_id = self._extract_order_id(resp)
            if not order_id:
                logger.error(f"Buy order failed: {resp}")
                return None
            return self._wait_for_fill(order_id, qty, config.ORDER_FILL_TIMEOUT_SECS)
        except Exception as e:
            logger.error(f"place_buy_order: {e}")
            return None

    def place_exit_order(self, token: str, strike: int, direction: str,
                         qty: int, reason: str = "") -> float | None:
        print(f"\n[Order] SELL {direction} {strike} qty={qty} reason={reason}")

        if config.PAPER_TRADE:
            return None

        for attempt in range(config.EXIT_RETRY_ATTEMPTS):
            try:
                resp = self.client.place_order(
                    exchange_segment="nse_fo", product="NRML",
                    price="0", order_type="MKT",
                    quantity=str(qty), validity="DAY",
                    trading_symbol=self._build_symbol(strike, direction),
                    transaction_type="S", amo="NO",
                    disclosed_quantity="0", market_protection="0",
                    pf="N", trigger_price="0", tag=None,
                )
                order_id = self._extract_order_id(resp)
                if not order_id:
                    time.sleep(2)
                    continue
                fill = self._wait_for_fill(order_id, qty, config.EXIT_FILL_TIMEOUT_SECS)
                if fill:
                    return fill.get("fill_price")
            except Exception as e:
                logger.error(f"place_exit_order attempt {attempt+1}: {e}")
                time.sleep(2)
        return None

    def _build_symbol(self, strike: int, direction: str) -> str:
        # Use the locked working prefix if found, else default weekly format
        global _working_prefix
        if _working_prefix:
            return f"{_working_prefix}{strike}{direction}"
        # Fallback: YYMDD format (most common Kotak weekly)
        pfx = self.expiry_date.strftime("%y") + str(self.expiry_date.month) + self.expiry_date.strftime("%d")
        return f"NIFTY{pfx}{strike}{direction}"

    def _extract_order_id(self, resp) -> str | None:
        if not resp:
            return None
        if isinstance(resp, dict):
            return (resp.get("nOrdNo") or resp.get("order_id") or
                    resp.get("orderId") or resp.get("id"))
        return None

    def _wait_for_fill(self, order_id: str, qty: int, timeout: float) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = self.client.order_report()
                if isinstance(resp, list):
                    for o in resp:
                        if str(o.get("nOrdNo") or "") == str(order_id):
                            status   = (o.get("ordSt") or "").upper()
                            fill_qty = int(o.get("fldQty") or 0)
                            fill_px  = float(o.get("avgPrc") or 0)
                            if status in ("COMPLETE", "FILLED") and fill_qty > 0:
                                return {"fill_price": fill_px, "qty": fill_qty,
                                        "order_id": order_id}
                            if status in ("REJECTED", "CANCELLED"):
                                return None
            except Exception as e:
                logger.debug(f"Poll: {e}")
            time.sleep(config.ORDER_STATUS_POLL_SECS)
        try:
            self.client.cancel_order(order_id=order_id)
        except Exception:
            pass
        return None

    @staticmethod
    def calc_trade_cost(entry_price: float, exit_price: float, qty: int) -> float:
        buy_tv  = entry_price * qty
        sell_tv = exit_price  * qty
        total   = buy_tv + sell_tv
        brok    = config.BROKERAGE_PER_ORDER * 2
        stt     = total * config.STT_PCT
        exc     = total * config.EXCHANGE_TXN_PCT
        sebi    = total * config.SEBI_PCT
        gst     = (brok + exc + sebi) * config.GST_PCT
        stamp   = buy_tv * config.STAMP_DUTY_PCT
        return round(brok + stt + exc + sebi + gst + stamp, 2)
