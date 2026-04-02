"""
GMGN Trending Token Alert Bot
==============================
Polls GMGN trending Solana tokens and sends push notifications
via ntfy.sh when tokens match your filters.

Setup:
  pip install cloudscraper requests

Run:
  python gmgn_alert_bot.py
"""

import time
import logging
import random

try:
    import cloudscraper
except ImportError:
    raise SystemExit(
        "\n[ERROR] cloudscraper is not installed.\n"
        "Run:  pip install cloudscraper\n"
    )

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

NTFY_TOPIC    = "gmgnalerts-hus93J"
NTFY_URL      = f"https://ntfy.sh/{NTFY_TOPIC}"

CHAIN         = "sol"
POLL_INTERVAL = 60     # seconds between polls
TIME_PERIOD   = "1m"   # trending window: 1m | 5m | 1h | 6h | 24h
ORDER_BY      = "swaps"

# ─────────────────────────────────────────────
#  FILTERS  (set any value to None to disable)
# ─────────────────────────────────────────────

FILTERS = {

    # ── GMGN metric flags (True = required) ──────────────────────────────
    "require_no_mint":           True,   # NoMint
    "require_not_migrated":      False,
    "require_migrated":          False,
    "require_burnt":             False,
    "require_dev_still_holding": False,
    "require_dev_sell_all":      False,
    "require_dev_burnt":         False,

    # ── Age ──────────────────────────────────────────────────────────────
    "min_age_days":              None,
    "max_age_days":              2,      # max 2 days old

    # ── Liquidity (USD) ──────────────────────────────────────────────────
    "min_liquidity":             None,
    "max_liquidity":             None,

    # ── Market Cap (USD) ─────────────────────────────────────────────────
    "min_market_cap":            None,
    "max_market_cap":            None,

    # ── ATH Market Cap (USD) ─────────────────────────────────────────────
    "min_ath_market_cap":        None,
    "max_ath_market_cap":        None,

    # ── 1m Volume (USD) ──────────────────────────────────────────────────
    "min_1m_volume":             3_000,  # $3K min
    "max_1m_volume":             None,

    # ── 1m Transactions ──────────────────────────────────────────────────
    "min_1m_txs":                None,
    "max_1m_txs":                None,

    # ── KOL wallets holding the token ────────────────────────────────────
    "min_kol":                   2,
    "max_kol":                   None,

    # ── Smart money wallets holding the token ────────────────────────────
    "min_smart":                 2,
    "max_smart":                 None,

    # ── Holders ──────────────────────────────────────────────────────────
    "min_holders":               None,
    "max_holders":               None,

    # ── Top 10 holder concentration % ────────────────────────────────────
    "min_top10_pct":             None,
    "max_top10_pct":             None,

    # ── Dev holding % ────────────────────────────────────────────────────
    "min_dev_holding_pct":       None,
    "max_dev_holding_pct":       None,

    # ── GMGN-side API filters ─────────────────────────────────────────────
    "api_filters":               ["not_honeypot"],
}

# A token won't re-alert for this many minutes after being notified
ALERT_COOLDOWN_MINUTES = 60

MAX_ALERTS_PER_CYCLE   = 5

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
#  CLOUDSCRAPER SESSION
# ─────────────────────────────────────────────

def build_scraper():
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False},
        delay=5,
    )
    scraper.headers.update({
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://gmgn.ai/sol/token",
        "Origin":          "https://gmgn.ai",
    })
    return scraper

SCRAPER = build_scraper()

# ─────────────────────────────────────────────
#  GMGN FETCH
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
        resp = SCRAPER.get(url, params=params, timeout=30)
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

def _in_range(value, min_val, max_val) -> bool:
    if value is None:
        return min_val is None
    if min_val is not None and value < min_val:
        return False
    if max_val is not None and value > max_val:
        return False
    return True

