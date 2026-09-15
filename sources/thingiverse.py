"""Thingiverse listing fetcher.

Unlike MakerWorld/Printables, Thingiverse's search is paged (page=1, 2, 3, ...) rather than an
infinite-scroll list, and it sits behind a Cloudflare managed challenge, so (like MakerWorld,
unlike Printables) it needs a real (visible, but minimized) browser window via Patchright,
which patches the CDP-level automation fingerprint plain Playwright leaks and that Cloudflare's
challenge otherwise detects.

Neither the search results nor the official REST API (api.thingiverse.com, which requires an
authenticated app token this project doesn't have) expose a per-model creation date anywhere
convenient - but the public thing page itself does show one next to the author name (e.g.
"September 15, 2026", day precision only, no time of day). So for every newly seen card, this
also opens that thing's own page to read it - one extra page load per model, considerably
slower than MakerWorld/Printables, but it's the only place the date actually lives - and, once
we have it, applies the same "stop once a result falls outside `hours`" cutoff they use, with
day-level precision instead of hour-level.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from patchright.sync_api import TimeoutError as PatchrightTimeoutError
from patchright.sync_api import sync_playwright as sync_patchright

from .common import ensure_profile_dir, minimize_browser_window

_THING_HREF_PATTERN = re.compile(r"^/thing:\d+$")
# A hash-suffixed CSS-module class ("DetailPageTitle__thingTitleMeta--P50Xo") backs this, but
# the "DetailPageTitle__thingTitleMeta--" prefix itself has held across at least one Thingiverse
# deploy - a substring match on the prefix survives the hash suffix changing under it, instead
# of pinning to one exact generated class name.
_THING_DATE_SELECTOR = '[class*="DetailPageTitle__thingTitleMeta--"] > div'


def _with_page(url: str, page_number: int) -> str:
    """Sets/overrides the "page" query parameter of a listing URL, keeping every other
    parameter (per_page, sort, type, q, ...) exactly as the user configured them."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    query["page"] = [str(page_number)]
    new_query = urllib.parse.urlencode(query, doseq=True)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


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


def _fetch_model_created_at(
    page,
    model_url: str,
    status_callback: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> str:
    """Opens one thing's own page and reads its publish date next to the author name (e.g.
    "September 15, 2026" - always English, regardless of browser locale, since Thingiverse
    itself has no other UI language). Returns "" (never raises) on any failure - a page that
    won't load, an unexpected date format, or a Cloudflare challenge that doesn't clear -
    since a single model's missing date shouldn't abort the whole scan."""
    try:
        page.goto(model_url, wait_until="domcontentloaded", timeout=45_000)
    except PatchrightTimeoutError:
        return ""
    if not _wait_out_cloudflare_challenge(
        page, status_callback, cancel_check, max_attempts=12, reload_page=True,
        content_selector=_THING_DATE_SELECTOR,
    ):
        return ""
    try:
        text = page.locator(_THING_DATE_SELECTOR).first.inner_text().strip()
        return datetime.strptime(text, "%B %d, %Y").strftime("%Y-%m-%d 00:00:00")
    except Exception:
        return ""


def fetch_listing(
    url: str,
    profile_dir: Path,
    legacy_profile_dir: Path,
    hours: int,
    max_count: int | None = None,
    unchanged_round_limit: int = 3,
    progress_callback: Callable[[int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> list[tuple[str, str, str, str]]:
    """Loads Thingiverse search result pages (page=1, 2, 3, ...) and returns unique thing
    cards, each with its own publish date fetched from its thing page (see
    _fetch_model_created_at). "Loading more" means navigating to the next page= value rather
    than scrolling, using a persistent browser profile (profile_dir, migrated from
    legacy_profile_dir if needed) to keep the Cloudflare clearance cookie between runs.

    Stops as soon as a result's date falls outside the requested `hours` window, the same way
    makerworld.fetch_listing/printables.fetch_listing do - just compared as whole calendar
    days instead of exact timestamps, since a day is all Thingiverse's own thing page exposes
    ("today" always counts as within the window, no matter how small `hours` is - there's no
    time of day to compare against). A model whose date couldn't be read at all (network
    hiccup, unexpected page layout, ...) is kept rather than used to decide the cutoff, so one
    bad read can't wrongly truncate the whole scan.
    """
    cutoff_date = (datetime.now() - timedelta(hours=hours)).date()
    models: dict[str, tuple[str, str, str, str]] = {}
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
        minimize_browser_window(context, page)
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
                # Collected up front, off the live search-result cards, before navigating away
                # to any thing page below to read its date - the card Locators only stay valid
                # for this page's current DOM, not across a goto() to another URL and back.
                card_data: list[tuple[str, str, str]] = []
                for card in page.locator(".item-card-container").all():
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
                    card_data.append((model_url, title, image_url))
                card_count = page.locator(".item-card-container").count()

                before_count = len(models)
                reached_cutoff = False
                for model_url, title, image_url in card_data:
                    if cancel_check and cancel_check():
                        break
                    if model_url in models:
                        continue
                    model_created_at = _fetch_model_created_at(page, model_url, status_callback, cancel_check)
                    if model_created_at:
                        created_date = datetime.strptime(model_created_at, "%Y-%m-%d %H:%M:%S").date()
                        # Compared as whole calendar days, not exact timestamps: the site only
                        # ever gives us a day, stored here as that day's midnight, and a plain
                        # `< cutoff` against a real point in time would wrongly treat "posted
                        # today" as already too old for any `hours` under ~24 (today's midnight
                        # is always more than a few hours in the past). This way "today" always
                        # counts as within any window, no matter how small.
                        if created_date < cutoff_date:
                            reached_cutoff = True
                            break
                    models[model_url] = (model_url, title, image_url, model_created_at)
                    if progress_callback:
                        progress_callback(len(models))
                    if max_count and len(models) >= max_count:
                        return list(models.values())
                if reached_cutoff:
                    break
                if len(models) == before_count:
                    # An empty page (no thing cards at all) means there simply are no more
                    # results to page through - stop right away instead of waiting out the
                    # unchanged-round limit, which is meant for "these are all already saved".
                    if card_count == 0:
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
