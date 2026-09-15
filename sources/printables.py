"""Printables listing fetcher.

Uses Printables' own GraphQL search API directly - like MakerWorld's search API, this endpoint
is plain, unauthenticated JSON with no Cloudflare challenge at all (unlike the printables.com
website itself), so no browser is needed here either. Stops as soon as a result's own
firstPublish falls outside the requested `hours` window, the same way makerworld.fetch_listing
does.
"""

from __future__ import annotations

import json
import math
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

from .common import USER_AGENT, parse_iso_datetime, urlopen_json

_MODEL_LIST_QUERY = """\
query ModelList($limit: Int!, $cursor: String, $ordering: String, $publishedDateLimitDays: Int) {
  models: morePrints(limit: $limit, cursor: $cursor, ordering: $ordering, publishedDateLimitDays: $publishedDateLimitDays) {
    cursor
    items {
      id
      name
      slug
      firstPublish
      image {
        filePath
        __typename
      }
      __typename
    }
    __typename
  }
}"""


def fetch_listing(
    url: str,
    hours: int,
    max_count: int | None = None,
    progress_callback: Callable[[int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> list[tuple[str, str, str, str]]:
    """Fetches Printables' newest-models results.

    publishedDateLimitDays (whole days, +1 day safety margin) narrows the query server-side
    first; the precise hour-level cutoff is still enforced client-side against firstPublish.
    `url` is accepted for interface consistency with the other listing fetchers (and in case
    per-category filtering is added later) but unused today - every call queries the same
    "all categories, newest" listing.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    since_days = max(1, math.ceil(hours / 24) + 1)
    models: dict[str, tuple[str, str, str, str]] = {}
    cursor: str | None = None
    while True:
        if cancel_check and cancel_check():
            break
        variables = {
            # 36 is the actual site's own page size - anything above it comes back with an
            # empty items list (no error, just silently nothing), rather than being clamped.
            "limit": 36,
            "cursor": cursor,
            "ordering": "-first_publish",
            "publishedDateLimitDays": since_days,
        }
        body = json.dumps(
            {"operationName": "ModelList", "query": _MODEL_LIST_QUERY, "variables": variables}
        ).encode("utf-8")
        request = urllib.request.Request(
            "https://api.printables.com/graphql/",
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        try:
            data = urlopen_json(request)
        except (OSError, ValueError) as error:
            raise ValueError(f"A Printables lista nem töltődött be: {error}") from error
        if "errors" in data:
            raise ValueError(f"A Printables API hibát adott vissza: {data['errors']}")
        result = (data.get("data") or {}).get("models") or {}
        items = result.get("items") or []
        if not items:
            break
        reached_cutoff = False
        for item in items:
            created_at = parse_iso_datetime(item.get("firstPublish"))
            if created_at is None or created_at < cutoff:
                reached_cutoff = True
                break
            model_id = item.get("id")
            if not model_id:
                continue
            slug = item.get("slug") or ""
            model_url = f"https://www.printables.com/model/{model_id}-{slug}" if slug else f"https://www.printables.com/model/{model_id}"
            if model_url in models:
                continue
            title = item.get("name") or ""
            file_path = (item.get("image") or {}).get("filePath") or ""
            image_url = f"https://media.printables.com/{file_path}" if file_path else ""
            model_created_at = created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            models[model_url] = (model_url, title, image_url, model_created_at)
            if progress_callback:
                progress_callback(len(models))
            if max_count and len(models) >= max_count:
                return list(models.values())
        if reached_cutoff:
            break
        cursor = result.get("cursor")
        if not cursor:
            break
    return list(models.values())
