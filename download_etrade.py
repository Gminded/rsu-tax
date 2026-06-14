#!/usr/bin/env python3
"""
Download all "Confirmation of Release" PDFs from E*Trade benefit history.

Usage:
    python download_etrade.py

First run: opens a browser window so you can log in. The session is saved to
.etrade_session.json and all future runs operate headlessly — no visible window,
no focus stealing, no lingering tabs.  Delete .etrade_session.json to force a
fresh login.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse
from playwright.async_api import async_playwright

ETRADE_URL = "https://us.etrade.com/etx/sp/stockplan#/myAccount/benefitHistory"
OUTPUT_DIR = Path(__file__).parent / "release-confirmations"
BIN_DIR = Path(__file__).parent / "bin"
SESSION_FILE = Path(__file__).parent / ".etrade_session.json"
BUTTON_TEXT = "View Confirmation of Release"
# Exact-text match. NB: `button:has-text(...)` resolves TWO matches per button
# (Playwright subtree substring matching), so it would double every release and
# waste a timeout on each phantom. `:text-is` (exact) yields one match per button.
BUTTON_SELECTOR = f'button:text-is("{BUTTON_TEXT}")'
DOWNLOAD_TIMEOUT_S = 5

# Load rename-release-confirmations (hyphenated name requires importlib)
sys.path.insert(0, str(BIN_DIR))
_spec = importlib.util.spec_from_file_location(
    "rename_rc", BIN_DIR / "rename-release-confirmations.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
rename_file = _mod.rename_file

from parse_pdf import parse_pdf as _parse_pdf  # noqa: E402 (after sys.path insert)


async def _rename_temp(tmp: Path) -> Optional[str]:
    """Rename a downloaded temp PDF to its canonical name.

    Returns the final filename, or None if the canonical file was already on
    disk (in which case the temp file is deleted and the download is skipped).
    """
    try:
        meta = _parse_pdf(tmp)
        canonical_name = _mod.build_target_name(meta)
    except Exception:
        canonical_name = None

    final = rename_file(tmp)
    if final is None:
        # parse failed entirely — use a fallback name
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

        print("Loading benefit history page...", end=" ", flush=True)
        await page.goto(ETRADE_URL)
        await page.wait_for_load_state("load")
        await asyncio.sleep(3)  # let the SPA finish rendering

        if await page.locator('input[type="password"]').count() > 0:
            SESSION_FILE.unlink(missing_ok=True)
            print("\nSession has expired. Re-run to log in again.")
            await browser.close()
            sys.exit(1)
        print("done.")

        # Step 1: expand the "Restricted Stock (RS)" section (collapsed by default)
        print('Expanding "Restricted Stock (RS)" section...', end=' ', flush=True)
        await page.locator('button:has-text("Restricted Stock (RS)")').click()
        await asyncio.sleep(1.5)
        print('done.')

        # Step 2: "View All" loads grants beyond the initial page — click it if present.
        # Use text-is (exact, case-sensitive) to avoid matching nav "View all X" buttons.
        view_all = page.locator('button:text-is("View All")')
        if await view_all.count() > 0:
            print('Loading all grants ("View All")...', end=' ', flush=True)
            await view_all.click()
            await asyncio.sleep(2)
            print('done.')

        # Step 3: expand all grant rows to reveal individual release rows
        print('Expanding all rows...', end=' ', flush=True)
        await page.locator('[aria-label="expand all rows"]').click()

        # Wait for at least one confirmation button to appear, then poll until the
        # count stabilises — rows render asynchronously so a premature count is wrong.
        await page.wait_for_selector(BUTTON_SELECTOR, timeout=20_000)
        prev_count = -1
        stable_ticks = 0
        for _ in range(30):  # hard cap: 30 s max
            await asyncio.sleep(1)
            cur_count = await page.locator(BUTTON_SELECTOR).count()
            if cur_count > 0 and cur_count == prev_count:
                stable_ticks += 1
                if stable_ticks >= 3:
                    break
            else:
                stable_ticks = 0
                prev_count = cur_count
        count = prev_count
        print(f'done. ({count} button(s) found)')

        if count == 0:
            print('No "View Confirmation of Release" buttons found on the page.')
            await browser.close()
            sys.exit(1)

        print(f"Found {count} button(s). Starting downloads...\n")
        downloaded, skipped, failed = 0, 0, 0

        # Route-based PDF interception: intercepts getReleaseConfirmation.pdf
        # requests at the network level and uses route.fetch() to read the body
        # before the browser can discard it (which happens with page navigations).
        pdf_queue: asyncio.Queue[bytes] = asyncio.Queue()
        new_pages: list = []
        seen_ids: set[str] = set()  # safety net: dedupe by eId+cId in case a click fires twice

        async def handle_pdf_route(route, *_):
            params = parse_qs(urlparse(route.request.url).query)
            key = (params.get("eId", [""])[0], params.get("cId", [""])[0])
            if key in seen_ids:
                await route.continue_()
                return
            try:
                response = await route.fetch()
                body = await response.body()
                if body[:4] == b"%PDF":
                    seen_ids.add(key)
                    await pdf_queue.put(body)
                await route.fulfill(response=response)
            except Exception:
                await route.continue_()

        async def on_new_page(new_page):
            new_pages.append(new_page)

        await context.route("**/getReleaseConfirmation.pdf*", handle_pdf_route)
        context.on("page", on_new_page)

        try:
            for i in range(count):
                btn = page.locator(BUTTON_SELECTOR).nth(i)
                print(f"[{i + 1}/{count}] Clicking...", end=" ", flush=True)
                try:
                    await btn.click()
                    pdf_bytes = await asyncio.wait_for(
                        pdf_queue.get(), timeout=DOWNLOAD_TIMEOUT_S
                    )
                    tmp = OUTPUT_DIR / f"_tmp_{i:03d}.pdf"
                    tmp.write_bytes(pdf_bytes)
                    name = await _rename_temp(tmp)
                    if name is None:
                        print("skipped (already on disk)")
                        skipped += 1
                    else:
                        print(f"saved: {name}")
                        downloaded += 1
                except asyncio.TimeoutError:
                    print(f"FAILED (no PDF received within {DOWNLOAD_TIMEOUT_S}s)")
                    failed += 1
                except Exception as exc:
                    print(f"ERROR: {exc}")
                    failed += 1

                # Close any new tabs that opened during this button click
                for np in new_pages:
                    try:
                        await np.close()
                    except Exception:
                        pass
                new_pages.clear()
                await asyncio.sleep(0.5)
        finally:
            await context.unroute("**/getReleaseConfirmation.pdf*", handle_pdf_route)
            context.remove_listener("page", on_new_page)

        print(f"\nDone. Downloaded: {downloaded}  Skipped: {skipped}  Failed: {failed}")
        print(f"Files are in: {OUTPUT_DIR}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
