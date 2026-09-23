"""Content-addressed HTTP cache. Fetch once, replay forever.

This is the foundation of the reproducibility contract (PIPELINE.md §0): every response
is stored by sha256 alongside a metadata record, so a re-run reads from disk and never
silently re-fetches. Tests run entirely off the cache and are therefore offline and free.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlparse

import httpx

USER_AGENT = "prodscrape/0.1 (+product catalogue research; contact via site owner)"

# Some vendors reject any non-browser User-Agent outright — ika.com returns 403 on its
# own declared sitemap. Retrying once with a browser UA is the difference between the
# tool working and not working on those sites.
#
# robots.txt is still parsed and obeyed under both agents: this changes how we identify,
# never what we are permitted to fetch. A page the site disallows stays disallowed.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

BLOCKED_STATUSES = (401, 403, 406, 429, 503)
ARCHIVE_CDX = "https://web.archive.org/cdx/search/cdx"
ARCHIVE_WEB = "https://web.archive.org/web"
ARCHIVE_MIN_DELAY = 1.5
# Gap between two requests to the same host. 1 s made a 400-page vendor take 7+ minutes
# of pure waiting; a quarter second is still gentle, and a vendor that asks for more in
# robots.txt (Crawl-delay) gets more.
DEFAULT_DELAY = 0.25
MAX_WORKERS = 6

# Bot-protection interstitials. Some are served with status 200, and the web archive
# captures them like any other page, so status codes alone cannot identify them.
_CHALLENGE_RE = re.compile(
    r"<title>\s*(?:Just a moment\.\.\.|Attention Required! \| Cloudflare|Access Denied)\s*</title>"
    r"|cf-browser-verification|challenge-platform|_Incapsula_Resource",
    re.I,
)


def looks_like_challenge(text: str) -> bool:
    return bool(_CHALLENGE_RE.search(text))


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
    # "live", or "archive" when the vendor blocked us and a public web-archive snapshot
    # was used instead. Defaults keep cache indexes written before this field readable.
    source: str = "live"
    archived_at: str = ""


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

    _lock = threading.Lock()

    def put(self, rec: FetchRecord, text: str) -> None:
        Path(rec.body_path).write_text(text, encoding="utf-8")
        with self._lock:                 # pages are fetched from several threads
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

    def __init__(
        self,
        cache: Cache,
        delay: float = DEFAULT_DELAY,
        respect_robots: bool = True,
        archive_fallback: bool = True,
    ):
        self.cache = cache
        self.archive_fallback = archive_fallback
        self.archive_hits: list[str] = []   # URLs served from the web archive
        self.delay = delay
        self.respect_robots = respect_robots
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self.ua_fallbacks: list[str] = []   # URLs that needed the browser User-Agent
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

    _throttle_lock = threading.Lock()

    def _host_delay(self, url: str) -> float:
        host = urlparse(url).netloc
        # The web archive is a shared public service; never hit it faster than this.
        if host == "web.archive.org":
            return max(self.delay, ARCHIVE_MIN_DELAY)
        parser = self._robots.get(host)
        asked = None
        if parser is not None:
            try:
                asked = parser.crawl_delay(USER_AGENT)
            except Exception:
                asked = None
        return max(self.delay, float(asked or 0))

    def _throttle(self, url: str) -> None:
        """Reserve the next free slot for this host, then wait for it.

        Slots are reserved under a lock, so parallel fetches to one host stay spaced by
        the delay while fetches to different hosts run side by side.
        """
        host = urlparse(url).netloc
        delay = self._host_delay(url)
        with self._throttle_lock:
            now = time.monotonic()
            last = self._last_request.get(host)
            slot = now if last is None else max(now, last + delay)
            self._last_request[host] = slot
        if slot > now:
            time.sleep(slot - now)

    def prefetch(self, urls: list[str]) -> None:
        """Fetch uncached URLs in parallel (bounded), so a later ``get`` is a cache hit.

        Errors are swallowed here; the caller's own ``get`` raises them in order.
        """
        from concurrent.futures import ThreadPoolExecutor

        todo = [u for u in dict.fromkeys(urls) if self.cache.get(u) is None]
        if len(todo) < 2:
            return

        def one(u: str) -> None:
            try:
                self.get(u)
            except Exception:
                pass

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            list(pool.map(one, todo))

    def get(self, url: str, *, refresh: bool = False) -> tuple[FetchRecord, str]:
        """Return (record, text), reading from cache unless ``refresh`` is set."""
        if not refresh:
            hit = self.cache.get(url)
            # A block recorded before the archive fallback existed is retried once; one
            # recorded after it ("blocked") is final, so re-runs stay offline.
            stale_block = (
                hit is not None and self.archive_fallback
                and hit[0].source == "live"
                and (hit[0].status in BLOCKED_STATUSES or looks_like_challenge(hit[1][:4000]))
            )
            if hit is not None and not stale_block:
                return hit
        if not self.allowed(url):
            raise PermissionError(f"robots.txt disallows {url}")

        self._throttle(url)
        resp = self._client.get(url)
        if resp.status_code in (403, 406, 429):
            # Identify as a browser once, then fall back. Still inside robots.txt.
            self._throttle(url)
            retry = self._client.get(url, headers={"User-Agent": BROWSER_USER_AGENT})
            if retry.status_code < 400:
                self.ua_fallbacks.append(url)
                resp = retry
        source, archived_at = "live", ""
        # Where the vendor itself sent us (agilent.com -> www.agilent.com) before it
        # blocked us. An archived page keeps this as its location, or discovery stays on
        # a host with no robots.txt and no sitemap.
        live_final = str(resp.url)
        blocked = resp.status_code in BLOCKED_STATUSES or (
            resp.status_code == 200 and looks_like_challenge(resp.text[:4000])
        )
        if blocked and self.archive_fallback:
            archived = self._from_archive(url)
            if archived is not None:
                resp, archived_at = archived
                source = "archive"
                self.archive_hits.append(url)
            else:
                source = "blocked"

        content_type = resp.headers.get("Content-Type", "")
        text, encoding = _decode(resp.content, content_type)
        sha = hashlib.sha256(resp.content).hexdigest()
        rec = FetchRecord(
            url=url,
            # An archived page keeps the vendor URL as its identity; the archive is
            # provenance (source + archived_at), not a location to crawl onward from.
            final_url=live_final if source == "archive" else str(resp.url),
            status=resp.status_code,
            sha256=sha,
            fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            content_type=content_type,
            encoding=encoding,
            body_path=str(self.cache.body_path_for(sha)),
            source=source,
            archived_at=archived_at,
        )
        self.cache.put(rec, text)
        return rec, text

    def _from_archive(self, url: str) -> tuple[httpx.Response, str] | None:
        """The newest *successful* public web-archive capture of a blocked page.

        Bot protection (Akamai on agilent.com, a Cloudflare challenge on beckman.com)
        answers every non-browser client with 403, on every page. Defeating it is not
        something this tool does. The Internet Archive's copy of the same public page is
        a legitimate, attributable source: the record keeps ``source="archive"`` and the
        capture timestamp, and the vendor's robots.txt has already been honoured.

        "Newest" alone is not enough — the archive also captures the challenge page
        itself, so beckman.com's latest homepage snapshot is a 403. Only 200 captures
        are considered.

        Fast path first: ``/web/<today>id_/<url>`` redirects to the capture nearest to
        today in one request. The CDX index (20s+ per query) is consulted only when that
        capture is missing or is a challenge page — on a 300-page vendor this is the
        difference between minutes and hours.
        """
        try:
            self._throttle(ARCHIVE_WEB)
            today = time.strftime("%Y%m%d", time.gmtime())
            resp = self._client.get(f"{ARCHIVE_WEB}/{today}id_/{url}", timeout=60.0)
            if resp.status_code == 200 and not looks_like_challenge(resp.text[:4000]):
                m = re.search(r"/web/(\d{8})", str(resp.url))
                stamp = m.group(1) if m else today
                return resp, f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
        except httpx.HTTPError:
            pass
        try:
            cdx = None
            # The CDX index is slow (20s+ is normal), and a timeout here would record
            # the page as permanently blocked — so allow for it and retry once.
            for _ in range(2):
                self._throttle(ARCHIVE_CDX)
                try:
                    cdx = self._client.get(
                        ARCHIVE_CDX,
                        params={"url": url, "fl": "timestamp",
                                "filter": "statuscode:200", "limit": "-1"},
                        timeout=90.0,
                    )
                    break
                except httpx.TimeoutException:
                    continue
            if cdx is None or cdx.status_code != 200:
                return None
            # Newest first. The archive also stores challenge pages served with 200,
            # so a capture is only accepted once its body is known not to be one.
            for stamp in list(reversed(cdx.text.split()))[:4]:
                self._throttle(ARCHIVE_CDX)
                resp = self._client.get(f"{ARCHIVE_WEB}/{stamp}id_/{url}", timeout=90.0)
                if resp.status_code != 200 or looks_like_challenge(resp.text[:4000]):
                    continue
                return resp, f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
            return None
        except httpx.HTTPError:
            return None

    def archive_urls(self, prefix: str, limit: int = 20_000) -> list[str]:
        """URLs the web archive holds under a prefix — discovery for a blocked site.

        Used only when the vendor's own sitemap is unreachable. Returns distinct HTML
        pages captured with status 200.
        """
        try:
            self._throttle(ARCHIVE_CDX)
            resp = self._client.get(
                ARCHIVE_CDX,
                params={"url": f"{prefix}*", "fl": "original", "collapse": "urlkey",
                        "filter": ["statuscode:200", "mimetype:text/html"],
                        "limit": str(limit)},
                timeout=120.0,
            )
        except httpx.HTTPError:
            return []
        if resp.status_code != 200:
            return []
        return [line.strip() for line in resp.text.splitlines() if line.strip()]
