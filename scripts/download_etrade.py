#!/usr/bin/env python3
# Copyright (C) 2025 Gianluca Guidi
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see
# <https://www.gnu.org/licenses/>.
"""
Download all "Confirmation of Release" PDFs from E*Trade.

Usage:
    python scripts/download_etrade.py

First run: opens a browser window so you can log in. The session is saved to
.etrade_session.json and all future runs operate headlessly — no visible window,
no focus stealing, no lingering tabs.  Delete .etrade_session.json to force a
fresh login.

Source of truth
---------------
This uses the "Stock Plan Confirmations" page
(https://us.etrade.com/etx/sp/stockplan#/myAccount/stockPlanConfirmations),
which is backed by a JSON API that returns an authoritative list of *every*
confirmation in a date range, each with a unique confirmationId.  We download
each PDF directly by that id.

This is far more reliable than the older "Benefit History" approach, which
required expanding every grant row and clicking 100+ buttons, then correlating
asynchronously-arriving PDFs back to their button — a race that could silently
drop or duplicate releases (and under-count total holdings as a result).
"""

import asyncio
import csv
import datetime as dt
import importlib.util
import re
import sys
from pathlib import Path
from typing import Optional
from playwright.async_api import async_playwright

# The page we log in against and scrape the API token from.
ETRADE_URL = "https://us.etrade.com/etx/sp/stockplan#/myAccount/stockPlanConfirmations"
# JSON API that lists confirmations for a date range.
CONFIRMATIONS_API = "https://us.etrade.com/webapisp/stockplan/ah/confirmations.json"
# Per-confirmation PDF endpoint (eId = encrypted employee id, cId = confirmationId).
PDF_API = "https://us.etrade.com/webapisp/stockplan/pdf/getReleaseConfirmation.pdf"
# JSON API that lists executed orders (disposals) for a date range.
ORDERS_API = "https://us.etrade.com/webapisp/stockplan/ah/orders.json"
# Pull everything from the start of the first grant year to today.
START_DATE = "1/1/2018"
START_YEAR = 2018

SCRIPTS_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPTS_DIR.parent
OUTPUT_DIR = PROJECT_DIR / "release-confirmations"
ORDERS_CSV = PROJECT_DIR / "sales" / "orders.csv"
SESSION_FILE = PROJECT_DIR / ".etrade_session.json"

# Broker/SEC charges on a disposal — all allowable incidental costs (TCGA 1992
# s.38(1)(c)).  Summed into one Fee.  applicableTax is income tax, NOT a disposal
# cost, so it is deliberately excluded.
_FEE_FIELDS = ("commissionFee", "postageHandlingFee", "brokerAssitFee",
               "specialHandlingFee", "secFee")
# orders.csv schema — names load_sales (in calculate-cost-basis.py) recognises,
# matching sales/sales.csv so both files load identically.
ORDERS_COLS = ["Date", "Type", "Shares", "Price per share ($)", "Fee ($)"]

