"""On-disk cache for the static GTFS zip (internal).

Layout under the cache dir:
    caltrain-gtfs.zip    the feed, replaced atomically
    caltrain-gtfs.json   metadata: url, downloaded_at, checked_at, etag, last_modified

Downloads are conditional (If-None-Match / If-Modified-Since), and a new zip
replaces the cached one only after it parses successfully, so a bad download
never clobbers a good cache.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TypeVar

import httpx

from . import __version__
from .errors import ScheduleUnavailableError

log = logging.getLogger(__name__)

ZIP_NAME = "caltrain-gtfs.zip"
META_NAME = "caltrain-gtfs.json"
USER_AGENT = f"caltrain-api/{__version__}"

T = TypeVar("T")


@dataclass
class FeedMeta:
    url: str
    downloaded_at: datetime
    checked_at: datetime
    etag: str | None = None
    last_modified: str | None = None

    def to_json(self) -> str:
        return json.dumps({
            "url": self.url,
            "downloaded_at": self.downloaded_at.isoformat(),
            "checked_at": self.checked_at.isoformat(),
            "etag": self.etag,
            "last_modified": self.last_modified,
        }, indent=2)

    @classmethod
    def from_json(cls, text: str) -> FeedMeta:
        d = json.loads(text)
        return cls(url=d["url"], downloaded_at=datetime.fromisoformat(d["downloaded_at"]),
                   checked_at=datetime.fromisoformat(d["checked_at"]),
                   etag=d.get("etag"), last_modified=d.get("last_modified"))


class FeedCache:
    def __init__(
        self,
        cache_dir: Path,
        url: str,
        max_age: timedelta,
        http_client: httpx.Client | None,
        clock: Callable[[], datetime],
    ):
        self.cache_dir = cache_dir
        self.url = url
        self.max_age = max_age
        self.http_client = http_client
        self.clock = clock

    @property
    def zip_path(self) -> Path:
        return self.cache_dir / ZIP_NAME

    @property
    def meta_path(self) -> Path:
        return self.cache_dir / META_NAME

    def read_meta(self) -> FeedMeta | None:
        try:
            return FeedMeta.from_json(self.meta_path.read_text())
        except (OSError, ValueError, KeyError) as exc:
            if self.meta_path.exists():
                log.warning("Ignoring unreadable GTFS cache metadata %s: %s", self.meta_path, exc)
            return None

    def is_stale(self, valid_until: date | None) -> bool:
        """True if the cache should be re-checked against the server."""
        meta = self.read_meta()
        now = self.clock()
        if meta is None or not self.zip_path.exists():
            return True
        if meta.url != self.url:
            log.info("GTFS URL changed; cache is stale")
            return True
        if now - meta.checked_at > self.max_age:
            return True
        return valid_until is not None and now.date() > valid_until

    def _write_atomic(self, path: Path, data: bytes) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.cache_dir, prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def download(self, parse: Callable[[bytes], T], *, conditional: bool) -> T | None:
        """Fetch the feed. Returns ``parse(data)`` for new content, or None if
        the server says the cached copy is current (HTTP 304).

        The data is parsed *before* it replaces the cache.

        Raises:
            ScheduleUnavailableError: network/HTTP failure or unparseable feed.
        """
        meta = self.read_meta()
        headers = {"User-Agent": USER_AGENT}
        if conditional and meta is not None and self.zip_path.exists() and meta.url == self.url:
            if meta.etag:
                headers["If-None-Match"] = meta.etag
            if meta.last_modified:
                headers["If-Modified-Since"] = meta.last_modified

        log.info("Downloading GTFS from %s", self.url)
        client = self.http_client or httpx.Client(timeout=60, follow_redirects=True)
        try:
            resp = client.get(self.url, headers=headers)
        except httpx.HTTPError as exc:
            raise ScheduleUnavailableError(f"GTFS download failed: {type(exc).__name__}: {exc}") from exc
        finally:
            if self.http_client is None:
                client.close()

        now = self.clock()
        if resp.status_code == 304 and meta is not None:
            log.info("GTFS unchanged on server (304)")
            meta.checked_at = now
            self._write_atomic(self.meta_path, meta.to_json().encode())
            return None
        if resp.status_code != 200:
            raise ScheduleUnavailableError(f"GTFS download failed: HTTP {resp.status_code}")

        try:
            parsed = parse(resp.content)
        except Exception as exc:
            raise ScheduleUnavailableError(f"downloaded GTFS is unusable: {exc}") from exc

        self._write_atomic(self.zip_path, resp.content)
        new_meta = FeedMeta(url=self.url, downloaded_at=now, checked_at=now,
                            etag=resp.headers.get("ETag"), last_modified=resp.headers.get("Last-Modified"))
        self._write_atomic(self.meta_path, new_meta.to_json().encode())
        log.info("Cached GTFS (%d bytes) at %s", len(resp.content), self.zip_path)
        return parsed
