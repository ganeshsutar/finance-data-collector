"""Download NSE option chain snapshots and store them as LZMA-compressed CSVs.

Layout: data/<SYMBOL>/<YYYY-MM-DD>/<YYYY-MM-DD_HH-MM-SS>.csv.xz  (IST)

The CSV has the same columns, in the same order, as the "Download (.csv)"
button on https://www.nseindia.in/option-chain (nearest expiry).
"""

from __future__ import annotations

import argparse
import csv
import logging
import lzma
import os
import time
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("nse")

BASE_URL = "https://www.nseindia.in"
IST = ZoneInfo("Asia/Kolkata")

# Index symbols offered on the option chain page.
INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"]

# (JSON field, header) for each side, in the site's table order.
CALL_COLS = [
    ("openInterest", "OI"),
    ("changeinOpenInterest", "CHNG IN OI"),
    ("totalTradedVolume", "VOLUME"),
    ("impliedVolatility", "IV"),
    ("lastPrice", "LTP"),
    ("change", "CHNG"),
    ("buyQuantity1", "BID QTY"),
    ("buyPrice1", "BID"),
    ("sellPrice1", "ASK"),
    ("sellQuantity1", "ASK QTY"),
]
PUT_COLS = [
    ("buyQuantity1", "BID QTY"),
    ("buyPrice1", "BID"),
    ("sellPrice1", "ASK"),
    ("sellQuantity1", "ASK QTY"),
    ("change", "CHNG"),
    ("lastPrice", "LTP"),
    ("impliedVolatility", "IV"),
    ("totalTradedVolume", "VOLUME"),
    ("changeinOpenInterest", "CHNG IN OI"),
    ("openInterest", "OI"),
]
HEADER = (
    [f"CALLS {h}" for _, h in CALL_COLS]
    + ["STRIKE"]
    + [f"PUTS {h}" for _, h in PUT_COLS]
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


class NSEClient:
    """Thin wrapper over NSE's JSON API. NSE needs session cookies, which it
    sets when the option chain page is loaded, and it rejects HTTP/2 clients
    (requests only speaks HTTP/1.1, which is what we want)."""

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": f"{BASE_URL}/option-chain",
            }
        )
        self._warm = False

    def warm_up(self) -> None:
        self.session.cookies.clear()
        self.session.get(f"{BASE_URL}/option-chain", timeout=self.timeout)
        self._warm = True

    def get_json(self, path: str, params: dict | None = None, retries: int = 3):
        if not self._warm:
            self.warm_up()
        last_err: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                r = self.session.get(
                    f"{BASE_URL}{path}", params=params, timeout=self.timeout
                )
                if r.status_code in (401, 403):
                    raise requests.HTTPError(f"{r.status_code} (session expired?)")
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as e:
                last_err = e
                log.debug("GET %s %s failed (attempt %d): %s", path, params, attempt, e)
                time.sleep(2 * attempt)
                self.warm_up()  # fresh cookies before retrying
        raise RuntimeError(f"GET {path} {params} failed: {last_err}")

    def fno_stocks(self) -> list[str]:
        return list(self.get_json("/api/master-quote"))

    def expiries(self, symbol: str) -> list[str]:
        data = self.get_json("/api/option-chain-contract-info", {"symbol": symbol})
        return data.get("expiryDates", [])

    def option_chain(self, symbol: str, expiry: str) -> dict:
        kind = "Indices" if symbol in INDICES else "Equity"
        return self.get_json(
            "/api/option-chain-v3",
            {"type": kind, "symbol": symbol, "expiry": expiry},
        )


def to_rows(chain: dict) -> list[list]:
    rows = []
    for rec in sorted(chain.get("records", {}).get("data", []), key=lambda r: r["strikePrice"]):
        ce, pe = rec.get("CE") or {}, rec.get("PE") or {}
        rows.append(
            [ce.get(k, "") for k, _ in CALL_COLS]
            + [rec["strikePrice"]]
            + [pe.get(k, "") for k, _ in PUT_COLS]
        )
    return rows


