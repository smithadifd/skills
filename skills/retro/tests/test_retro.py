"""Contract tests for retro.py: triage, typing, validation and the write path."""

import copy
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "retro.py"
FIX = HERE / "fixtures"
sys.path.insert(0, str(HERE.parent))
import retro  # noqa: E402


def run(*args, cwd=None):
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *map(str, args)],
        capture_output=True, text=True, cwd=cwd,
    )


def draft():
    return json.loads((FIX / "draft.json").read_text())


def built():
    return retro.build(draft())


def by_id(data, finding_id):
    return next(f for f in data["findings"] if f["id"] == finding_id)


# ---- triage


def test_trivial_transcript_is_skipped_with_a_message():
    result = run("triage", FIX / "trivial.jsonl")
    assert result.returncode == 3
    assert "trivial session" in result.stdout


def test_real_transcript_proceeds():
    result = run("triage", FIX / "mistakes.jsonl")
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"trivial": False, "tool_calls": 16, "edits": 7}


# ---- scan


def test_scan_finds_the_repeated_mechanical_class_and_both_corrections():
    out = json.loads(run("scan", FIX / "mistakes.jsonl").stdout)
    classes = {c["class"]: c["count"] for c in out["mechanical_candidates"]}
    assert classes == {"E501": 2}
    assert len(out["corrections"]) == 2


# ---- typing


def test_mechanical_class_is_typed_encode_in_structure():
    finding = by_id(built(), "line-length")
    assert finding["type"] == "encode-in-structure"
    assert finding["structure"] == "hook"


def test_judgment_class_is_a_proposed_unapproved_diff():
    finding = by_id(built(), "deps")
    assert finding["type"] == "skill-or-rule-edit"
    assert finding["approved"] is False
    assert finding["diff"].startswith("--- a/")


def test_single_instance_judgment_is_dropped_not_proposed():
    data = built()
    assert "single-slip" not in [f["id"] for f in data["findings"]]
    assert data["dropped"] == ["single-slip"]


def test_nothing_met_the_bar_is_a_valid_single_result():
    data = retro.build(json.loads((FIX / "draft-none.json").read_text()))
    assert [f["type"] for f in data["findings"]] == ["nothing-met-the-bar"]
    assert retro.validate(data) == []


def test_obsolete_rule_is_proposed_for_removal_without_two_instances():
    item = {"id": "old", "severity": "low", "obsolete_rule": True,
            "evidence": ["the lint now rejects this"], "diff": "-old rule\n"}
    data = retro.build({"findings": [item]})
    finding = data["findings"][0]
    assert finding["type"] == "skill-or-rule-edit" and finding["removal"] is True
    assert retro.validate(data) == []


def test_repo_learning_needs_a_docs_path_and_no_existing_doc():
    item = {"id": "note", "severity": "low", "learning": "Run make seed first.",
            "path": "docs/setup.md", "evidence": ["one slip"]}
    data = retro.build({"findings": [item]})
    assert data["findings"][0]["type"] == "repo-learning"
    assert retro.validate(data) == []
    item["already_documented"] = True
    assert retro.build({"findings": [item]})["findings"][0]["type"] == "nothing-met-the-bar"
    item["already_documented"] = False
    item["path"] = "src/notes.md"
    assert any("docs/" in p for p in retro.validate(retro.build({"findings": [item]})))


# ---- checker


def test_built_findings_are_sorted_and_valid():
    data = built()
    assert [f["severity"] for f in data["findings"]] == ["high", "medium"]
    assert retro.validate(data) == []


def test_checker_refuses_a_rule_proposal_with_one_instance():
    data = built()
    by_id(data, "deps")["instances"] = by_id(data, "deps")["instances"][:1]
    assert any("at least 2" in p for p in retro.validate(data))


def test_checker_counts_distinct_instances():
    data = built()
    deps = by_id(data, "deps")
    deps["instances"] = [deps["instances"][0], copy.deepcopy(deps["instances"][0])]
    assert any("at least 2" in p for p in retro.validate(data))


def test_checker_refuses_an_unsorted_list():
    data = built()
    data["findings"].reverse()
    assert any("not sorted" in p for p in retro.validate(data))


def test_checker_refuses_an_edit_finding_without_a_diff():
    data = built()
    by_id(data, "deps")["diff"] = ""
    assert any("needs a diff" in p for p in retro.validate(data))


