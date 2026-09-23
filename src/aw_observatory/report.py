"""Report assembly and rendering (JSON record + GitHub job summary)."""

from __future__ import annotations

from dataclasses import asdict
from urllib.parse import quote

from aw_observatory import SCHEMA_VERSION
from aw_observatory.datadog import app_url, trace_id
from aw_observatory.detectors import Thresholds, run_detectors, verdict
from aw_observatory.model import Report, RunContext, RunTelemetry

ICONS = {"healthy": "🟢", "degraded": "🟡", "silent_failure": "🔴", "failed": "⛔"}


def build_report(ctx: RunContext, tel: RunTelemetry, thresholds: Thresholds) -> Report:
    findings = run_detectors(ctx, tel, thresholds)
    return Report(context=ctx, telemetry=tel, findings=findings, verdict=verdict(ctx, findings),
                  budget_aic=thresholds.budget_aic)


def to_record(report: Report) -> dict:
    """The normalized event documented in schemas/agentic-event.schema.json."""
    c, t = report.context, report.telemetry
    return {
        "schema_version": SCHEMA_VERSION,
        "trace_id": trace_id(report),
        "verdict": report.verdict,
        "run": {**asdict(c), "session_id": c.session_id, "duration_s": c.duration_s},
        "agent": {"engine": t.engine, "model": t.model},
        "usage": {
            "llm_calls": len(t.llm_calls),
            "input_tokens": t.input_tokens,
            "output_tokens": t.output_tokens,
            "cache_read_tokens": t.cache_read_tokens,
            "aic": t.aic,
            "aic_estimated": t.aic_estimated,
            "budget_aic": report.budget_aic,
            "budget_utilization": report.budget_utilization,
        },
        "tools": {"calls": len(t.tool_calls), "errors": sum(c.error for c in t.tool_calls),
                  "distinct": len({c.name for c in t.tool_calls})},
        "firewall": {"requests": len(t.firewall), "blocked": len(t.blocked_requests),
                     "blocked_domains": sorted({r.domain for r in t.blocked_requests})},
        "outputs": [{"type": o.type, "chars": len(o.body)} for o in t.safe_outputs],
        "findings": [asdict(f) for f in report.findings],
    }


def to_markdown(report: Report, site: str, ml_app: str) -> str:
    c, t = report.context, report.telemetry
    query = quote(f"@trace_id:{trace_id(report)}")
    lines = [
        f"## {ICONS.get(report.verdict, '')} Agentic run `{c.workflow_name}` #{c.run_id}: **{report.verdict.replace('_', ' ')}**",
        "",
        f"CI conclusion: `{c.conclusion}`"
        + (f" · PR #{c.pull_request}" if c.pull_request else "")
        + (f" · source: `{c.source_path}`" if c.source_path else ""),
        "",
        "| Engine / model | LLM calls | Tokens in / out | AI Credits | Tool calls | Firewall blocks |",
        "|---|---|---|---|---|---|",
        f"| {t.engine} / {t.model} | {len(t.llm_calls)} | {t.input_tokens:,} / {t.output_tokens:,} | "
        f"{t.aic:.2f}{'*' if t.aic_estimated else ''} ({report.budget_utilization:.0%} of {report.budget_aic:.0f}) | "
        f"{len(t.tool_calls)} | {len(t.blocked_requests)} |",
        "",
    ]
    if report.findings:
        lines += ["### Findings", ""]
        lines += [f"- **{f.severity}** `{f.code}`: {f.message}" for f in report.findings]
        lines.append("")
    else:
        lines += ["No problems detected.", ""]
    if t.aic_estimated:
        lines += ["<sub>* AI Credits estimated from token counts and list prices.</sub>", ""]
    lines.append(f"[Open trace in Datadog LLM Observability]({app_url(site)}/llm/traces?query={query}) · ml_app `{ml_app}`")
    return "\n".join(lines) + "\n"
