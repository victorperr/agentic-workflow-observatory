import json

import pytest

from aw_observatory import artifacts, context
from aw_observatory.datadog import DatadogClient, build_evaluations, build_metrics, build_spans, trace_id
from aw_observatory.detectors import Thresholds
from aw_observatory.report import build_report, to_markdown, to_record


@pytest.fixture
def report(fixtures, repo_root):
    run = fixtures / "runs/silent-failure"
    ctx = context.load_context(str(run / "event.json"), {})
    tel = artifacts.load_run(run, repo_root, ctx.source_path)
    return build_report(ctx, tel, Thresholds(budget_aic=100))


def test_spans_follow_llmobs_intake_contract(report):
    body = build_spans(report, "gh-aw")
    attrs = body["data"]["attributes"]
    assert body["data"]["type"] == "span"
    assert attrs["ml_app"] == "gh-aw"
    assert attrs["session_id"] == "acme/webapp#pr-42"
    spans = attrs["spans"]
    root = spans[0]
    assert root["parent_id"] == "undefined" and root["meta"]["kind"] == "agent"
    assert root["status"] == "error"
    assert all(s["trace_id"] == root["trace_id"] for s in spans)
    assert all(s["parent_id"] == root["span_id"] for s in spans[1:])
    assert len({s["span_id"] for s in spans}) == len(spans)
    kinds = [s["meta"]["kind"] for s in spans]
    assert kinds.count("llm") == 3 and kinds.count("tool") == 5
    assert any(s["name"] == "awf.firewall" and s["status"] == "error" for s in spans)
    assert any(s["name"] == "safe_output.add_comment" for s in spans)
    for s in spans:
        assert isinstance(s["start_ns"], int) and s["duration"] > 0
    json.dumps(body)  # serializable


def test_trace_carries_github_correlation_tags(report):
    tags = build_spans(report, "gh-aw")["data"]["attributes"]["tags"]
    for expected in ("gh.repo:acme/webapp", "gh.pr:42", "gh.run_id:9876543210",
                     "gh.workflow_source:.github/workflows/pr-reviewer.md", "aw.verdict:silent_failure",
                     "aw.finding:firewall_blocked", "ci.conclusion:success"):
        assert expected in tags


def test_trace_id_is_deterministic_per_attempt(report):
    first = trace_id(report)
    assert first == trace_id(report) and len(first) == 32
    report.context.run_attempt = 2
    assert trace_id(report) != first


def test_evaluations_join_the_root_span(report):
    body = build_evaluations(report, "gh-aw")
    metrics = body["data"]["attributes"]["metrics"]
    root = build_spans(report, "gh-aw")["data"]["attributes"]["spans"][0]
    by_label = {m["label"]: m for m in metrics}
    assert by_label["aw_verdict"]["categorical_value"] == "silent_failure"
    assert by_label["aw_firewall_clean"]["boolean_value"] is False
    assert by_label["aw_tool_efficiency"]["score_value"] == 0.4
    for m in metrics:
        assert m["join_on"]["span"] == {"span_id": root["span_id"], "trace_id": root["trace_id"]}


def test_metrics_use_low_cardinality_tags(report):
    series = build_metrics(report, now=1790157600)["series"]
    names = {s["metric"] for s in series}
    assert {"agentic.run.count", "agentic.run.silent_failure", "agentic.run.aic", "agentic.run.finding"} <= names
    for s in series:
        assert not any(t.startswith(("gh.run_id", "gh.pr", "gh.sha")) for t in s["tags"])
    silent = next(s for s in series if s["metric"] == "agentic.run.silent_failure")
    assert silent["points"][0]["value"] == 1.0


def test_record_and_summary(report):
    record = to_record(report)
    assert record["verdict"] == "silent_failure"
    assert record["firewall"]["blocked_domains"] == ["registry.npmjs.org"]
    md = to_markdown(report, "datadoghq.eu", "gh-aw")
    assert "silent failure" in md and "https://app.datadoghq.eu/llm/traces" in md


def test_client_sends_three_payloads(report, monkeypatch):
    sent = []
    client = DatadogClient(api_key="k", site="us5.datadoghq.com")
    monkeypatch.setattr(client, "post", lambda path, body: sent.append(path) or 202)
    client.send(report, "gh-aw")
    assert client.api_base == "https://api.us5.datadoghq.com"
    assert sent == ["/api/intake/llm-obs/v1/trace/spans", "/api/intake/llm-obs/v2/eval-metric", "/api/v2/series"]