def test_checker_refuses_missing_approved_state_type_and_evidence():
    data = built()
    del by_id(data, "deps")["approved"]
    by_id(data, "line-length")["evidence"] = []
    problems = retro.validate(data)
    assert any("approved state" in p for p in problems)
    assert any("evidence is required" in p for p in problems)
    data["findings"][0]["type"] = "other"
    assert any("type must be" in p for p in retro.validate(data))


def test_build_refuses_an_edit_without_a_diff(tmp_path):
    bad = draft()
    del bad["findings"][0]["diff"]
    src = tmp_path / "bad.json"
    src.write_text(json.dumps(bad))
    result = run("build", src, "--out", tmp_path / "f.json")
    assert result.returncode == 1 and "needs a diff" in result.stderr
    assert not (tmp_path / "f.json").exists()


def test_check_command_reads_json_and_markdown(tmp_path):
    for name in ("f.json", "f.md"):
        assert run("build", FIX / "draft.json", "--out", tmp_path / name).returncode == 0
        assert run("check", tmp_path / name).returncode == 0
    bad = json.loads((tmp_path / "f.json").read_text())
    bad["findings"].reverse()
    (tmp_path / "bad.json").write_text(json.dumps(bad))
    assert run("check", tmp_path / "bad.json").returncode == 1


# ---- write path


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout


def test_write_path_changes_only_the_findings_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "CONTRIBUTING.md").write_text("# Contributing\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "setup.md").write_text("setup\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")

    out = repo / "retro-findings.md"
    assert run("build", FIX / "draft.json", "--out", out, cwd=repo).returncode == 0
    assert run("approve", out, "deps", cwd=repo).returncode == 0
    assert by_id(retro._load_findings(out), "deps")["approved"] is True
    assert run("check", out, cwd=repo).returncode == 0

    changed = git(repo, "status", "--porcelain", "--untracked-files=all").split("\n")
    assert [line[3:] for line in changed if line] == ["retro-findings.md"]
    assert (repo / "CONTRIBUTING.md").read_text() == "# Contributing\n"


def test_approve_refuses_a_non_edit_finding(tmp_path):
    out = tmp_path / "f.json"
    run("build", FIX / "draft.json", "--out", out)
    assert run("approve", out, "line-length").returncode == 2
    assert by_id(json.loads(out.read_text()), "deps")["approved"] is False


# ---- round 2


def test_mechanical_class_needs_two_distinct_instances():
    item = draft()["findings"][1]
    item["instances"] = item["instances"][:1]
    data = retro.build({"findings": [item]})
    assert [f["type"] for f in data["findings"]] == ["nothing-met-the-bar"]
    typed = built()
    by_id(typed, "line-length")["instances"] = by_id(typed, "line-length")["instances"][:1]
    assert any("at least 2" in p for p in retro.validate(typed))


def test_structure_finding_needs_a_proposal():
    data = built()
    by_id(data, "line-length")["proposal"] = "  "
    assert any("needs a proposal" in p for p in retro.validate(data))


def test_passing_check_runs_are_not_failures():
    out = json.loads(run("scan", FIX / "passing.jsonl").stdout)
    assert out["mechanical_candidates"] == []


def test_approve_refuses_an_invalid_file(tmp_path):
    out = tmp_path / "f.json"
    run("build", FIX / "draft.json", "--out", out)
    data = json.loads(out.read_text())
    data["findings"].reverse()
    out.write_text(json.dumps(data))
    result = run("approve", out, "deps")
    assert result.returncode == 2 and "invalid" in result.stderr
    assert by_id(json.loads(out.read_text()), "deps")["approved"] is False


# ---- round 3


def test_suffix_zero_counts_are_not_failures():
    out = json.loads(run("scan", FIX / "passing.jsonl").stdout)
    assert out["mechanical_candidates"] == []


def test_nonzero_suffix_counts_are_still_failures(tmp_path):
    rec = [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "pytest"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "Errors: 10"}]}},
    ]
    src = tmp_path / "t.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in rec) + "\n")
    out = json.loads(run("scan", src).stdout)
    assert len(out["mechanical_candidates"]) == 1


def test_malformed_instance_ref_is_reported_cleanly(tmp_path):
    bad = draft()
    bad["findings"][0]["instances"][0]["ref"] = ["line", 11]
    src = tmp_path / "bad.json"
    src.write_text(json.dumps(bad))
    result = run("build", src, "--out", tmp_path / "f.json")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr and "non-empty string ref" in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1
    data = built()
    by_id(data, "deps")["instances"][0]["ref"] = ["x"]
    assert any("non-empty string ref" in p for p in retro.validate(data))
    by_id(data, "deps")["instances"][0]["ref"] = ""
    assert any("non-empty string ref" in p for p in retro.validate(data))