# Load rename-release-confirmations (hyphenated name requires importlib)
sys.path.insert(0, str(SCRIPTS_DIR))
_spec = importlib.util.spec_from_file_location(
    "rename_rc", SCRIPTS_DIR / "rename-release-confirmations.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
rename_file = _mod.rename_file

from parse_pdf import parse_pdf as _parse_pdf  # noqa: E402 (after sys.path insert)


def _rename_temp(tmp: Path) -> Optional[str]:
    """Rename a downloaded temp PDF to its canonical name.

    Returns the final filename, or None if the canonical file was already on
    disk (in which case the temp file is deleted and the download is skipped —
    this makes re-runs idempotent).
    """
    try:
        meta = _parse_pdf(tmp)
        canonical_name = _mod.build_target_name(meta)
    except Exception:
        canonical_name = None

    final = rename_file(tmp)
    if final is None:
        # parse failed entirely — use a fallback name so nothing is lost
        fallback = OUTPUT_DIR / f"confirmation_{tmp.stem.lstrip('_tmp_')}.pdf"
        tmp.rename(fallback)
        return fallback.name

    # If rename_file's result differs from the canonical name, unique_path()
    # added a numeric suffix, meaning the canonical file already existed.
    if canonical_name is not None and final.name != canonical_name:
        final.unlink(missing_ok=True)
        return None

    return final.name


async def do_login(p) -> None:
    """Open a visible browser, let the user log in, and persist the session."""
    browser = await p.chromium.launch(headless=False)
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(ETRADE_URL)
    print("Log in to E*Trade in the browser window.")
    print("You do not need to navigate anywhere — just complete the login.")
    input("\nPress Enter once you are logged in > ")
    await context.storage_state(path=str(SESSION_FILE))
    print(f"Session saved to {SESSION_FILE.name}. The browser will now close.\n")
    await browser.close()


async def _capture_api_token(page) -> dict:
    """Load the confirmations page and capture the params the SPA uses to call
    the JSON API: the encrypted employee id and the per-session `stk1` token.

    The page fires a confirmations.json POST on load; we read both values off
    that request rather than hard-coding them.
    """
    import json as _json

    token: dict = {}

    async def on_request(req):
        if "confirmations.json" in req.url and req.method == "POST" and req.post_data:
            try:
                token["eId"] = _json.loads(req.post_data)["value"]["encryptedEmployeeId"]
                headers = await req.all_headers()
                if "stk1" in headers:
                    token["stk1"] = headers["stk1"]
            except Exception:
                pass

    page.on("request", on_request)
    await page.goto(ETRADE_URL)
    await page.wait_for_load_state("load")
    # The SPA renders and fires the API call asynchronously; give it a moment.
    for _ in range(20):
        await asyncio.sleep(0.5)
        if "eId" in token and "stk1" in token:
            break
    page.remove_listener("request", on_request)
    return token


def _order_date(o: dict) -> Optional[str]:
    """Trade-execution date of an order as 'YYYY-MM-DD'.

    Prefer executionDate ('YYYYMMDDhhmmss'); fall back to actionDate
    ('MM/DD/YYYY').  This is the disposal's contract date for HMRC, not the later
    settlement date."""
    ed = str(o.get("executionDate") or "").strip()
    if len(ed) >= 8 and ed[:8].isdigit():
        return f"{ed[0:4]}-{ed[4:6]}-{ed[6:8]}"
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(o.get("actionDate") or "").strip())
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def _orders_to_rows(orders: list[dict]) -> list[dict]:
    """Convert the orders.json `data.pse.list` into orders.csv row dicts.

    Pure (no I/O) so it is unit-testable against a captured sample.  Only Settled
    orders are real disposals: Cancelled/Rejected/Open/Pending orders never
    completed and must be discarded (a cancelled order is not a chargeable
    disposal).  Rows with no executed price or shares are skipped defensively.
    """
    rows = []
    for o in orders:
        status = str(o.get("status") or "").strip().lower()
        if status != "settled" and str(o.get("statusCode") or "").upper() != "SE":
            continue
        shares = o.get("numberOfShares") or 0
        price = o.get("executedPrice") or o.get("avgPrice") or 0
        if not shares or not price:
            continue
        date = _order_date(o)
        if not date:
            continue
        fee = sum(float(o.get(f) or 0) for f in _FEE_FIELDS)
        rows.append({
            "Date":                date,
            "Type":                o.get("transType") or "Sell",
            "Shares":              shares,
            "Price per share ($)": price,
            "Fee ($)":             round(fee, 2),
        })
    return rows


async def fetch_orders(context, token: dict) -> list[dict]:
    """Pull executed orders year-by-year from START_YEAR to this year.

    Queried per calendar year because the captured payload spans a single year;
    results are deduplicated by tradeId so a server that ignores our dates (the
    dateRangeSet preset) cannot inflate the list.
    # ponytail: dedupe by tradeId guards the per-year loop against duplicate returns.
    """
    seen: set = set()
    raw: list[dict] = []
    for year in range(START_YEAR, dt.date.today().year + 1):
        payload = {"value": {
            "encryptedEmployeeId": token["eId"],
            "startDate":   f"01/01/{year}",
            "endDate":     f"12/31/{year}",
            "dateRangeSet": 1,
            "getTradeDetails": "N",
            "getOrderHistory": "N",
            "getNetProceeds":  "N",
        }}
        resp = await context.request.post(
            ORDERS_API,
            data=payload,
            headers={
                "content-type": "application/json; charset=UTF-8",
                "accept": "application/json, text/plain, */*",
                "stk1": token["stk1"],
            },
        )
        body = await resp.json()
        orders = (((body or {}).get("data") or {}).get("pse") or {}).get("list") or []
        for o in orders:
            tid = o.get("tradeId")
            if tid in seen:
                continue
            seen.add(tid)
            raw.append(o)
    return _orders_to_rows(raw)


