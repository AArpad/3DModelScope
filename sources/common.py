"""Helpers shared across the listing fetchers (MakerWorld, Printables, Thingiverse)."""

from __future__ import annotations

import json
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# A plain, self-identifying UA ("WebRecordCollector/1.0") worked fine until MakerWorld
# started answering it with a flat 403 Forbidden (Printables never minded it, but there's no
# reason to keep tempting the same fate there) - a generic desktop Chrome UA string is what
# an actual browser hitting these same JSON endpoints would send, and both APIs accept it.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


def urlopen_json(request: urllib.request.Request, retries: int = 3) -> dict:
    """Fetches and JSON-decodes one response, retrying a couple of times on HTTP 429 (rate
    limited) with a backoff - MakerWorld's and Printables' search APIs can both throttle a scan
    that pages through many requests in a row for a large `hours` window."""
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt < retries:
                try:
                    wait_seconds = float(error.headers.get("Retry-After", ""))
                except ValueError:
                    wait_seconds = 2.0 * (attempt + 1)
                time.sleep(min(wait_seconds, 30))
                continue
            raise
    raise AssertionError("unreachable")


def parse_iso_datetime(value: str | None) -> datetime | None:
    """Parses an ISO-8601 timestamp as used by both MakerWorld's ("...Z") and Printables'
    ("...+00:00") search APIs into an aware datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def ensure_profile_dir(new_dir: Path, legacy_dir: Path | None = None) -> Path:
    """Resolves the persistent browser profile directory to actually launch with, for the
    two sources (MakerWorld, Thingiverse) that need a real browser to clear a Cloudflare
    challenge and want to keep that clearance cookie between runs.

    Prefers new_dir. If it doesn't exist yet but an older build's profile is sitting at
    legacy_dir, copies it over first so an already-solved challenge isn't lost on upgrade.
    legacy_dir is left in place (copied, not moved) so downgrading to an older build still
    finds its cookies there too. If neither exists (or there's no legacy_dir to check at
    all), new_dir is created fresh and the browser starts a brand new profile in it.
    """
    if new_dir.exists() and any(new_dir.iterdir()):
        return new_dir
    if legacy_dir is not None and legacy_dir.exists() and any(legacy_dir.iterdir()):
        new_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(legacy_dir, new_dir, dirs_exist_ok=True)
        return new_dir
    new_dir.mkdir(parents=True, exist_ok=True)
    return new_dir


def minimize_browser_window(context, page) -> None:
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