def write_csv_xz(path: Path, rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with lzma.open(tmp, "wt", encoding="utf-8", newline="", preset=9) as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)
    os.replace(tmp, path)  # never leave a half-written file under the real name


class Collector:
    def __init__(self, out_dir: Path, symbols: list[str] | None, delay: float):
        self.client = NSEClient()
        self.out_dir = out_dir
        self.fixed_symbols = symbols
        self.delay = delay
        self._expiry_cache: dict[str, tuple[str, str]] = {}  # symbol -> (date, expiry)
        self._last_ts: dict[str, str] = {}  # symbol -> NSE data timestamp last saved

    def symbols(self) -> list[str]:
        if self.fixed_symbols:
            return self.fixed_symbols
        return INDICES + self.client.fno_stocks()

    def nearest_expiry(self, symbol: str, today: str) -> str | None:
        cached = self._expiry_cache.get(symbol)
        if cached and cached[0] == today:
            return cached[1]
        exps = self.client.expiries(symbol)
        time.sleep(self.delay)
        if not exps:
            return None
        self._expiry_cache[symbol] = (today, exps[0])
        return exps[0]

    def collect_one(self, symbol: str) -> Path | None:
        now = datetime.now(IST)
        today = now.strftime("%Y-%m-%d")
        expiry = self.nearest_expiry(symbol, today)
        if not expiry:
            log.warning("%s: no expiries listed, skipping", symbol)
            return None
        chain = self.client.option_chain(symbol, expiry)
        nse_ts = chain.get("records", {}).get("timestamp", "")
        if nse_ts and self._last_ts.get(symbol) == nse_ts:
            log.info("%s: data unchanged since %s, not saved", symbol, nse_ts)
            return None
        rows = to_rows(chain)
        if not rows:
            log.warning("%s: empty option chain for %s", symbol, expiry)
            return None
        path = self.out_dir / symbol / today / f"{now:%Y-%m-%d_%H-%M-%S}.csv.xz"
        write_csv_xz(path, rows)
        self._last_ts[symbol] = nse_ts
        log.info("%s %s: %d strikes -> %s", symbol, expiry, len(rows), path)
        return path

    def run_cycle(self) -> tuple[int, int]:
        symbols = self.symbols()
        ok = failed = 0
        log.info("Cycle start: %d symbols", len(symbols))
        for sym in symbols:
            try:
                if self.collect_one(sym):
                    ok += 1
            except Exception as e:  # keep going; one bad symbol must not stop the run
                failed += 1
                log.error("%s: %s", sym, e)
            time.sleep(self.delay)
        log.info("Cycle done: %d saved, %d failed", ok, failed)
        return ok, failed


def market_open(now: datetime) -> bool:
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        description="Download NSE option chain CSVs (LZMA-compressed) on a schedule."
    )
    p.add_argument("--out", default="data", type=Path, help="output folder (default: data)")
    p.add_argument("--symbols", nargs="+", help="only these symbols, e.g. NIFTY ABB")
    p.add_argument("--interval", type=int, default=300,
                   help="seconds between cycle starts (default: 300)")
    p.add_argument("--delay", type=float, default=1.0,
                   help="seconds to wait between requests (default: 1.0)")
    p.add_argument("--once", action="store_true", help="run a single cycle and exit")
    p.add_argument("--ignore-market-hours", action="store_true",
                   help="also run outside Mon-Fri 09:15-15:30 IST")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    symbols = [s.upper() for s in args.symbols] if args.symbols else None
    collector = Collector(args.out, symbols, args.delay)

    if args.once:
        collector.run_cycle()
        return

    log.info("Scheduler started: every %ss, output in %s", args.interval, args.out.resolve())
    while True:
        start = time.monotonic()
        if args.ignore_market_hours or market_open(datetime.now(IST)):
            try:
                collector.run_cycle()
            except Exception as e:
                log.error("Cycle failed: %s", e)
        else:
            log.debug("Market closed, waiting")
        time.sleep(max(5.0, args.interval - (time.monotonic() - start)))
