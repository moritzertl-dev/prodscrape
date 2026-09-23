"""Background runs — because an MCP tool call must answer within ~60 seconds.

Claude Desktop cuts a tool call off after about a minute. A vendor run takes several
minutes, so a blocking ``catalogue_vendor`` could never finish there: on LiCONiC the
user had to hand-write a recipe and split the scan into pieces to get through.

The run is therefore a separate process (``prodscrape run``), started detached so it
outlives the tool call and even a restart of the MCP server. The tools only start it,
and read two files it writes:

``runs/<domain>/progress.json``   stage, detail, heartbeat — rewritten as it goes
``runs/<domain>/job.json``        pid, start time, arguments, and the log path

A job is *done* when ``catalogue_manifest.json`` is newer than the job's start, *failed*
when its log ends in a traceback, and *stalled* when the heartbeat is older than
``STALL_AFTER_S`` — each a state the tools report, never guess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .paths import home, runs_dir

STALL_AFTER_S = 600
WAIT_S = 40          # how long one tool call waits for news; stays under the 60 s limit


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Progress:
    """Writes the heartbeat the status tool reads. Cheap enough to call per page."""

    def __init__(self, run_dir: Path):
        self.path = Path(run_dir) / "progress.json"
        self._last = 0.0

    def __call__(self, stage: str, detail: str = "", *, force: bool = True) -> None:
        now = time.time()
        if not force and now - self._last < 2:
            return
        self._last = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"stage": stage, "detail": detail, "updated_at": now,
                                   "updated": _now()}), encoding="utf-8")
        os.replace(tmp, self.path)


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status(domain: str) -> dict:
    """``idle`` | ``running`` | ``done`` | ``failed`` | ``stalled`` with evidence."""
    out = runs_dir() / domain
    job = _read(out / "job.json")
    if job is None:
        return {"state": "idle"}
    progress = _read(out / "progress.json") or {}
    manifest = out / "catalogue_manifest.json"
    if manifest.exists() and manifest.stat().st_mtime >= job["started_at"]:
        return {"state": "done", "job": job, "result": _read(manifest)}
    log = Path(job["log"])
    tail = log.read_text(encoding="utf-8", errors="replace")[-2000:] if log.exists() else ""
    if "Traceback (most recent call last)" in tail:
        return {"state": "failed", "job": job, "log_tail": tail[-800:]}
    beat = progress.get("updated_at", job["started_at"])
    if time.time() - beat > STALL_AFTER_S:
        return {"state": "stalled", "job": job, "progress": progress,
                "log_tail": tail[-800:]}
    return {"state": "running", "job": job, "progress": progress,
            "elapsed_s": round(time.time() - job["started_at"])}


def start(domain: str, *, manufacturer: str | None, llm: str | None, budget_usd: float,
          limit: int | None) -> dict:
    """Start ``prodscrape run`` detached, unless one is already running for the domain."""
    current = status(domain)
    if current["state"] == "running":
        return current
    out = runs_dir() / domain
    out.mkdir(parents=True, exist_ok=True)
    log = out / "job.log"
    cmd = [sys.executable, "-m", "prodscrape.cli", "run", domain,
           "--budget", str(budget_usd)]
    if manufacturer:
        cmd += ["--manufacturer", manufacturer]
    if llm:
        cmd += ["--llm", llm]
    if limit:
        cmd += ["--limit", str(limit)]
    env = dict(os.environ, PRODSCRAPE_HOME=str(home()), PYTHONIOENCODING="utf-8")
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    started = time.time()
    with log.open("w", encoding="utf-8") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, **kwargs)
    job = {"pid": proc.pid, "started_at": started, "started": _now(), "cmd": cmd[3:],
           "log": str(log)}
    (out / "job.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
    Progress(out)("starting", "background run launched")
    return {"state": "running", "job": job, "progress": {"stage": "starting"},
            "elapsed_s": 0}


def wait(domain: str, seconds: float = WAIT_S) -> dict:
    """Poll until the job leaves ``running`` or the time is up."""
    deadline = time.time() + seconds
    while True:
        current = status(domain)
        if current["state"] != "running" or time.time() >= deadline:
            return current
        time.sleep(2)
