"""Helpers shared by the JSON-API listing fetchers (MakerWorld, Printables)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime

USER_AGENT = "WebRecordCollector/1.0"


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
