from __future__ import annotations

import re
from typing import Optional


def normalize_ingest_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if not re.match(r"^https?://", cleaned, flags=re.IGNORECASE):
        return None
    return cleaned


def normalize_org_source_urls(values: Optional[list[str]]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        url = normalize_ingest_url(raw)
        if not url or url in seen:
            continue
        seen.add(url)
        normalized.append(url)
    return normalized


def clean_ingest_tags(tags: Optional[list[str]], city: Optional[str] = None) -> list[str]:
    cleaned: list[str] = []
    for tag in tags or []:
        item = str(tag or "").strip()
        if item:
            cleaned.append(item)
    if city:
        cleaned.append(f"city:{city.strip().lower()}")
    return sorted(set(cleaned))


def derive_org_name(source_url: Optional[str], fallback: Optional[str] = None) -> str:
    preferred = str(fallback or "").strip()
    if preferred:
        return preferred
    source = normalize_ingest_url(source_url)
    if source:
        host = source.split("://", 1)[1].split("/", 1)[0]
        host = host.replace("www.", "")
        return host
    return "Organization"


def city_from_feed_url(feed_url: str) -> Optional[str]:
    cleaned = feed_url.split("://", 1)[-1]
    path = "/" + cleaned.split("/", 1)[1] if "/" in cleaned else ""
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) >= 2 and segments[-1].lower() == "upcoming_events.json":
        return segments[-2].strip().lower() or None
    return None
