"""MakerWorld listing fetcher.

Uses MakerWorld's own search JSON API directly - no browser needed at all, this endpoint is
plain, unauthenticated JSON - and stops as soon as a result's own createTime falls outside the
requested `hours` window.
"""

from __future__ import annotations

import math
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

from .common import USER_AGENT, parse_iso_datetime, urlopen_json


def _extract_category(url: str) -> str:
    """Pulls a category slug (e.g. "900-3d-printer") out of a configured MakerWorld list URL
    like .../3d-models/900-3d-printer, if the user pointed the source at one category instead
    of the full "all models" listing. Empty string means "no category filter"."""
    match = re.search(r"/3d-models/([\w-]+)", url)
    return match.group(1) if match else ""


def fetch_listing(
    url: str,
    hours: int,
    max_count: int | None = None,
    progress_callback: Callable[[int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> list[tuple[str, str, str, str]]:
    """Fetches MakerWorld's newest-models results.

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
        request = urllib.request.Request(
            f"https://makerworld.com/api/v1/search-service/select/design2?{query}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        try:
            data = urlopen_json(request)
        except (OSError, ValueError) as error:
            raise ValueError("A MakerWorld lista nem töltődött be időben.") from error
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
            # Stored alongside the record so the actual reason a scan stopped where it did
            # stays visible later, not just implied by "created_at" (when *we* saved it).
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
    return list(models.values())
