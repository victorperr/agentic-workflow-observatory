"""Parse the artifacts a gh-aw run uploads into a RunTelemetry.

Works on the directory produced by `gh run download <run-id>` and on the
`logs/run-<id>/` directories produced by `gh aw logs` / `gh aw audit`.
The artifact layout changes between gh-aw releases, so every parser searches
by file name anywhere under the run directory and tolerates missing files.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

from aw_observatory.context import parse_time
from aw_observatory.model import FirewallRequest, LlmCall, RunTelemetry, SafeOutput, ToolCall
from aw_observatory.pricing import estimate_aic

_FRONTMATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)

# Plain-text signals in agent-stdio.log that a run was cut short rather than finished.
_MARKERS = {
    "budget_exhausted": re.compile(
        r"\b(ai[- _]?credits?|aic|budget)\b.{0,60}(exceed|exhaust|reached|limit hit)", re.I
    ),
    "max_turns_reached": re.compile(r"max[- _]?turns?.{0,40}(reached|exceed|limit)", re.I),
    "timeout": re.compile(r"(timed out|timeout exceeded|exceeded the maximum execution time)", re.I),
}


# --------------------------------------------------------------------------- helpers


def _files(run_dir: Path, *names: str) -> list[Path]:
    wanted = set(names)
    return sorted(p for p in run_dir.rglob("*") if p.is_file() and p.name in wanted)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None


def _json_lines(path: Path) -> Iterator[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def _first(d: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        cur: Any = d
        for part in key.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, ""):
            return cur
    return default


# --------------------------------------------------------------------------- parsers


def parse_token_usage(run_dir: Path) -> list[LlmCall]:
    """firewall-audit-logs/api-proxy-logs/token-usage.jsonl: one line per model API call."""
    calls: list[LlmCall] = []
    for path in _files(run_dir, "token-usage.jsonl"):
        for rec in _json_lines(path):
            aic = _first(rec, "aic", "ai_credits", "usage.aic")
            calls.append(
                LlmCall(
                    model=str(_first(rec, "model", "model_name", "usage.model", default="unknown")),
                    input_tokens=int(_first(rec, "input_tokens", "prompt_tokens", "usage.input_tokens", default=0)),
                    output_tokens=int(
                        _first(rec, "output_tokens", "completion_tokens", "usage.output_tokens", default=0)
                    ),
                    cache_read_tokens=int(
                        _first(rec, "cache_read_tokens", "cache_read_input_tokens", "usage.cache_read_input_tokens", default=0)
                    ),
                    cache_write_tokens=int(
                        _first(rec, "cache_write_tokens", "cache_creation_input_tokens", "usage.cache_creation_input_tokens", default=0)
                    ),
                    aic=float(aic) if aic is not None else None,
                    timestamp=parse_time(_first(rec, "timestamp", "ts", "time", "start_time")),
                    duration_ms=_first(rec, "duration_ms", "latency_ms"),
                )
            )
    if calls:
        return calls
    # Older runs only have aggregate totals.
    for path in _files(run_dir, "agent_usage.json"):
        rec = _read_json(path) or {}
        rec = rec.get("agent_usage", rec) if isinstance(rec, dict) else {}
        if rec:
            aic = _first(rec, "aic", "ai_credits")
            calls.append(
                LlmCall(
                    model=str(rec.get("model", "unknown")),
                    input_tokens=int(rec.get("input_tokens", 0)),
                    output_tokens=int(rec.get("output_tokens", 0)),
                    cache_read_tokens=int(rec.get("cache_read_tokens", 0)),
                    cache_write_tokens=int(rec.get("cache_write_tokens", 0)),
                    aic=float(aic) if aic is not None else None,
                )
            )
    return calls


def parse_firewall(run_dir: Path) -> list[FirewallRequest]:
    """AWF (Squid) access log: `ts client domain:port dest:port proto method status decision url ua`."""
    paths = [
        p
        for p in run_dir.rglob("*.log")
        if p.is_file() and ("squid-logs" in p.parts or p.name == "access.log" or "firewall" in p.name)
    ]
    requests: list[FirewallRequest] = []
    for path in sorted(set(paths)):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            fields = line.split(maxsplit=9)
            if len(fields) < 8:
                continue
            try:
                ts = float(fields[0])
            except ValueError:
                continue
            domain = fields[2].rsplit(":", 1)[0] if fields[2] != "-" else fields[8].split("/")[0].rsplit(":", 1)[0]
            status = int(fields[6]) if fields[6].isdigit() else None
            decision = fields[7]
            if status in (403, 407) or "TCP_DENIED" in decision or "NONE_NONE" in decision:
                allowed = False
            elif status in (200, 206, 304) or any(k in decision for k in ("TCP_TUNNEL", "TCP_HIT", "TCP_MISS")):
                allowed = True
            else:
                allowed = False  # gh-aw classifies ambiguous entries as denied
            requests.append(FirewallRequest(domain=domain, allowed=allowed, status=status, timestamp=ts))
    return requests


def _tool_calls_from_record(rec: dict) -> Iterator[ToolCall]:
    ts = parse_time(_first(rec, "timestamp", "ts", "time"))
    # MCP JSON-RPC request (MCP gateway logs), possibly wrapped by the logger.
    for msg in (rec, rec.get("payload"), rec.get("message"), rec.get("request")):
        if isinstance(msg, dict) and msg.get("method") == "tools/call":
            params = msg.get("params") or {}
            yield ToolCall(name=str(params.get("name", "unknown")), arguments=params.get("arguments"), timestamp=ts,
                           call_id=str(msg.get("id")) if msg.get("id") is not None else None)
            return
    # Claude stream-json: {"type":"assistant","message":{"content":[{"type":"tool_use",...}]}}
    message = rec.get("message")
    if rec.get("type") == "assistant" and isinstance(message, dict):
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield ToolCall(name=str(block.get("name", "unknown")), arguments=block.get("input"), timestamp=ts,
                               call_id=block.get("id"))
        return
    # Generic engine event shapes.
    if rec.get("type") in ("tool_call", "tool.call", "tool_use", "tool.execution_start"):
        name = _first(rec, "name", "tool_name", "toolName", "tool")
        if name:
            yield ToolCall(name=str(name), arguments=_first(rec, "arguments", "input", "args"), timestamp=ts,
                           call_id=_first(rec, "id", "call_id", "toolCallId"))


def _tool_errors_from_record(rec: dict) -> Iterator[str]:
    message = rec.get("message")
    if rec.get("type") == "user" and isinstance(message, dict):
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                yield str(block.get("tool_use_id"))
    if rec.get("error") and _first(rec, "id", "call_id", "toolCallId"):
        yield str(_first(rec, "id", "call_id", "toolCallId"))


def parse_tool_calls(run_dir: Path) -> list[ToolCall]:
    """Prefer the engine transcript (sees every tool); fall back to MCP gateway RPC logs."""
    sources = [_files(run_dir, "agent-stdio.log")]
    sources.append(sorted(p for p in run_dir.rglob("*.jsonl") if "mcp" in str(p.relative_to(run_dir)).lower()))
    for paths in sources:
        calls: list[ToolCall] = []
        errored: set[str] = set()
        for path in paths:
            for rec in _json_lines(path):
                calls.extend(_tool_calls_from_record(rec))
                errored.update(_tool_errors_from_record(rec))
        if calls:
            for call in calls:
                call.error = call.call_id is not None and call.call_id in errored
            return calls
    return []


def parse_safe_outputs(run_dir: Path) -> tuple[list[SafeOutput], list[str]]:
    """agent_output.json ({"items": [...], "errors": [...]}) or safe_output(s).jsonl."""
    items: list[dict] = []
    errors: list[str] = []
    for path in _files(run_dir, "agent_output.json"):
        data = _read_json(path)
        if isinstance(data, dict):
            items.extend(i for i in data.get("items", []) if isinstance(i, dict))
            errors.extend(str(e) for e in data.get("errors", []) or [])
        elif isinstance(data, list):
            items.extend(i for i in data if isinstance(i, dict))
    if not items:
        for path in _files(run_dir, "safe_output.jsonl", "safe_outputs.jsonl"):
            items.extend(_json_lines(path))
    outputs = []
    for item in items:
        kind = str(item.get("type", "unknown")).replace("-", "_")
        body = "\n\n".join(str(item[k]) for k in ("title", "body", "message", "reason") if item.get(k))
        outputs.append(SafeOutput(type=kind, body=body, raw=item))
    return outputs, errors


def parse_markers(run_dir: Path) -> set[str]:
    found: set[str] = set()
    for path in _files(run_dir, "agent-stdio.log"):
        text = path.read_text(encoding="utf-8", errors="replace")
        found.update(name for name, pattern in _MARKERS.items() if pattern.search(text))
    return found


def load_instructions(run_dir: Path, repo_root: Path | None, source_path: str) -> str:
    """The Markdown body of the source workflow (frontmatter stripped) or the rendered prompt."""
    candidates = []
    if repo_root and source_path:
        candidates.append(repo_root / source_path)
    candidates += _files(run_dir, "prompt.txt", "prompt.md")
    for path in candidates:
        if path.is_file():
            return _FRONTMATTER.sub("", path.read_text(encoding="utf-8", errors="replace")).strip()
    return ""


def load_aw_info(run_dir: Path) -> dict:
    for path in _files(run_dir, "aw_info.json"):
        data = _read_json(path)
        if isinstance(data, dict):
            return data
    return {}


def load_run(run_dir: Path, repo_root: Path | None = None, source_path: str = "") -> RunTelemetry:
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run directory not found: {run_dir}")
    aw_info = load_aw_info(run_dir)
    llm_calls = parse_token_usage(run_dir)
    safe_outputs, output_errors = parse_safe_outputs(run_dir)
    detection = None
    for path in _files(run_dir, "detection_result.json"):
        detection = _read_json(path)
    telemetry = RunTelemetry(
        engine=str(aw_info.get("engine_id") or aw_info.get("engine") or "unknown"),
        model=str(aw_info.get("model") or (llm_calls[0].model if llm_calls else "unknown")),
        llm_calls=llm_calls,
        tool_calls=parse_tool_calls(run_dir),
        firewall=parse_firewall(run_dir),
        safe_outputs=safe_outputs,
        output_errors=output_errors,
        detection=detection if isinstance(detection, dict) else None,
        markers=parse_markers(run_dir),
        instructions=load_instructions(run_dir, repo_root, source_path),
        files_seen=sorted(str(p.relative_to(run_dir)) for p in run_dir.rglob("*") if p.is_file()),
    )
    # The AWF proxy reports AI Credits per call; estimate from list prices when it doesn't.
    for call in telemetry.llm_calls:
        if call.aic is None:
            call.aic = estimate_aic(call)
            telemetry.aic_estimated = True
    return telemetry
