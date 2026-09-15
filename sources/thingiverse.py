"""Thingiverse listing fetcher.

Unlike MakerWorld/Printables, Thingiverse's search is paged (page=1, 2, 3, ...) rather than an
infinite-scroll list, and it sits behind a Cloudflare managed challenge - so, unlike the other
two, this one still needs a real (visible, but minimized) browser window via Patchright, which
patches the CDP-level automation fingerprint plain Playwright leaks and that Cloudflare's
challenge otherwise detects.
"""

from __future__ import annotations

import re
import shutil
import urllib.parse
from pathlib import Path
from typing import Callable

from patchright.sync_api import TimeoutError as PatchrightTimeoutError
from patchright.sync_api import sync_playwright as sync_patchright

_THING_HREF_PATTERN = re.compile(r"^/thing:\d+$")


def _with_page(url: str, page_number: int) -> str:
    """Sets/overrides the "page" query parameter of a listing URL, keeping every other
    parameter (per_page, sort, type, q, ...) exactly as the user configured them."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    query["page"] = [str(page_number)]
    new_query = urllib.parse.urlencode(query, doseq=True)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def _minimize_browser_window(context, page) -> None:
    """Minimizes the visible automation window via the CDP Browser domain.

    The --start-minimized launch arg looks like the natural way to do this, but Playwright
    itself repositions/resizes the window right after launch (to its own default bounds),
    which silently overrides the flag - the window always ends up "normal" regardless.
    Asking Chrome DevTools Protocol directly to minimize, after the context already exists,
    actually sticks. Best-effort: a failure here should never abort the scan itself.
    """
    try:
        cdp_session = context.new_cdp_session(page)
        window = cdp_session.send("Browser.getWindowForTarget")
        cdp_session.send("Browser.setWindowBounds", {"windowId": window["windowId"], "bounds": {"windowState": "minimized"}})
    except Exception:
        pass


def ensure_profile_dir(new_dir: Path, legacy_dir: Path) -> Path:
    """Resolves the persistent browser profile directory to actually launch with.

    Prefers new_dir. If it doesn't exist yet but an older build's profile is sitting at
    legacy_dir, copies it over first so an already-solved Cloudflare challenge isn't lost on
    upgrade. legacy_dir is left in place (copied, not moved) so downgrading to an older build
    still finds its cookies there too. If neither exists, new_dir is created fresh and
    Playwright starts a brand new profile in it.
    """
    if new_dir.exists() and any(new_dir.iterdir()):
        return new_dir
    if legacy_dir.exists() and any(legacy_dir.iterdir()):
        new_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(legacy_dir, new_dir, dirs_exist_ok=True)
        return new_dir
    new_dir.mkdir(parents=True, exist_ok=True)
    return new_dir


def _wait_out_cloudflare_challenge(
    page,
    status_callback: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    max_attempts: int = 12,
    wait_ms: int = 5_000,
    reload_page: bool = False,
    content_selector: str = 'a[href*="/model/"]',
) -> bool:
    """Polls a Cloudflare interstitial until it clears on its own.

    This only waits out the passive/managed challenge that a normal browser also clears
    automatically after a few seconds - it never attempts to click a verification checkbox
    or otherwise defeat an interactive challenge. When reload_page is set, a single manual
    reload is tried halfway through the wait, since that sometimes nudges the passive
    challenge into completing where just waiting does not.

    Whether the challenge is still showing is judged by the absence of content_selector,
    not by the interstitial's title text - Cloudflare serves that title translated into the
    browser's own language ("Egy pillanat..." in Hungarian, for example), so matching only
    the English "Just a moment"/"Checking your browser" strings silently failed to detect
    the challenge at all on a non-English browser.
    """
    def is_challenge_showing() -> bool:
        # Cloudflare's own auto-reload can momentarily destroy the page's execution context;
        # treat that as "still on the challenge" rather than letting it blow up the whole scan.
        try:
            return page.locator(content_selector).count() == 0
        except Exception:
            return True

    reloaded = False
    for attempt in range(max_attempts):
        if cancel_check and cancel_check():
            return False
        if not is_challenge_showing():
            return True
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
    return not is_challenge_showing()


def fetch_listing(
    url: str,
    profile_dir: Path,
    legacy_profile_dir: Path,
    max_count: int | None = None,
    unchanged_round_limit: int = 3,
    progress_callback: Callable[[int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> list[tuple[str, str, str]]:
    """Loads Thingiverse search result pages (page=1, 2, 3, ...) and returns unique thing
    cards. "Loading more" means navigating to the next page= value rather than scrolling, using
    a persistent browser profile (profile_dir, migrated from legacy_profile_dir if needed) to
    keep the Cloudflare clearance cookie between runs."""
    models: dict[str, tuple[str, str, str]] = {}
    profile_dir = ensure_profile_dir(profile_dir, legacy_profile_dir)
    start_page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("page", ["1"])[0] or "1")
    with sync_patchright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            channel="msedge",
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        # Still a real, visible window (required to pass Cloudflare) - just minimized so it
        # doesn't steal focus or clutter the screen during a normal run.
        _minimize_browser_window(context, page)
        try:
            no_growth_rounds = 0
            page_number = start_page
            while True:
                if cancel_check and cancel_check():
                    break
                page.goto(_with_page(url, page_number), wait_until="domcontentloaded", timeout=45_000)
                if page_number == start_page and status_callback:
                    status_callback("Cloudflare ellenőrzés folyamatban - ha kell, kattints át rajta a felugró ablakban...")
                if not _wait_out_cloudflare_challenge(
                    page, status_callback, cancel_check, max_attempts=24, reload_page=True,
                    content_selector='.item-card-container a[href^="/thing:"]',
                ):
                    if cancel_check and cancel_check():
                        break
                    raise ValueError("A Thingiverse Cloudflare-ellenőrzése nem oldódott fel időben. Próbáld újra kicsit később.")
                cards = page.locator(".item-card-container")
                before_count = len(models)
                for card in cards.all():
                    # Each card has several <a href="/thing:...">: one just wrapping the
                    # thumbnail image (no text) and one carrying the visible title text -
                    # the title-specific class picks the right one directly.
                    link = card.locator("a.item-card-header__title").first
                    if link.count() == 0:
                        continue
                    href = link.get_attribute("href") or ""
                    if not _THING_HREF_PATTERN.match(href):
                        continue
                    title = link.inner_text().strip()
                    if not title:
                        continue
                    model_url = urllib.parse.urljoin(page.url, href)
                    if model_url in models:
                        continue
                    image = card.locator("img").first
                    image_url = image.get_attribute("src") or "" if image.count() > 0 else ""
                    if image_url:
                        image_url = urllib.parse.urljoin(page.url, image_url)
                    # Thingiverse's search card doesn't expose a per-model creation timestamp,
                    # unlike MakerWorld's/Printables' search APIs, so this stays blank here.
                    models[model_url] = (model_url, title, image_url, "")
                    if progress_callback:
                        progress_callback(len(models))
                    if max_count and len(models) >= max_count:
                        return list(models.values())
                if len(models) == before_count:
                    # An empty page (no thing cards at all) means there simply are no more
                    # results to page through - stop right away instead of waiting out the
                    # unchanged-round limit, which is meant for "these are all already saved".
                    if cards.count() == 0:
                        break
                    no_growth_rounds += 1
                    if no_growth_rounds >= unchanged_round_limit:
                        break
                else:
                    no_growth_rounds = 0
                page_number += 1
        except PatchrightTimeoutError as error:
            raise ValueError("A Thingiverse lista nem töltődött be időben.") from error
        finally:
            context.close()
    return list(models.values())
