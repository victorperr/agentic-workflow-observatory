import json

from aw_observatory.cli import main


def test_dry_run_end_to_end(fixtures, repo_root, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    run = fixtures / "runs/silent-failure"
    out, summary = tmp_path / "record.json", tmp_path / "summary.md"
    code = main([
        "collect", "--run-dir", str(run), "--event", str(run / "event.json"), "--repo-root", str(repo_root),
        "--budget-aic", "100", "--dry-run", "--output", str(out), "--summary", str(summary),
        "--fail-on", "silent_failure",
    ])
    assert code == 1  # --fail-on silent_failure
    payloads = json.loads(capsys.readouterr().out)
    assert set(payloads) == {"spans", "evaluations", "metrics"}
    assert json.loads(out.read_text())["verdict"] == "silent_failure"
    assert "firewall_blocked" in summary.read_text(encoding="utf-8")


def test_missing_api_key_is_a_clear_error(fixtures, monkeypatch):
    monkeypatch.delenv("DD_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    run = fixtures / "runs/healthy"
    assert main(["collect", "--run-dir", str(run), "--event", str(run / "event.json"), "--summary", ""]) == 2


def test_github_output_is_written(fixtures, tmp_path, monkeypatch):
    gh_out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    run = fixtures / "runs/healthy"
    assert main(["collect", "--run-dir", str(run), "--event", str(run / "event.json"), "--dry-run",
                 "--summary", ""]) == 0
    assert "verdict=healthy" in gh_out.read_text()
