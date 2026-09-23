import json

from aw_observatory import artifacts, context


def test_workflow_run_event_links_run_to_pr_and_markdown_source(fixtures):
    ctx = context.load_context(str(fixtures / "runs/silent-failure/event.json"), {})
    assert ctx.repository == "acme/webapp"
    assert ctx.run_id == 9876543210
    assert ctx.pull_request == 42
    assert ctx.source_path == ".github/workflows/pr-reviewer.md"
    assert ctx.conclusion == "success"
    assert ctx.duration_s == 390
    assert ctx.session_id == "acme/webapp#pr-42"


def test_aw_info_fallback_when_no_event(fixtures):
    aw_info = artifacts.load_aw_info(fixtures / "runs/silent-failure")
    ctx = context.load_context(None, aw_info)
    assert ctx.run_id == 9876543210
    assert ctx.head_branch == "feature/login"
    assert ctx.session_id == "acme/webapp@feature/login"


def test_parse_time_accepts_iso_and_epoch_units():
    assert context.parse_time("2026-09-23T10:00:00Z") == context.parse_time(1790157600)
    assert context.parse_time(1790157600_000) == 1790157600
    assert context.parse_time(1790157600_000_000_000) == 1790157600
    assert context.parse_time("not a date") is None


def test_silent_failure_run_is_fully_parsed(fixtures, repo_root):
    tel = artifacts.load_run(fixtures / "runs/silent-failure", repo_root, ".github/workflows/pr-reviewer.md")
    assert tel.engine == "claude" and tel.model == "claude-sonnet-5"
    assert len(tel.llm_calls) == 3
    assert tel.aic == 88.5 and not tel.aic_estimated
    assert [c.name for c in tel.tool_calls].count("mcp__github__get_pull_request_diff") == 4
    assert sum(c.error for c in tel.tool_calls) == 5
    assert {r.domain for r in tel.blocked_requests} == {"registry.npmjs.org"}
    assert len(tel.firewall) == 4
    assert tel.safe_outputs[0].type == "add_comment"
    assert tel.instructions.startswith("# PR Reviewer")  # frontmatter stripped


def test_healthy_run_uses_mcp_logs_and_estimates_cost(fixtures):
    tel = artifacts.load_run(fixtures / "runs/healthy")
    assert [c.name for c in tel.tool_calls] == ["get_pull_request", "get_pull_request_files", "get_file_contents"]
    assert tel.safe_outputs[0].type == "add_comment"  # add-comment normalized
    assert tel.aic_estimated and tel.aic > 0
    assert tel.firewall == []


def test_markers_detect_truncated_runs(tmp_path):
    (tmp_path / "agent-stdio.log").write_text(
        "Error: AI credits budget exceeded (1000 AIC), stopping agent\nmax-turns limit reached\n"
    )
    assert artifacts.parse_markers(tmp_path) == {"budget_exhausted", "max_turns_reached"}


def test_agent_usage_fallback(tmp_path):
    (tmp_path / "agent_usage.json").write_text(json.dumps(
        {"agent_usage": {"model": "claude-opus-5", "input_tokens": 1000, "output_tokens": 50, "aic": 4.2}}
    ))
    calls = artifacts.parse_token_usage(tmp_path)
    assert len(calls) == 1 and calls[0].aic == 4.2


def test_missing_run_dir_raises(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        artifacts.load_run(tmp_path / "nope")