async def download_orders(context, token: dict) -> None:
    """Fetch executed orders and write sales/orders.csv (the disposal feed)."""
    print("Fetching order history...", end=" ", flush=True)
    try:
        rows = await fetch_orders(context, token)
    except Exception as exc:
        print(f"FAILED ({exc}). Skipping orders.csv.")
        return
    rows.sort(key=lambda r: r["Date"])
    ORDERS_CSV.parent.mkdir(exist_ok=True)
    with ORDERS_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ORDERS_COLS)
        w.writeheader()
        w.writerows(rows)
    if rows:
        print(f"done. ({len(rows)} order(s), {rows[0]['Date']} → {rows[-1]['Date']})")
        print(f"  Wrote {ORDERS_CSV.relative_to(PROJECT_DIR)}. "
              f"Earliest order: {rows[0]['Date']} — older sell-to-cover sales (if "
              f"any) must be added manually to sales/sales.csv.")
    else:
        print("done. (no executed orders found)")


async def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

    async with async_playwright() as p:
        if not SESSION_FILE.exists():
            print("No saved session found. Starting interactive login...\n")
            await do_login(p)

        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            storage_state=str(SESSION_FILE),
            accept_downloads=True,
        )
        page = await context.new_page()

        print("Loading Stock Plan Confirmations page...", end=" ", flush=True)
        token = await _capture_api_token(page)

        if await page.locator('input[type="password"]').count() > 0:
            SESSION_FILE.unlink(missing_ok=True)
            print("\nSession has expired. Re-run to log in again.")
            await browser.close()
            sys.exit(1)

        if "eId" not in token or "stk1" not in token:
            print("\nCould not read the API token from the page. "
                  "E*Trade may have changed their site.")
            await browser.close()
            sys.exit(1)
        print("done.")

        # Fetch the authoritative list of confirmations for the full date range.
        print("Fetching confirmation list...", end=" ", flush=True)
        today = dt.date.today().strftime("%-m/%-d/%Y")
        payload = {"value": {
            "encryptedEmployeeId": token["eId"],
            "taxYear": "",
            "startDate": START_DATE,
            "endDate": today,
            "planTypeCode": "All",
            "appType": "STOCKPLAN",
        }}
        resp = await context.request.post(
            CONFIRMATIONS_API,
            data=payload,
            headers={
                "content-type": "application/json; charset=UTF-8",
                "accept": "application/json, text/plain, */*",
                "stk1": token["stk1"],
            },
        )
        body = await resp.json()
        try:
            confirmations = body["data"]["confirmation"]["confirmations"]
        except (TypeError, KeyError):
            print("\nUnexpected API response — could not read confirmation list.")
            await browser.close()
            sys.exit(1)
        count = len(confirmations)
        print(f"done. ({count} confirmation(s) found)")

        if count == 0:
            print("No confirmations found in the selected date range.")
            await browser.close()
            sys.exit(1)

        print(f"Starting downloads...\n")
        downloaded, skipped, failed = 0, 0, 0

        for i, conf in enumerate(confirmations):
            cid = conf.get("confirmationId")
            date = conf.get("confirmationDate", "?")
            print(f"[{i + 1}/{count}] {date} (cId {cid})...", end=" ", flush=True)
            try:
                r = await context.request.get(
                    PDF_API, params={"eId": token["eId"], "cId": cid}
                )
                data = await r.body()
                if data[:4] != b"%PDF":
                    print("FAILED (response was not a PDF)")
                    failed += 1
                    continue
                tmp = OUTPUT_DIR / f"_tmp_{cid}.pdf"
                tmp.write_bytes(data)
                name = _rename_temp(tmp)
                if name is None:
                    print("skipped (already on disk)")
                    skipped += 1
                else:
                    print(f"saved: {name}")
                    downloaded += 1
            except Exception as exc:
                print(f"ERROR: {exc}")
                failed += 1

        print(f"\nDone. Downloaded: {downloaded}  Skipped: {skipped}  Failed: {failed}")
        print(f"Files are in: {OUTPUT_DIR}")

        await download_orders(context, token)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
