"""
GMGN Trending Token Alert Bot
==============================
Setup:  pip install curl_cffi
Run:    python gmgn_alert_bot.py
"""

import os
import time
import logging
import random
import threading
from collections import deque

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    raise SystemExit("\n[ERROR] Run: pip install curl_cffi\n")

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

NTFY_TOPIC    = "gmgnalerts-hus93J"
NTFY_URL      = f"https://ntfy.sh/{NTFY_TOPIC}"

CHAIN         = "sol"
POLL_INTERVAL = 60
TIME_PERIOD   = "1m"
ORDER_BY      = "swaps"

VOL_SMA_PERIODS = 5       # 5 x 1-minute candles
VOL_SMA_MIN     = 20_000   # $20K average to pass

# Volatility ratio = vol_sma / liquidity — must be >= 1.5x to pass
MIN_VOL_RATIO = 1.5

# Rank scale 0.0–5.0: 1.5x -> 1.0, 10.0x -> 5.0 (linear, capped)
RANK_LOW_RATIO  = 1.5
RANK_HIGH_RATIO = 10.0
RANK_LOW        = 1.0
RANK_HIGH       = 5.0

# Minimum token age in minutes before an alert is sent
MIN_TOKEN_AGE_MINUTES = 15

# 6 hours — same token won't alert again within this window
ALERT_COOLDOWN_MINUTES = 360

MAX_ALERTS_PER_CYCLE = 5

# ─────────────────────────────────────────────
#  FILTERS  (None = disabled)
# ─────────────────────────────────────────────

FILTERS = {
    "require_no_mint":           True,
    "require_not_migrated":      False,
    "require_migrated":          False,
    "require_burnt":             False,
    "require_dev_still_holding": False,
    "require_dev_sell_all":      False,
    "require_dev_burnt":         False,
    "min_age_days":              None,
    "max_age_days":              2,
    "min_liquidity":             None,
    "max_liquidity":             None,
    "min_market_cap":            None,
    "max_market_cap":            None,
    "min_ath_market_cap":        None,
    "max_ath_market_cap":        None,
    "min_1m_txs":                None,
    "max_1m_txs":                None,
    "min_kol":                   2,
    "max_kol":                   None,
    "min_smart":                 2,
    "max_smart":                 None,
    "min_holders":               None,
    "max_holders":               None,
    "min_top10_pct":             None,
    "max_top10_pct":             None,
    "min_dev_holding_pct":       None,
    "max_dev_holding_pct":       None,
    "api_filters":               ["not_honeypot"],
}

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gmgn_bot")

# ─────────────────────────────────────────────
#  VOLUME HISTORY
# ─────────────────────────────────────────────

vol_history: dict[str, deque] = {}

def record_volume(address: str, volume: float):
    """Append a reading. Returns (sma, count); sma is None until buffer is full."""
    if address not in vol_history:
        vol_history[address] = deque(maxlen=VOL_SMA_PERIODS)
    vol_history[address].append(volume)
    buf = vol_history[address]
    if len(buf) < VOL_SMA_PERIODS:
        return None, len(buf)
    return sum(buf) / len(buf), len(buf)

def seed_vol_history(address: str, volume: float):
    """Pre-fill the buffer with a single value repeated — used by the debug command."""
    vol_history[address] = deque([volume] * VOL_SMA_PERIODS, maxlen=VOL_SMA_PERIODS)

# ─────────────────────────────────────────────
#  RANK
# ─────────────────────────────────────────────

def compute_rank(ratio: float) -> float:
    """Map vol/liq ratio to a 0.0–5.0 rank (1 decimal place)."""
    if ratio < RANK_LOW_RATIO:
        return 0.0
    rank = RANK_LOW + (ratio - RANK_LOW_RATIO) / (RANK_HIGH_RATIO - RANK_LOW_RATIO) * (RANK_HIGH - RANK_LOW)
    return round(min(rank, RANK_HIGH), 1)


