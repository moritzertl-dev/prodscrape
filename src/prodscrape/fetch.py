"""Content-addressed HTTP cache. Fetch once, replay forever.

This is the foundation of the reproducibility contract (PIPELINE.md §0): every response
is stored by sha256 alongside a metadata record, so a re-run reads from disk and never
silently re-fetches. Tests run entirely off the cache and are therefore offline and free.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlparse

import httpx

USER_AGENT = "prodscrape/0.1 (+product catalogue research; contact via site owner)"

_META_CHARSET_RE = re.compile(rb"""<meta[^>]*charset=["']?([A-Za-z0-9_-]+)""", re.I)


@dataclass(frozen=True)
class FetchRecord:
    """Provenance for one fetched URL."""

    url: str
    final_url: str
    status: int
    sha256: str
    fetched_at: str
    content_type: str
    encoding: str
    body_path: str


def _decode(raw: bytes, content_type: str) -> tuple[str, str]:
    """Decode bytes honouring the declared charset.

    Spec sheets are full of ``µ``, ``°``, ``±``, ``≤`` — decoding blind corrupts values.
    Order of authority: Content-Type header, then ``<meta charset>``, then UTF-8.
    """
    charset = None
    if "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=", 1)[1].split(";")[0].strip()
    if not charset:
        m = _META_CHARSET_RE.search(raw[:4096])
        if m:
            charset = m.group(1).decode("ascii", "ignore")
    for candidate in (charset, "utf-8", "cp1252"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate), candidate
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace"), "utf-8/replace"


class Cache:
    """On-disk content-addressed store for fetched pages."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.bodies = self.root / "bodies"
        self.index_path = self.root / "index.jsonl"
        self.bodies.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, FetchRecord] = {}
        if self.index_path.exists():
            for line in self.index_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = FetchRecord(**json.loads(line))
                    self._index[rec.url] = rec

    def get(self, url: str) -> tuple[FetchRecord, str] | None:
        rec = self._index.get(url)
        if rec is None:
            return None
        body = Path(rec.body_path)
        if not body.exists():
            return None
        return rec, body.read_text(encoding="utf-8")

    def put(self, rec: FetchRecord, text: str) -> None:
        Path(rec.body_path).write_text(text, encoding="utf-8")
        self._index[rec.url] = rec
        with self.index_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")

    def body_path_for(self, sha: str) -> Path:
        return self.bodies / f"{sha}.html"

    def __len__(self) -> int:
        return len(self._index)


class Fetcher:
    """Polite, cached HTTP client.

    Respects robots.txt and enforces a minimum delay between requests to the same host.
    """

    def __init__(self, cache: Cache, delay: float = 1.0, respect_robots: bool = True):
        self.cache = cache
        self.delay = delay
        self.respect_robots = respect_robots
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _robots_for(self, url: str) -> robotparser.RobotFileParser | None:
        host = urlparse(url).netloc
        if host not in self._robots:
            parser = robotparser.RobotFileParser()
            robots_url = f"{urlparse(url).scheme}://{host}/robots.txt"
            try:
                resp = self._client.get(robots_url)
                parser.parse(resp.text.splitlines())
            except httpx.HTTPError:
                parser = None
            self._robots[host] = parser
        return self._robots[host]

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parser = self._robots_for(url)
        return True if parser is None else parser.can_fetch(USER_AGENT, url)

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last_request.get(host)
        if last is not None:
            wait = self.delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_request[host] = time.monotonic()

    def get(self, url: str, *, refresh: bool = False) -> tuple[FetchRecord, str]:
        """Return (record, text), reading from cache unless ``refresh`` is set."""
        if not refresh:
            hit = self.cache.get(url)
            if hit is not None:
                return hit
        if not self.allowed(url):
            raise PermissionError(f"robots.txt disallows {url}")

        self._throttle(url)
        resp = self._client.get(url)
        content_type = resp.headers.get("Content-Type", "")
        text, encoding = _decode(resp.content, content_type)
        sha = hashlib.sha256(resp.content).hexdigest()
        rec = FetchRecord(
            url=url,
            final_url=str(resp.url),
            status=resp.status_code,
            sha256=sha,
            fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            content_type=content_type,
            encoding=encoding,
            body_path=str(self.cache.body_path_for(sha)),
        )
        self.cache.put(rec, text)
        return rec, text
