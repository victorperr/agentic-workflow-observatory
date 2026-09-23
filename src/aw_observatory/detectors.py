"""Silent-failure detectors.

An agentic run can be green in CI and still be a failure: it looped on the same tool,
had its egress blocked by the AWF firewall, ran out of AI Credits mid-reasoning, or
posted a useless comment. Each detector turns one of those into a Finding.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Callable

from aw_observatory.model import FAILED_CONCLUSIONS, Finding, RunContext, RunTelemetry

# Safe-output types that carry human-facing text worth quality-checking.
TEXT_OUTPUTS = {
    "add_comment", "create_issue", "create_discussion", "create_pull_request",
    "create_pull_request_review_comment", "submit_pull_request_review", "update_issue",
    "update_pull_request", "update_discussion",
}
# Explicit "nothing to do" decisions are legitimate outcomes, not silence.
DECISION_OUTPUTS = {"noop", "missing_tool", "missing_data"}

_LOW_QUALITY = [
    (re.compile(r"\{\{.*?\}\}|\$\{\{"), "unrendered template expression"),
    (re.compile(r"\bTODO\b|\bTBD\b|lorem ipsum", re.I), "placeholder text"),
    (re.compile(r"\bundefined\b|\[object Object\]|\bNaN\b"), "serialization artifact"),
    (re.compile(r"as an ai (language )?model", re.I), "assistant boilerplate"),
    (re.compile(r"\bI (?:can(?:no|')t|am unable to|was unable to) (?:access|complete|find|read)", re.I),
     "agent reports it could not do the task"),
]


@dataclass
class Thresholds:
    budget_aic: float = 1000.0  # gh-aw default `max-ai-credits`
    budget_warn_ratio: float = 0.8
    loop_threshold: int = 3  # identical tool+args calls before we call it a loop
    max_tool_calls: int = 40
    min_body_chars: int = 40


def _signature(name: str, arguments: object) -> str:
    try:
        args = json.dumps(arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):
        args = str(arguments)
    return f"{name}:{args}"


def detect_tool_loops(ctx: RunContext, tel: RunTelemetry, t: Thresholds) -> list[Finding]:
    findings = []
    counts = Counter(_signature(c.name, c.arguments) for c in tel.tool_calls)
    repeated = {sig: n for sig, n in counts.items() if n >= t.loop_threshold}
    if repeated:
        worst_sig, worst_n = max(repeated.items(), key=lambda kv: kv[1])
        findings.append(Finding(
            code="tool_loop",
            severity="error",
            message=f"Agent repeated the same tool call {worst_n}x ({worst_sig.split(':', 1)[0]}).",
            evidence={"repeated_calls": {k[:200]: v for k, v in repeated.items()}},
        ))
    if len(tel.tool_calls) > t.max_tool_calls:
        findings.append(Finding(
            code="excessive_tool_calls",
            severity="warning",
            message=f"{len(tel.tool_calls)} tool calls (threshold {t.max_tool_calls}).",
            evidence={"by_tool": dict(Counter(c.name for c in tel.tool_calls).most_common(10))},
        ))
    failed = [c.name for c in tel.tool_calls if c.error]
    if failed and len(failed) >= max(2, len(tel.tool_calls) // 3):
        findings.append(Finding(
            code="tool_errors",
            severity="warning",
            message=f"{len(failed)} of {len(tel.tool_calls)} tool calls returned errors.",
            evidence={"by_tool": dict(Counter(failed))},
        ))
    return findings


def detect_firewall_blocks(ctx: RunContext, tel: RunTelemetry, t: Thresholds) -> list[Finding]:
    blocked = tel.blocked_requests
    if not blocked:
        return []
    domains = Counter(r.domain for r in blocked)
    return [Finding(
        code="firewall_blocked",
        severity="error",
        message=f"AWF firewall blocked {len(blocked)} request(s) to {', '.join(sorted(domains))}. "
        "Add them to `network.allowed` if they are legitimate.",
        evidence={"blocked_domains": dict(domains)},
    )]


def detect_budget(ctx: RunContext, tel: RunTelemetry, t: Thresholds) -> list[Finding]:
    findings = []
    used = tel.aic
    ratio = used / t.budget_aic if t.budget_aic > 0 else 0.0
    evidence = {"aic": used, "budget_aic": t.budget_aic, "estimated": tel.aic_estimated}
    if "budget_exhausted" in tel.markers or ratio >= 1.0:
        findings.append(Finding("budget_exhausted", "error",
                                f"Run hit its AI Credits budget ({used:.1f}/{t.budget_aic:.0f} AIC).", evidence))
    elif ratio >= t.budget_warn_ratio:
        findings.append(Finding("budget_near_limit", "warning",
                                f"Run used {ratio:.0%} of its AI Credits budget.", evidence))
    if "max_turns_reached" in tel.markers:
        findings.append(Finding("max_turns_reached", "error", "Agent stopped at its max-turns cap before finishing."))
    if "timeout" in tel.markers:
        findings.append(Finding("agent_timeout", "error", "Agent execution timed out."))
    return findings


def detect_output_problems(ctx: RunContext, tel: RunTelemetry, t: Thresholds) -> list[Finding]:
    findings = []
    kinds = {o.type for o in tel.safe_outputs}
    if not tel.safe_outputs and ctx.conclusion not in FAILED_CONCLUSIONS:
        findings.append(Finding("no_output", "error",
                                "Run succeeded but produced no safe output and no explicit noop."))
    for err in tel.output_errors:
        findings.append(Finding("safe_output_invalid", "error", f"Safe output rejected: {err[:300]}"))
    for out in tel.safe_outputs:
        if out.type in ("missing_tool", "missing_data"):
            findings.append(Finding(out.type, "warning",
                                    f"Agent reported {out.type.replace('_', ' ')}: {out.body[:200]}", {"item": out.raw}))
        if out.type not in TEXT_OUTPUTS:
            continue
        problems = [label for pattern, label in _LOW_QUALITY if pattern.search(out.body)]
        if len(out.body.strip()) < t.min_body_chars:
            problems.append(f"only {len(out.body.strip())} characters")
        paragraphs = [p.strip() for p in out.body.split("\n\n") if len(p.strip()) > 20]
        if paragraphs and Counter(paragraphs).most_common(1)[0][1] >= 3:
            problems.append("repeated paragraphs")
        if problems:
            findings.append(Finding("low_quality_output", "warning",
                                    f"{out.type} looks low quality: {', '.join(problems)}.",
                                    {"type": out.type, "excerpt": out.body[:300]}))
    if kinds and kinds <= DECISION_OUTPUTS - {"noop"}:
        findings.append(Finding("gave_up", "warning", "Agent only reported missing tools/data and did no work."))
    return findings


def detect_threats(ctx: RunContext, tel: RunTelemetry, t: Thresholds) -> list[Finding]:
    d = tel.detection or {}
    flagged = [k for k in ("prompt_injection", "secret_leak", "malicious_patch") if d.get(k)]
    verdict = str(d.get("verdict", d.get("conclusion", ""))).lower()
    if flagged or verdict in ("unsafe", "threat", "threat_detected", "fail", "failure"):
        return [Finding("threat_detected", "error",
                        f"Threat detection flagged the run ({', '.join(flagged) or verdict}).", {"detection": d})]
    return []


DETECTORS: list[Callable[[RunContext, RunTelemetry, Thresholds], list[Finding]]] = [
    detect_tool_loops,
    detect_firewall_blocks,
    detect_budget,
    detect_output_problems,
    detect_threats,
]


def run_detectors(ctx: RunContext, tel: RunTelemetry, t: Thresholds) -> list[Finding]:
    return [f for detector in DETECTORS for f in detector(ctx, tel, t)]


def verdict(ctx: RunContext, findings: list[Finding]) -> str:
    """failed = CI saw it; silent_failure = CI green but the agent failed; degraded = warnings only."""
    if ctx.conclusion in FAILED_CONCLUSIONS:
        return "failed"
    if any(f.severity == "error" for f in findings):
        return "silent_failure"
    if findings:
        return "degraded"
    return "healthy"
