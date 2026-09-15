"""MakerWorld listing fetcher.

MakerWorld's search JSON API used to be plain, unauthenticated JSON reachable with a bare
HTTP request - no browser needed at all. It has since gone behind a Cloudflare managed
challenge (Cf-Mitigated: challenge), the same kind Printables/Thingiverse already needed a
browser for, so a bare request now gets a flat 403 instead of results. This opens the same
kind of real (visible, but minimized) Patchright browser window Thingiverse uses, navigates
straight to the API URL itself for each page, and waits for the response body to actually
become parseable JSON rather than a Cloudflare interstitial - keeping the same createTime-
based `hours` cutoff and pagination logic the plain-HTTP version used.
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from patchright.sync_api import TimeoutError as PatchrightTimeoutError
from patchright.sync_api import sync_playwright as sync_patchright

from .common import ensure_profile_dir, minimize_browser_window, parse_iso_datetime


def _extract_category(url: str) -> str:
    """Pulls a category slug (e.g. "900-3d-printer") out of a configured MakerWorld list URL
    like .../3d-models/900-3d-printer, if the user pointed the source at one category instead
    of the full "all models" listing. Empty string means "no category filter"."""
    match = re.search(r"/3d-models/([\w-]+)", url)
    return match.group(1) if match else ""


def _wait_for_json(
    page,
    status_callback: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    max_attempts: int = 24,
    wait_ms: int = 5_000,
    reload_page: bool = False,
) -> dict | None:
    """Polls a page navigated straight to the MakerWorld JSON API until its body actually
    parses as JSON, instead of showing Cloudflare's interstitial - unlike Thingiverse's
    HTML listing pages, there's no fixed CSS selector to check for here, so the JSON parse
    itself doubles as the "challenge cleared" signal. Returns None if it never clears."""
    def body_json() -> dict | None:
        try:
            text = page.evaluate("() => document.body.innerText")
        except Exception:
            return None
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return None

    reloaded = False
    for attempt in range(max_attempts):
        if cancel_check and cancel_check():
            return None
        data = body_json()
        if data is not None:
            return data
        if reload_page and not reloaded and attempt == max_attempts // 2:
            reloaded = True
            try:
                page.reload(wait_until="domcontentloaded", timeout=20_000)
            except Exception:
                pass
        if status_callback:
            status_callback(f"Cloudflare ellenőrzés, várakozás... ({attempt + 1}/{max_attempts})")
        try:
            page.wait_for_timeout(wait_ms)
        except Exception:
            pass
    return body_json()


def fetch_listing(
    url: str,
    profile_dir: Path,
    hours: int,
    max_count: int | None = None,
    progress_callback: Callable[[int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> list[tuple[str, str, str, str]]:
    """Fetches MakerWorld's newest-models results, stopping as soon as a result's own
    createTime falls outside the requested `hours` window.

    MakerWorld's search backend caps "total" at 10000 regardless of how many models actually
    match a query, so an undated "give me everything newer than X" scan could in principle
    have to page through up to 10000 candidates just to find where the cutoff falls.
    designCreateSince (whole days, counted from the API side, with a +1 day safety margin
    here to avoid ever narrowing it TOO much) shrinks that server-side first; the exact
    hour-level cutoff is still enforced client-side against each result's own createTime, so
    designCreateSince only needs to be roughly right, never exact.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    since_days = max(1, math.ceil(hours / 24) + 1)
    category = _extract_category(url)
    models: dict[str, tuple[str, str, str, str]] = {}
    limit = 100
    offset = 0
    profile_dir = ensure_profile_dir(profile_dir)
    with sync_patchright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            channel="msedge",
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        # Still a real, visible window (required to pass Cloudflare) - just minimized so it
        # doesn't steal focus or clutter the screen during a normal run.
        minimize_browser_window(context, page)
        try:
            while True:
                if cancel_check and cancel_check():
                    break
                query = urllib.parse.urlencode(
                    {
                        "orderBy": "newUploads",
                        "categories": category,
                        "designCreateSince": since_days,
                        "entrance": "list",
                        "designType": 0,
                        "limit": limit,
                        "offset": offset,
                    }
                )
                api_url = f"https://makerworld.com/api/v1/search-service/select/design2?{query}"
                if offset == 0 and status_callback:
                    status_callback("Cloudflare ellenőrzés folyamatban - ha kell, kattints át rajta a felugró ablakban...")
                try:
                    page.goto(api_url, wait_until="domcontentloaded", timeout=45_000)
                except PatchrightTimeoutError as error:
                    raise ValueError(f"A MakerWorld lista nem töltődött be: {error}") from error
                data = _wait_for_json(page, status_callback, cancel_check, max_attempts=24, reload_page=True)
                if data is None:
                    if cancel_check and cancel_check():
                        break
                    raise ValueError("A MakerWorld Cloudflare-ellenőrzése nem oldódott fel időben. Próbáld újra kicsit később.")
                hits = data.get("hits") or []
                if not hits:
                    break
                reached_cutoff = False
                for hit in hits:
                    created_at = parse_iso_datetime(hit.get("createTime"))
                    if created_at is None or created_at < cutoff:
                        reached_cutoff = True
                        break
                    model_id = hit.get("id")
                    if not model_id:
                        continue
                    slug = hit.get("slug") or ""
                    model_url = f"https://makerworld.com/en/models/{model_id}-{slug}" if slug else f"https://makerworld.com/en/models/{model_id}"
                    if model_url in models:
                        continue
                    title = hit.get("title") or hit.get("titleTranslated") or ""
                    image_url = hit.get("cover") or ""
                    # Stored alongside the record so the actual reason a scan stopped where it
                    # did stays visible later, not just implied by "created_at" (when *we*
                    # saved it).
                    model_created_at = created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                    models[model_url] = (model_url, title, image_url, model_created_at)
                    if progress_callback:
                        progress_callback(len(models))
                    if max_count and len(models) >= max_count:
                        return list(models.values())
                if reached_cutoff:
                    break
                offset += limit
                total = data.get("total") or 0
                if offset >= total:
                    break
                if offset >= 10_000:
                    if status_callback:
                        status_callback(
                            "A MakerWorld kereső 10 000 találat fölött nem ad több eredményt - "
                            "néhány, az időablak szélén lévő tétel emiatt kimaradhatott."
                        )
                    break
        finally:
            context.close()
    return list(models.values())