def token_age_seconds(open_ts) -> float | None:
    """Return token age in seconds for a valid Unix timestamp, else None."""
    if open_ts is None:
        return None
    try:
        open_ts = float(open_ts)
    except (TypeError, ValueError):
        return None
    if open_ts <= 0:
        return None
    now = time.time()
    if open_ts > now * 1000:
        return None
    if open_ts > now * 10:
        open_ts /= 1000
    return now - open_ts

# ─────────────────────────────────────────────
#  SCRAPER
# ─────────────────────────────────────────────

def build_scraper():
    s = curl_requests.Session(impersonate="chrome120")
    s.headers.update({
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control":   "no-cache",
        "Pragma":          "no-cache",
        "Referer":         "https://gmgn.ai/sol/token",
        "Origin":          "https://gmgn.ai",
    })
    return s

SCRAPER = build_scraper()


def warmup_scraper(scraper):
    """Prime Cloudflare cookies before hitting the GMGN API."""
    for url in ("https://gmgn.ai/", "https://gmgn.ai/sol/token"):
        try:
            scraper.get(url, timeout=20)
        except Exception:
            continue


def is_cloudflare_403(resp) -> bool:
    """Return True for the Cloudflare block page GMGN serves on 403."""
    body = (getattr(resp, "text", "") or "").lower()
    return resp.status_code == 403 and (
        "cloudflare" in body or "attention required" in body or "blocked" in body
    )


def fetch_gmgn_rank(scraper, url: str, params: dict):
    """Fetch the GMGN rank endpoint, retrying once after a warmup if blocked."""
    resp = scraper.get(url, params=params, timeout=30)
    if not is_cloudflare_403(resp):
        return resp, scraper

    log.warning("GMGN returned a Cloudflare 403 block page; refreshing session and retrying once")
    scraper = build_scraper()
    warmup_scraper(scraper)
    resp = scraper.get(url, params=params, timeout=30)
    return resp, scraper

# ─────────────────────────────────────────────
#  FETCH TRENDING
# ─────────────────────────────────────────────

def fetch_trending() -> list[dict]:
    global SCRAPER
    url = f"https://gmgn.ai/defi/quotation/v1/rank/{CHAIN}/swaps/{TIME_PERIOD}"
    params = {
        "orderby":   ORDER_BY,
        "direction": "desc",
        "filters[]": FILTERS.get("api_filters", []),
    }
    time.sleep(random.uniform(1.5, 3.5))
    try:
        warmup_scraper(SCRAPER)
        resp, SCRAPER = fetch_gmgn_rank(SCRAPER, url, params)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            log.warning("GMGN non-zero response: %s", data.get("msg"))
            return []
        tokens = data.get("data", {}).get("rank", [])
        log.info("Fetched %d tokens from GMGN", len(tokens))
        return tokens
    except Exception as e:
        log.error("Fetch error: %s", e)
        SCRAPER = build_scraper()
        return []

# ─────────────────────────────────────────────
#  FILTER LOGIC
# ─────────────────────────────────────────────

def _in_range(value, mn, mx) -> bool:
    """Return True if value falls within [mn, mx]. None means no bound."""
    if value is None:
        return mn is None
    if mn is not None and value < mn:
        return False
    if mx is not None and value > mx:
        return False
    return True

def build_stats(token: dict) -> dict:
    """Extract and compute all stats for a token. Always records volume for SMA."""
    address   = token.get("address", "")
    volume    = token.get("volume") or 0
    liquidity = token.get("liquidity") or 0
    open_ts   = token.get("open_timestamp")
    age_seconds = token_age_seconds(open_ts)

    vol_sma, readings = record_volume(address, volume)
    vol_ratio = (vol_sma / liquidity) if (vol_sma and liquidity) else 0.0

    return {
        "vol_sma":    vol_sma,
        "readings":   readings,
        "vol_ratio":  vol_ratio,
        "rank":       compute_rank(vol_ratio),
        "liquidity":  liquidity,
        "market_cap": token.get("market_cap") or 0,
        "ath_mc":     token.get("ath") or 0,
        "kol":        token.get("renowned_count") or 0,
        "smart":      token.get("smart_degen_count") or 0,
        "holders":    token.get("holder_count") or 0,
        "top10_pct":  (token.get("top_10_holder_rate") or 0) * 100,
        "dev_pct":    (token.get("dev_token_burn_ratio") or 0) * 100,
        "swaps":      token.get("swaps") or 0,
        "price_chg":  token.get("price_change_percent") or 0,
        "age_days":   age_seconds / 86_400 if age_seconds is not None else None,
        "mintable":   token.get("mintable"),
        "is_migrated":token.get("is_migrated"),
        "burn_status":token.get("burn_status"),
    }