def passes_filters(token: dict) -> tuple[bool, list[str]]:
    f       = FILTERS
    reasons = []
    sym     = token.get("symbol", "???")

    volume        = token.get("volume") or 0
    liquidity     = token.get("liquidity") or 0
    market_cap    = token.get("market_cap") or 0
    ath_mc        = token.get("ath") or 0
    swaps         = token.get("swaps") or 0
    kol_count     = token.get("renowned_count") or 0
    smart_count   = token.get("smart_degen_count") or 0
    holders       = token.get("holder_count") or 0
    top10_pct     = (token.get("top_10_holder_rate") or 0) * 100
    dev_pct       = (token.get("dev_token_burn_ratio") or 0) * 100
    price_chg     = token.get("price_change_percent") or 0
    mintable      = token.get("mintable")
    is_migrated   = token.get("is_migrated")
    burn_status   = token.get("burn_status")
    open_ts       = token.get("open_timestamp")
    age_days      = (time.time() - open_ts) / 86_400 if open_ts else None

    # Boolean flags
    if f["require_no_mint"] and mintable:
        log.debug("FAIL no_mint | %s", sym); return False, []
    if f["require_not_migrated"] and is_migrated:
        log.debug("FAIL not_migrated | %s", sym); return False, []
    if f["require_migrated"] and not is_migrated:
        log.debug("FAIL migrated | %s", sym); return False, []
    if f["require_burnt"] and not burn_status:
        log.debug("FAIL burnt | %s", sym); return False, []

    # Age
    if age_days is not None:
        if not _in_range(age_days, f["min_age_days"], f["max_age_days"]):
            log.debug("FAIL age=%.2fd | %s", age_days, sym); return False, []
        label = f"{age_days*24:.1f}h old" if age_days < 1 else f"{age_days:.1f}d old"
        reasons.append(label)

    # Volume
    if not _in_range(volume, f["min_1m_volume"], f["max_1m_volume"]):
        log.debug("FAIL vol=%.0f | %s", volume, sym); return False, []
    reasons.append(f"Vol ${volume:,.0f}")

    # Liquidity
    if not _in_range(liquidity, f["min_liquidity"], f["max_liquidity"]):
        log.debug("FAIL liq=%.0f | %s", liquidity, sym); return False, []
    if liquidity:
        reasons.append(f"Liq ${liquidity:,.0f}")

    # Market cap
    if not _in_range(market_cap, f["min_market_cap"], f["max_market_cap"]):
        log.debug("FAIL mc=%.0f | %s", market_cap, sym); return False, []
    if market_cap:
        reasons.append(f"MC ${market_cap:,.0f}")

    # ATH market cap
    if not _in_range(ath_mc, f["min_ath_market_cap"], f["max_ath_market_cap"]):
        log.debug("FAIL ath=%.0f | %s", ath_mc, sym); return False, []

    # Swaps
    if not _in_range(swaps, f["min_1m_txs"], f["max_1m_txs"]):
        log.debug("FAIL swaps=%d | %s", swaps, sym); return False, []
    if swaps:
        reasons.append(f"{swaps} swaps")

    # KOL
    if not _in_range(kol_count, f["min_kol"], f["max_kol"]):
        log.debug("FAIL kol=%d | %s", kol_count, sym); return False, []
    reasons.append(f"{kol_count} KOL")

    # Smart money
    if not _in_range(smart_count, f["min_smart"], f["max_smart"]):
        log.debug("FAIL smart=%d | %s", smart_count, sym); return False, []
    reasons.append(f"{smart_count} smart")

    # Holders
    if not _in_range(holders, f["min_holders"], f["max_holders"]):
        log.debug("FAIL holders=%d | %s", holders, sym); return False, []
    if holders:
        reasons.append(f"{holders} holders")

    # Top 10 %
    if not _in_range(top10_pct, f["min_top10_pct"], f["max_top10_pct"]):
        log.debug("FAIL top10=%.1f%% | %s", top10_pct, sym); return False, []

    # Dev holding %
    if not _in_range(dev_pct, f["min_dev_holding_pct"], f["max_dev_holding_pct"]):
        log.debug("FAIL dev=%.1f%% | %s", dev_pct, sym); return False, []

    reasons.append(f"{price_chg:+.1f}%")
    return True, reasons

