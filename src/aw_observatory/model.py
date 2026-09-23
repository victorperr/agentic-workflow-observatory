"""Normalized data model shared by the parsers, detectors and exporters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# CI conclusions that mean the run failed "the normal way" (visible in any pipeline view).
FAILED_CONCLUSIONS = {"failure", "timed_out", "cancelled", "startup_failure", "action_required"}


@dataclass
class RunContext:
    """Where a run came from: repository, PR, source Markdown workflow, CI outcome."""

    repository: str
    run_id: int
    run_attempt: int = 1
    workflow_name: str = "unknown"
    workflow_path: str = ""  # compiled lock file, e.g. .github/workflows/pr-reviewer.lock.yml
    source_path: str = ""  # Markdown source, e.g. .github/workflows/pr-reviewer.md
    event: str = ""
    head_branch: str = ""
    head_sha: str = ""
    actor: str = ""
    conclusion: str = "unknown"
    pull_request: int | None = None
    html_url: str = ""
    started_at: float | None = None  # epoch seconds
    ended_at: float | None = None

    @property
    def duration_s(self) -> float | None:
        if self.started_at is None or self.ended_at is None:
            return None
        return max(0.0, self.ended_at - self.started_at)

    @property
    def session_id(self) -> str:
        """Every agent run on the same PR shares one LLM Observability session."""
        if self.pull_request:
            return f"{self.repository}#pr-{self.pull_request}"
        return f"{self.repository}@{self.head_branch or 'default'}"


@dataclass
class LlmCall:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    aic: float | None = None  # AI Credits reported by the AWF API proxy (1 AIC = $0.01)
    timestamp: float | None = None
    duration_ms: float | None = None


@dataclass
class ToolCall:
    name: str
    arguments: Any = None
    timestamp: float | None = None
    error: bool = False
    call_id: str | None = None


@dataclass
class FirewallRequest:
    domain: str
    allowed: bool
    status: int | None = None
    timestamp: float | None = None


@dataclass
class SafeOutput:
    type: str
    body: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class RunTelemetry:
    """Everything recovered from the run's gh-aw artifacts."""

    engine: str = "unknown"
    model: str = "unknown"
    llm_calls: list[LlmCall] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    firewall: list[FirewallRequest] = field(default_factory=list)
    safe_outputs: list[SafeOutput] = field(default_factory=list)
    output_errors: list[str] = field(default_factory=list)
    detection: dict | None = None
    markers: set[str] = field(default_factory=set)  # e.g. budget_exhausted, max_turns_reached
    instructions: str = ""  # the Markdown prompt the workflow was compiled from
    aic_estimated: bool = False
    files_seen: list[str] = field(default_factory=list)

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.llm_calls)

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.llm_calls)

    @property
    def cache_read_tokens(self) -> int:
        return sum(c.cache_read_tokens for c in self.llm_calls)

    @property
    def aic(self) -> float:
        return round(sum(c.aic or 0.0 for c in self.llm_calls), 3)

    @property
    def blocked_requests(self) -> list[FirewallRequest]:
        return [r for r in self.firewall if not r.allowed]


@dataclass
class Finding:
    code: str  # stable identifier, also used as a Datadog tag: finding:<code>
    severity: str  # "warning" | "error"
    message: str
    evidence: dict = field(default_factory=dict)


@dataclass
class Report:
    context: RunContext
    telemetry: RunTelemetry
    findings: list[Finding]
    verdict: str  # healthy | degraded | silent_failure | failed
    budget_aic: float = 1000.0

    @property
    def budget_utilization(self) -> float:
        return round(self.telemetry.aic / self.budget_aic, 4) if self.budget_aic > 0 else 0.0
