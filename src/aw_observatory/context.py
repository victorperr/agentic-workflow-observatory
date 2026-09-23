"""Build a RunContext from the GitHub `workflow_run` event (or aw_info.json as a fallback).

The `workflow_run` payload is the glue Datadog is missing: it names the compiled lock
file, the PR, the commit and the CI conclusion of the agentic run we are observing.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from aw_observatory.model import RunContext


def parse_time(value: Any) -> float | None:
    """Epoch seconds from ISO-8601 strings or epoch numbers in s / ms / ns."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e17:
            return v / 1e9
        if v > 1e11:
            return v / 1e3
        return v
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        try:
            return parse_time(float(value))
        except ValueError:
            return None


def source_markdown_path(lock_path: str) -> str:
    """`.github/workflows/foo.lock.yml` -> `.github/workflows/foo.md` (gh-aw compile convention)."""
    if lock_path.endswith(".lock.yml"):
        return lock_path[: -len(".lock.yml")] + ".md"
    return ""


def from_workflow_run_event(event: dict) -> RunContext:
    run = event.get("workflow_run") or {}
    if not run:
        raise ValueError("event payload has no 'workflow_run' object")
    repo = (run.get("repository") or event.get("repository") or {}).get("full_name", "unknown/unknown")
    prs = run.get("pull_requests") or []
    lock_path = run.get("path", "")
    return RunContext(
        repository=repo,
        run_id=int(run["id"]),
        run_attempt=int(run.get("run_attempt") or 1),
        workflow_name=run.get("name") or "unknown",
        workflow_path=lock_path,
        source_path=source_markdown_path(lock_path),
        event=run.get("event", ""),
        head_branch=run.get("head_branch") or "",
        head_sha=run.get("head_sha") or "",
        actor=((run.get("triggering_actor") or run.get("actor")) or {}).get("login", ""),
        conclusion=run.get("conclusion") or "unknown",
        pull_request=int(prs[0]["number"]) if prs else None,
        html_url=run.get("html_url", ""),
        started_at=parse_time(run.get("run_started_at") or run.get("created_at")),
        ended_at=parse_time(run.get("updated_at")),
    )


def from_aw_info(aw_info: dict, repository: str | None = None, conclusion: str = "unknown") -> RunContext:
    """Fallback for local backfills (`gh aw logs` / `gh aw audit` directories) with no event file."""
    repo = repository or aw_info.get("repository") or "unknown/unknown"
    lock_path = aw_info.get("workflow_path") or aw_info.get("workflow_file") or ""
    run_id = aw_info.get("run_id") or 0
    return RunContext(
        repository=repo,
        run_id=int(run_id),
        run_attempt=int(aw_info.get("run_attempt") or 1),
        workflow_name=aw_info.get("workflow_name") or "unknown",
        workflow_path=lock_path,
        source_path=source_markdown_path(lock_path),
        event=aw_info.get("event_name", ""),
        head_branch=aw_info.get("ref", "").removeprefix("refs/heads/"),
        head_sha=aw_info.get("sha", ""),
        actor=aw_info.get("actor", ""),
        conclusion=conclusion,
        started_at=parse_time(aw_info.get("created_at")),
        html_url=f"https://github.com/{repo}/actions/runs/{run_id}" if run_id else "",
    )


def load_context(event_path: str | None, aw_info: dict, repository: str | None = None) -> RunContext:
    if event_path and Path(event_path).is_file():
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        if "workflow_run" in event:
            return from_workflow_run_event(event)
    return from_aw_info(aw_info, repository)
