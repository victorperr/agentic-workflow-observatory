"""Ship a Report to Datadog: LLM Observability spans + evaluations, and metrics.

Uses the public HTTP intake APIs, so no Datadog Agent is needed on the runner:
  - LLM Obs spans:        POST https://api.<site>/api/intake/llm-obs/v1/trace/spans
  - LLM Obs evaluations:  POST https://api.<site>/api/intake/llm-obs/v2/eval-metric
  - Metrics:              POST https://api.<site>/api/v2/series
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from aw_observatory.model import Report

log = logging.getLogger(__name__)

NS = 1_000_000_000
METRIC_COUNT, METRIC_GAUGE = 1, 3


# --------------------------------------------------------------------------- identity & tags


def trace_id(report: Report) -> str:
    c = report.context
    return hashlib.sha256(f"{c.repository}/{c.run_id}/{c.run_attempt}".encode()).hexdigest()[:32]


def span_id(trace: str, name: str) -> str:
    return str(int(hashlib.sha256(f"{trace}/{name}".encode()).hexdigest()[:15], 16))


def model_provider(model: str) -> str:
    m = model.lower()
    if "claude" in m:
        return "anthropic"
    if m.startswith(("gpt", "o1", "o3", "o4", "codex")):
        return "openai"
    if "gemini" in m:
        return "google"
    return "custom"


def run_tags(report: Report) -> list[str]:
    """High-cardinality tags for traces: everything needed to get from a trace back to GitHub."""
    c, t = report.context, report.telemetry
    tags = [
        f"gh.repo:{c.repository}",
        f"gh.workflow:{c.workflow_name}",
        f"gh.workflow_source:{c.source_path or c.workflow_path}",
        f"gh.run_id:{c.run_id}",
        f"gh.run_attempt:{c.run_attempt}",
        f"gh.event:{c.event}",
        f"gh.branch:{c.head_branch}",
        f"gh.sha:{c.head_sha[:12]}",
        f"gh.actor:{c.actor}",
        f"ci.conclusion:{c.conclusion}",
        f"aw.engine:{t.engine}",
        f"aw.model:{t.model}",
        f"aw.verdict:{report.verdict}",
    ]
    if c.pull_request:
        tags.append(f"gh.pr:{c.pull_request}")
    tags += sorted({f"aw.finding:{f.code}" for f in report.findings})
    return [tag for tag in tags if not tag.endswith(":")]


def metric_tags(report: Report) -> list[str]:
    """Low-cardinality tags only (no run id / PR / sha) to keep custom-metric costs bounded."""
    c, t = report.context, report.telemetry
    return [
        f"repo:{c.repository}",
        f"workflow:{c.workflow_name}",
        f"engine:{t.engine}",
        f"model:{t.model}",
        f"ci_conclusion:{c.conclusion}",
        f"verdict:{report.verdict}",
    ]


# --------------------------------------------------------------------------- payload builders


def _window(report: Report) -> tuple[float, float]:
    c, t = report.context, report.telemetry
    stamps = [x.timestamp for x in (*t.llm_calls, *t.tool_calls, *t.firewall) if x.timestamp]
    start = c.started_at or (min(stamps) if stamps else time.time() - 60)
    end = c.ended_at or (max(stamps) if stamps else start + 60)
    return start, max(end, start + 1)


def _span(trace: str, sid: str, parent: str, name: str, kind: str, start: float, duration_s: float,
          *, status: str = "ok", input_value: str = "", output_value: str = "", extra_meta: dict | None = None,
          metrics: dict | None = None, tags: list[str] | None = None) -> dict:
    meta = {"kind": kind, "input": {"value": input_value}, "output": {"value": output_value}}
    meta.update(extra_meta or {})
    span = {
        "trace_id": trace,
        "span_id": sid,
        "parent_id": parent,
        "name": name,
        "start_ns": int(start * NS),
        "duration": float(max(duration_s, 0.001) * NS),
        "status": status,
        "meta": meta,
        "metrics": metrics or {},
    }
    if tags:
        span["tags"] = tags
    return span


def _outputs_summary(report: Report) -> str:
    outs = report.telemetry.safe_outputs
    if not outs:
        return "(no safe outputs)"
    return "\n\n---\n\n".join(f"[{o.type}]\n{o.body}".strip() for o in outs)[:20_000]


def build_spans(report: Report, ml_app: str) -> dict:
    """One trace per run: agent root span with llm / tool / firewall / safe-output children."""
    c, t = report.context, report.telemetry
    trace = trace_id(report)
    root = span_id(trace, "root")
    start, end = _window(report)
    tags = run_tags(report)
    failed = report.verdict in ("failed", "silent_failure")

    spans = [_span(
        trace, root, "undefined", c.workflow_name, "agent", start, end - start,
        status="error" if failed else "ok",
        input_value=(t.instructions or f"{c.event} on {c.repository}")[:20_000],
        output_value=_outputs_summary(report),
        extra_meta={"metadata": {
            "github_run_url": c.html_url,
            "workflow_source": c.source_path,
            "pull_request": c.pull_request,
            "verdict": report.verdict,
            "aic": t.aic,
            "aic_estimated": t.aic_estimated,
            "budget_utilization": report.budget_utilization,
            "findings": [{"code": f.code, "severity": f.severity, "message": f.message} for f in report.findings],
        }},
        metrics={
            "input_tokens": t.input_tokens,
            "output_tokens": t.output_tokens,
            "total_tokens": t.input_tokens + t.output_tokens,
            "cache_read_input_tokens": t.cache_read_tokens,
        },
        tags=tags,
    )]

    # Children without timestamps are laid out sequentially across the run window.
    children: list[tuple[str, object]] = [("llm", x) for x in t.llm_calls] + [("tool", x) for x in t.tool_calls]
    slot = (end - start) / max(len(children), 1)
    for i, (kind, item) in enumerate(children):
        ts = getattr(item, "timestamp", None) or start + i * slot
        sid = span_id(trace, f"{kind}/{i}")
        if kind == "llm":
            duration = (item.duration_ms / 1000) if item.duration_ms else slot * 0.9
            spans.append(_span(
                trace, sid, root, f"{item.model}", "llm", ts, duration,
                input_value="(prompt content not exported by gh-aw)",
                extra_meta={"model_name": item.model, "model_provider": model_provider(item.model),
                            "metadata": {"aic": item.aic}},
                metrics={"input_tokens": item.input_tokens, "output_tokens": item.output_tokens,
                         "total_tokens": item.input_tokens + item.output_tokens,
                         "cache_read_input_tokens": item.cache_read_tokens},
            ))
        else:
            args = item.arguments if isinstance(item.arguments, str) else json.dumps(item.arguments, default=str)
            spans.append(_span(trace, sid, root, item.name, "tool", ts, slot * 0.9,
                               status="error" if item.error else "ok", input_value=(args or "")[:5_000]))

    if t.firewall:
        blocked = sorted({r.domain for r in t.blocked_requests})
        spans.append(_span(
            trace, span_id(trace, "firewall"), root, "awf.firewall", "task", start, end - start,
            status="error" if blocked else "ok",
            input_value=f"{len(t.firewall)} egress requests",
            output_value=f"blocked: {', '.join(blocked)}" if blocked else "all requests allowed",
            extra_meta={"metadata": {"blocked_domains": blocked, "blocked_requests": len(t.blocked_requests)}},
        ))
    for i, out in enumerate(t.safe_outputs):
        spans.append(_span(trace, span_id(trace, f"safe_output/{i}"), root, f"safe_output.{out.type}", "task",
                           end - 0.5, 0.5, output_value=out.body[:20_000]))

    return {"data": {"type": "span", "attributes": {
        "ml_app": ml_app, "session_id": c.session_id, "tags": tags, "spans": spans,
    }}}


def build_evaluations(report: Report, ml_app: str) -> dict:
    """Detector results as LLM Obs evaluations, so they are filterable next to the trace."""
    t = report.telemetry
    trace = trace_id(report)
    join = {"span": {"span_id": span_id(trace, "root"), "trace_id": trace}}
    now_ms = int(time.time() * 1000)
    codes = {f.code for f in report.findings}
    unique_calls = len({(c.name, json.dumps(c.arguments, sort_keys=True, default=str)) for c in t.tool_calls})
    efficiency = round(unique_calls / len(t.tool_calls), 3) if t.tool_calls else 1.0

    def metric(label: str, metric_type: str, value, passed: bool, reasoning: str = "") -> dict:
        m = {"eval_scope": "span", "join_on": join, "ml_app": ml_app, "timestamp_ms": now_ms,
             "metric_type": metric_type, "label": label, f"{metric_type}_value": value,
             "assessment": "pass" if passed else "fail"}
        if reasoning:
            m["reasoning"] = reasoning[:1000]
        return m

    metrics = [
        metric("aw_verdict", "categorical", report.verdict, report.verdict in ("healthy", "degraded"),
               "; ".join(f.message for f in report.findings)),
        metric("aw_tool_efficiency", "score", efficiency, "tool_loop" not in codes,
               f"{unique_calls} unique of {len(t.tool_calls)} tool calls"),
        metric("aw_firewall_clean", "boolean", "firewall_blocked" not in codes, "firewall_blocked" not in codes),
        metric("aw_budget_utilization", "score", report.budget_utilization,
               not codes & {"budget_exhausted", "budget_near_limit"},
               f"{t.aic:.2f} of {report.budget_aic:.0f} AIC" + (" (estimated)" if t.aic_estimated else "")),
        metric("aw_output_quality", "boolean", not codes & {"no_output", "low_quality_output", "safe_output_invalid"},
               not codes & {"no_output", "low_quality_output", "safe_output_invalid"}),
    ]
    return {"data": {"type": "evaluation_metric", "attributes": {"metrics": metrics}}}


def build_metrics(report: Report, now: float | None = None) -> dict:
    """Aggregates for dashboards/monitors. Stamped `now`: the series API rejects points >1h old."""
    t, c = report.telemetry, report.context
    ts = int(now or time.time())
    tags = metric_tags(report)

    def series(name: str, value: float, kind: int = METRIC_GAUGE, extra: list[str] | None = None) -> dict:
        return {"metric": f"agentic.run.{name}", "type": kind,
                "points": [{"timestamp": ts, "value": float(value)}], "tags": tags + (extra or [])}

    out = [
        series("count", 1, METRIC_COUNT),
        series("silent_failure", int(report.verdict == "silent_failure"), METRIC_COUNT),
        series("aic", t.aic),
        series("budget_utilization", report.budget_utilization),
        series("tokens.input", t.input_tokens, METRIC_COUNT),
        series("tokens.output", t.output_tokens, METRIC_COUNT),
        series("tokens.cache_read", t.cache_read_tokens, METRIC_COUNT),
        series("tool_calls", len(t.tool_calls)),
        series("firewall.blocked_requests", len(t.blocked_requests), METRIC_COUNT),
        series("safe_outputs", len(t.safe_outputs)),
    ]
    if c.duration_s is not None:
        out.append(series("duration_seconds", c.duration_s))
    for f in report.findings:
        out.append(series("finding", 1, METRIC_COUNT, [f"finding:{f.code}", f"severity:{f.severity}"]))
    return {"series": out}


# --------------------------------------------------------------------------- transport


@dataclass
class DatadogClient:
    api_key: str
    site: str = "datadoghq.com"
    timeout: float = 15.0
    retries: int = 3

    @property
    def api_base(self) -> str:
        return f"https://api.{self.site}"

    def post(self, path: str, body: dict) -> int:
        data = json.dumps(body, default=str).encode()
        headers = {"DD-API-KEY": self.api_key, "Content-Type": "application/json"}
        for attempt in range(1, self.retries + 1):
            req = urllib.request.Request(self.api_base + path, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.status
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                if exc.code == 429 or exc.code >= 500:
                    log.warning("Datadog %s -> %s (attempt %d): %s", path, exc.code, attempt, detail)
                else:
                    raise RuntimeError(f"Datadog {path} rejected payload ({exc.code}): {detail}") from exc
            except urllib.error.URLError as exc:
                log.warning("Datadog %s unreachable (attempt %d): %s", path, attempt, exc.reason)
            time.sleep(2 ** attempt)
        raise RuntimeError(f"Datadog {path} failed after {self.retries} attempts")

    def send(self, report: Report, ml_app: str) -> None:
        self.post("/api/intake/llm-obs/v1/trace/spans", build_spans(report, ml_app))
        self.post("/api/intake/llm-obs/v2/eval-metric", build_evaluations(report, ml_app))
        self.post("/api/v2/series", build_metrics(report))


def app_url(site: str) -> str:
    return f"https://app.{site}" if site.startswith("datadoghq.") else f"https://{site}"