def passes_filters(token: dict):
    """Returns (passed: bool, stats: dict)."""
    f    = FILTERS
    sym  = token.get("symbol", "???")
    s    = build_stats(token)

    # Boolean flags
    if f["require_no_mint"] and s["mintable"]:
        log.debug("FAIL no_mint | %s", sym); return False, s
    if f["require_not_migrated"] and s["is_migrated"]:
        log.debug("FAIL not_migrated | %s", sym); return False, s
    if f["require_migrated"] and not s["is_migrated"]:
        log.debug("FAIL migrated | %s", sym); return False, s
    if f["require_burnt"] and not s["burn_status"]:
        log.debug("FAIL burnt | %s", sym); return False, s

    # Minimum token age
    age_seconds = token_age_seconds(token.get("open_timestamp"))
    if age_seconds is None:
        log.debug("FAIL invalid age | %s", sym); return False, s
    age_minutes = age_seconds / 60
    if age_minutes < MIN_TOKEN_AGE_MINUTES:
        log.debug("FAIL too new=%.1fmin | %s", age_minutes, sym); return False, s

    # Age
    if s["age_days"] is not None and not _in_range(s["age_days"], f["min_age_days"], f["max_age_days"]):
        log.debug("FAIL age=%.2fd | %s", s["age_days"], sym); return False, s

    # Vol SMA
    if s["vol_sma"] is None:
        log.debug("FAIL SMA building (%d/%d) | %s", s["readings"], VOL_SMA_PERIODS, sym)
        return False, s
    if s["vol_sma"] < VOL_SMA_MIN:
        log.debug("FAIL SMA=%.0f | %s", s["vol_sma"], sym); return False, s

    # Volatility ratio
    if s["vol_ratio"] < MIN_VOL_RATIO:
        log.debug("FAIL ratio=%.2f | %s", s["vol_ratio"], sym); return False, s

    # Range filters
    checks = [
        (s["liquidity"],  f["min_liquidity"],       f["max_liquidity"],       "liq"),
        (s["market_cap"], f["min_market_cap"],       f["max_market_cap"],      "mc"),
        (s["ath_mc"],     f["min_ath_market_cap"],   f["max_ath_market_cap"],  "ath"),
        (s["swaps"],      f["min_1m_txs"],           f["max_1m_txs"],          "swaps"),
        (s["kol"],        f["min_kol"],              f["max_kol"],             "kol"),
        (s["smart"],      f["min_smart"],            f["max_smart"],           "smart"),
        (s["holders"],    f["min_holders"],          f["max_holders"],         "holders"),
        (s["top10_pct"],  f["min_top10_pct"],        f["max_top10_pct"],       "top10"),
        (s["dev_pct"],    f["min_dev_holding_pct"],  f["max_dev_holding_pct"], "dev"),
    ]
    for value, mn, mx, label in checks:
        if not _in_range(value, mn, mx):
            log.debug("FAIL %s=%s | %s", label, value, sym); return False, s

    return True, s

# ─────────────────────────────────────────────
#  NOTIFICATION
# ─────────────────────────────────────────────

