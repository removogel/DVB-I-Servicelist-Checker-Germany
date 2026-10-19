"""Laden der Service List aus Datei oder URL."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .httpclient import HttpClient, HttpResult, add_query


@dataclass
class Source:
    location: str
    is_url: bool
    data: bytes
    request_url: str | None = None
    http: HttpResult | None = None
    meta: dict = field(default_factory=dict)


def is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def load(location: str, client: HttpClient, postcode: str | None = None) -> Source:
    if is_url(location):
        url = add_query(location, postcode=postcode) if postcode else location
        r = client.get(url, use_cache=False)
        if not r.ok:
            raise RuntimeError(f"Service List nicht abrufbar ({url}): {r.describe()}")
        meta = {
            "status": r.status,
            "content_type": r.headers.get("content-type", ""),
            "bytes": len(r.content),
            "elapsed_s": round(r.elapsed, 2),
            "last_modified": r.headers.get("last-modified"),
            "cache_control": r.headers.get("cache-control"),
            "etag": r.headers.get("etag"),
        }
        return Source(location, True, r.content, url, r, meta)
    p = Path(location).expanduser()
    data = p.read_bytes()
    return Source(str(p), False, data, None, None, {"bytes": len(data)})