# ─────────────────────────────────────────────
#  NOTIFICATIONS
# ─────────────────────────────────────────────

def send_alert(token: dict, reasons: list[str]) -> bool:
    symbol    = token.get("symbol", "???")
    address   = token.get("address", "")
    price     = token.get("price") or 0
    price_chg = token.get("price_change_percent") or 0

    gmgn_link = f"https://gmgn.ai/sol/token/{address}"
    dex_link  = f"https://dexscreener.com/solana/{address}"

    title    = f"${symbol} — GMGN trending"
    message  = (
        f"{' | '.join(reasons)}\n"
        f"Price: ${price:.8g}\n"
        f"GMGN: {gmgn_link}\n"
        f"Chart: {dex_link}"
    )
    priority = "high" if abs(price_chg) >= 50 else "default"

    try:
        resp = SCRAPER.post(
            NTFY_URL,
            data=message.encode("utf-8"),
            headers={
                "Title":        title.encode("utf-8"),
                "Priority":     priority,
                "Tags":         "chart_with_upwards_trend,fire",
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=10,
        )
        resp.raise_for_status()
        log.info("✅ Alert: $%s | %s", symbol, " | ".join(reasons))
        return True
    except Exception as e:
        log.error("Failed to send alert for %s: %s", symbol, e)
        return False

# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────

def main():
    log.info("=" * 55)
    log.info("GMGN Alert Bot started")
    log.info("Chain: %s | Period: %s | Poll every %ds", CHAIN, TIME_PERIOD, POLL_INTERVAL)
    log.info("Filters: Vol >$%s | KOL >=%s | Smart >=%s | Max age %sd",
             FILTERS["min_1m_volume"], FILTERS["min_kol"],
             FILTERS["min_smart"], FILTERS["max_age_days"])
    log.info("Notifying: %s", NTFY_URL)
    log.info("=" * 55)

    # {address: unix timestamp of last alert}
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

                # Skip if already alerted within cooldown window
                if time.time() - alerted.get(address, 0) < cooldown_secs:
                    continue

                passed, reasons = passes_filters(token)
                if passed:
                    if send_alert(token, reasons):
                        alerted[address] = time.time()
                        alerts_this_cycle += 1
                else:
                    sym      = token.get("symbol", "???")
                    vol      = token.get("volume") or 0
                    kol      = token.get("renowned_count") or 0
                    smart    = token.get("smart_degen_count") or 0
                    open_ts  = token.get("open_timestamp")
                    age_days = (time.time() - open_ts) / 86_400 if open_ts else None
                    pchg     = token.get("price_change_percent") or 0

                    score = 0
                    if vol   >= (FILTERS["min_1m_volume"] or 0): score += 1
                    if kol   >= (FILTERS["min_kol"] or 0):       score += 1
                    if smart >= (FILTERS["min_smart"] or 0):      score += 1
                    if age_days is not None and age_days <= (FILTERS["max_age_days"] or 999): score += 1

                    near_misses.append((score, sym, vol, kol, smart, age_days, pchg))

            if alerts_this_cycle == 0:
                log.info("No tokens matched filters this cycle")
                near_misses.sort(key=lambda x: x[0], reverse=True)
                for score, sym, vol, kol, smart, age_days, pchg in near_misses[:3]:
                    age_str = f"{age_days*24:.1f}h" if age_days is not None else "?"
                    log.info(
                        "  near miss: $%-10s  vol=$%-8.0f  kol=%-2d  smart=%-2d  age=%-6s  %+.1f%%  (%d/4 filters)",
                        sym, vol, kol, smart, age_str, pchg, score
                    )

            # Prune cooldown entries older than 24h
            cutoff = time.time() - 86_400
            alerted = {k: v for k, v in alerted.items() if v > cutoff}

        except KeyboardInterrupt:
            log.info("Stopped.")
            break
        except Exception as e:
            log.error("Unhandled error: %s", e)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