def format_notification(token: dict, s: dict) -> tuple[str, str]:
    """Returns (title, body) exactly as they appear on the phone."""
    symbol = token.get("symbol", "???")
    name   = token.get("name") or symbol
    addr   = token.get("address", "")

    age_days = s["age_days"]
    age_str  = (
        f"{age_days*24:.1f}h" if age_days is not None and age_days < 1
        else f"{age_days:.1f}d" if age_days is not None
        else "?"
    )

    title = f"{s['rank']} | {name} | {symbol}"

    parts = [
        f"Ratio {s['vol_ratio']:.2f}x",
        f"SMA ${s['vol_sma'] or 0:,.0f}",
        f"Liq ${s['liquidity']:,.0f}",
    ]
    if s["market_cap"]:
        parts.append(f"MC ${s['market_cap']:,.0f}")
    parts += [f"KOL {s['kol']}", f"Smart {s['smart']}"]
    if s["holders"]:
        parts.append(f"Holders {s['holders']}")
    if s["swaps"]:
        parts.append(f"Swaps {s['swaps']}")
    parts += [f"{s['price_chg']:+.1f}%", f"Age {age_str}"]

    body = " | ".join(parts) + f"\n{addr}"
    return title, body


def send_alert(token: dict, stats: dict) -> bool:
    title, message = format_notification(token, stats)
    priority = "high" if abs(stats["price_chg"]) >= 50 else "default"

    try:
        resp = SCRAPER.post(
            NTFY_URL,
            data=message.encode("utf-8"),
            headers={
                "Title":        title.encode("utf-8"),
                "Priority":     priority,
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=10,
        )
        resp.raise_for_status()
        log.info("Alert: %s | rank=%.1f | ratio=%.2fx | SMA=$%.0f | kol=%d | smart=%d",
                 token.get("symbol"), stats["rank"], stats["vol_ratio"],
                 stats["vol_sma"] or 0, stats["kol"], stats["smart"])
        return True
    except Exception as e:
        log.error("Failed to send alert for %s: %s", token.get("symbol"), e)
        return False

# ─────────────────────────────────────────────
#  DEBUG COMMAND
# ─────────────────────────────────────────────

def debug_address(address: str):
    """Search all time windows for a token and preview its notification."""
    address = address.strip()
    print(f"\n  Fetching data for {address} ...")

    token = None
    found_period = None

    for period in ["1m", "5m", "1h", "6h", "24h"]:
        try:
            url = f"https://gmgn.ai/defi/quotation/v1/rank/{CHAIN}/swaps/{period}"
            params = {"orderby": "swaps", "direction": "desc", "filters[]": []}
            time.sleep(1.0)
            resp = SCRAPER.get(url, params=params, timeout=30)
            resp.raise_for_status()
            tokens = resp.json().get("data", {}).get("rank", [])
            match = next((t for t in tokens if t.get("address") == address), None)
            if match:
                token = match
                found_period = period
                break
            print(f"  Not in {period} list, trying next...")
        except Exception as e:
            print(f"  Error searching {period}: {e}")

    if not token:
        print(f"  Token not found in any trending window (1m/5m/1h/6h/24h).\n")
        return

    print(f"  Found in {found_period} trending list.")

    # Seed vol history so preview shows a meaningful SMA
    volume = token.get("volume") or 0
    if address not in vol_history:
        seed_vol_history(address, volume)

    stats = build_stats(token)
    title, body = format_notification(token, stats)

    print(f"\n  ┌─ NOTIFICATION PREVIEW ──────────────────────────")
    print(f"  │ TITLE : {title}")
    for i, line in enumerate(body.splitlines()):
        label = "BODY  :" if i == 0 else "       "
        print(f"  │ {label} {line}")
    print(f"  └─────────────────────────────────────────────────\n")

# ─────────────────────────────────────────────
#  INPUT LISTENER
# ─────────────────────────────────────────────

def input_listener():
    """Runs in a background thread. Accepts token addresses or 'quit'."""
    while True:
        try:
            cmd = input().strip()
        except EOFError:
            break
        if not cmd:
            continue
        if cmd.lower() in ("quit", "exit", "q"):
            print("  Stopping bot...")
            os.kill(os.getpid(), 2)
            break
        debug_address(cmd)

# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────

def main():
    log.info("=" * 55)
    log.info("GMGN Alert Bot started")
    log.info("Chain: %s | Period: %s | Poll every %ds", CHAIN, TIME_PERIOD, POLL_INTERVAL)
    log.info("Vol SMA: >=$%.0f over %d candles | Min ratio: %.1fx",
             VOL_SMA_MIN, VOL_SMA_PERIODS, MIN_VOL_RATIO)
    log.info("Rank: 1.0 = %.1fx | 5.0 = %.1fx", RANK_LOW_RATIO, RANK_HIGH_RATIO)
    log.info("Filters: KOL >=%s | Smart >=%s | Min age %dmin | Max age %sd",
             FILTERS["min_kol"], FILTERS["min_smart"], MIN_TOKEN_AGE_MINUTES, FILTERS["max_age_days"])
    log.info("Cooldown: %dh per token", ALERT_COOLDOWN_MINUTES // 60)
    log.info("Notifying: %s", NTFY_URL)
    log.info("Paste a token address + Enter to preview. Type 'quit' to stop.")
    log.info("=" * 55)

    threading.Thread(target=input_listener, daemon=True).start()

    alerted: dict[str, float] = {}
    cooldown_secs = ALERT_COOLDOWN_MINUTES * 60

    while True:
        try:
            tokens = fetch_trending()
            alerts_this_cycle = 0
            near_misses = []

            for token in tokens:
                if alerts_this_cycle >= MAX_ALERTS_PER_CYCLE:
                    break

                address = token.get("address", "")
                if not address:
                    continue

                if time.time() - alerted.get(address, 0) < cooldown_secs:
                    continue

                passed, stats = passes_filters(token)

                if passed:
                    if send_alert(token, stats):
                        alerted[address] = time.time()
                        alerts_this_cycle += 1
                else:
                    kol      = token.get("renowned_count") or 0
                    smart    = token.get("smart_degen_count") or 0
                    open_ts  = token.get("open_timestamp")
                    age_days = (time.time() - open_ts) / 86_400 if open_ts else None
                    pchg     = token.get("price_change_percent") or 0
                    hist     = vol_history.get(address)
                    sma      = sum(hist) / len(hist) if hist else 0
                    readings = len(hist) if hist else 0
                    liq      = token.get("liquidity") or 0
                    ratio    = (sma / liq) if (sma and liq) else 0

                    score = sum([
                        sma   >= VOL_SMA_MIN,
                        ratio >= MIN_VOL_RATIO,
                        kol   >= (FILTERS["min_kol"] or 0),
                        smart >= (FILTERS["min_smart"] or 0),
                        age_days is not None and age_days <= (FILTERS["max_age_days"] or 999),
                    ])
                    near_misses.append((score, token.get("symbol", "?"), sma, readings,
                                        ratio, kol, smart, age_days, pchg))

            if alerts_this_cycle == 0:
                log.info("No tokens matched filters this cycle")
                near_misses.sort(key=lambda x: x[0], reverse=True)
                for score, sym, sma, readings, ratio, kol, smart, age_days, pchg in near_misses[:3]:
                    age_str = f"{age_days*24:.1f}h" if age_days is not None else "?"
                    log.info(
                        "  near miss: $%-10s  sma=$%-7.0f (%d/%d)  ratio=%.2fx"
                        "  kol=%-2d  smart=%-2d  age=%-5s  %+.1f%%  (%d/5)",
                        sym, sma, readings, VOL_SMA_PERIODS,
                        ratio, kol, smart, age_str, pchg, score
                    )

            # Prune cooldown entries older than 24h
            cutoff = time.time() - 86_400
            alerted = {k: v for k, v in alerted.items() if v > cutoff}

            # Prune vol_history — keep entries that are still in trending OR were
            # recently debug-looked-up (give them 10 minutes grace)
            active_addresses = {tok.get("address") for tok in tokens}
            for addr in list(vol_history):
                if addr not in active_addresses:
                    del vol_history[addr]

        except KeyboardInterrupt:
            log.info("Stopped.")
            break
        except Exception as e:
            log.error("Unhandled error: %s", e)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
