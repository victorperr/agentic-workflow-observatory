from aw_observatory import artifacts, context
from aw_observatory.detectors import Thresholds, run_detectors, verdict
from aw_observatory.model import RunContext, RunTelemetry, SafeOutput, ToolCall


def _load(fixtures, repo_root, name):
    run = fixtures / "runs" / name
    ctx = context.load_context(str(run / "event.json"), {})
    return ctx, artifacts.load_run(run, repo_root, ctx.source_path)


def _codes(findings):
    return {f.code for f in findings}


def test_green_ci_run_is_flagged_as_silent_failure(fixtures, repo_root):
    ctx, tel = _load(fixtures, repo_root, "silent-failure")
    findings = run_detectors(ctx, tel, Thresholds(budget_aic=100))
    assert {"tool_loop", "tool_errors", "firewall_blocked", "budget_near_limit", "low_quality_output"} <= _codes(findings)
    assert ctx.conclusion == "success"
    assert verdict(ctx, findings) == "silent_failure"


def test_healthy_run_has_no_findings(fixtures, repo_root):
    ctx, tel = _load(fixtures, repo_root, "healthy")
    findings = run_detectors(ctx, tel, Thresholds())
    assert findings == []
    assert verdict(ctx, findings) == "healthy"


def test_ci_failure_wins_over_everything():
    ctx = RunContext(repository="a/b", run_id=1, conclusion="failure")
    assert verdict(ctx, []) == "failed"


def test_no_output_on_green_run_is_an_error():
    ctx = RunContext(repository="a/b", run_id=1, conclusion="success")
    findings = run_detectors(ctx, RunTelemetry(), Thresholds())
    assert _codes(findings) == {"no_output"}
    assert verdict(ctx, findings) == "silent_failure"


def test_noop_is_a_legitimate_outcome():
    ctx = RunContext(repository="a/b", run_id=1, conclusion="success")
    tel = RunTelemetry(safe_outputs=[SafeOutput(type="noop", body="Docs-only change, nothing to review.")])
    assert run_detectors(ctx, tel, Thresholds()) == []


def test_distinct_calls_to_same_tool_are_not_a_loop():
    ctx = RunContext(repository="a/b", run_id=1, conclusion="success")
    calls = [ToolCall(name="get_file_contents", arguments={"path": f"src/{i}.py"}) for i in range(5)]
    tel = RunTelemetry(tool_calls=calls, safe_outputs=[SafeOutput("noop", "nothing")])
    assert "tool_loop" not in _codes(run_detectors(ctx, tel, Thresholds()))


def test_low_quality_patterns():
    ctx = RunContext(repository="a/b", run_id=1, conclusion="success")
    body = "Thanks for the PR, ${{ github.event.pull_request.title }} looks good to me overall!"
    tel = RunTelemetry(safe_outputs=[SafeOutput("add_comment", body)])
    finding = next(f for f in run_detectors(ctx, tel, Thresholds()) if f.code == "low_quality_output")
    assert "unrendered template expression" in finding.message


def test_threat_detection_and_budget_exhaustion():
    ctx = RunContext(repository="a/b", run_id=1, conclusion="success")
    tel = RunTelemetry(detection={"prompt_injection": True}, markers={"budget_exhausted"},
                       safe_outputs=[SafeOutput("noop", "x")])
    assert {"threat_detected", "budget_exhausted"} <= _codes(run_detectors(ctx, tel, Thresholds()))


def test_repetitive_comment_is_low_quality():
    ctx = RunContext(repository="a/b", run_id=1, conclusion="success")
    body = "\n\n".join(["Consider adding more tests to this pull request."] * 3)
    tel = RunTelemetry(safe_outputs=[SafeOutput("add_comment", body)])
    finding = next(f for f in run_detectors(ctx, tel, Thresholds()) if f.code == "low_quality_output")
    assert "repeated paragraphs" in finding.message
    assert verdict(ctx, [finding]) == "degraded"
