"""Network-free tests for memory_gardener's deterministic mechanics.

Covers the load-bearing invariants: additive-only frontmatter splicing (the
harness reads name/description/metadata.type and must keep working), wikilink
extract/repair, MEMORY.md index rebuild, >90d age logic, plan validation, the
report==apply content-parity guarantee, and a full apply against the invented
fixture memory dir.
"""
import os
import re
import shutil
import stat
import subprocess
from datetime import date

import pytest

import memory_gardener as mg

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "memory")
PLAN = os.path.join(HERE, "fixtures", "plan.json")
# Mirrors the live sharded memory store's shape: a MEMORY.md core carrying JIT
# shard-pointer lines to MEMORY.watering.md / MEMORY.tools.md (each holding a
# couple of real fact notes), plus MEMORY.orphan.md — a shard file that exists
# on disk but has NO pointer line in the core index, used to prove the
# in_index fix only trusts a shard that is actually reachable from the core.
SHARDED_FIXTURE = os.path.join(HERE, "fixtures", "memory_sharded")
# Also already-sharded, but shaped for `shard --extend` (issue #175):
# MEMORY.md carries JIT pointers to MEMORY.watering.md / MEMORY.tools.md (each
# already holding one note) PLUS two bullets still sitting directly in the
# core: sprinkler-timer.md (genuine OVERFLOW — classifies to the EXISTING
# watering shard) and compost-bin-setup.md (classifies to a "compost" shard
# that has NO MEMORY.compost.md file on disk — extend must leave it alone,
# proving it never creates a new shard).
EXTEND_FIXTURE = os.path.join(HERE, "fixtures", "memory_extend")
TODAY = date(2026, 7, 8)

SAMPLE_FM = (
    "---\n"
    "name: garden-hose-leak\n"
    'description: "Recurring low pressure at the far spigot"\n'
    "metadata: \n"
    "  node_type: memory\n"
    "  type: project\n"
    "  originSessionId: cccc3333\n"
    "---\n"
    "\nBody text with a [[sprinkler-timer]] link.\n"
)


# ---------------------------------------------------------------- frontmatter

def test_split_and_read_fields():
    fm, body = mg.split_frontmatter(SAMPLE_FM)
    assert mg.read_fm_field(fm, "name") == "garden-hose-leak"
    assert mg.read_fm_field(fm, "description") == "Recurring low pressure at the far spigot"
    assert mg.read_metadata_field(fm, "type") == "project"
    assert mg.read_metadata_field(fm, "node_type") == "memory"
    assert body.strip().startswith("Body text")


def test_no_frontmatter():
    fm, body = mg.split_frontmatter("Just a body, no fences.\n")
    assert fm is None
    assert body == "Just a body, no fences.\n"


def test_apply_frontmatter_is_additive_and_preserves_existing():
    out = mg.apply_frontmatter(SAMPLE_FM, {
        "status": "current",
        "last_verified": "2026-07-08",
        "supersedes": ["sprinkler-timer"],
    })
    # Existing keys byte-preserved.
    assert "name: garden-hose-leak\n" in out
    assert 'description: "Recurring low pressure at the far spigot"\n' in out
    assert "  type: project\n" in out
    assert "  originSessionId: cccc3333\n" in out
    # New keys added.
    assert "status: current\n" in out
    assert "last_verified: 2026-07-08\n" in out
    assert 'supersedes: ["sprinkler-timer"]\n' in out
    # metadata.type still readable by the harness path.
    fm, _ = mg.split_frontmatter(out)
    assert mg.read_metadata_field(fm, "type") == "project"


def test_apply_frontmatter_rejects_non_writable_keys():
    # description is DELIBERATELY absent here — issue #177 made it a
    # fourth writable key; see the dedicated description tests below.
    for bad in ("name", "metadata", "type"):
        with pytest.raises(ValueError):
            mg.apply_frontmatter(SAMPLE_FM, {bad: "x"})


def test_apply_frontmatter_restamp_updates_in_place():
    once = mg.apply_frontmatter(SAMPLE_FM, {"last_verified": "2026-01-01"})
    twice = mg.apply_frontmatter(once, {"last_verified": "2026-07-08"})
    assert twice.count("last_verified:") == 1
    assert "last_verified: 2026-07-08\n" in twice
    assert "2026-01-01" not in twice


def test_apply_frontmatter_creates_block_when_absent():
    out = mg.apply_frontmatter("Body only.\n", {"status": "current"})
    assert out.startswith("---\nstatus: current\n---\n")
    assert out.endswith("Body only.\n")


def _fm_description(fm_lines):
    """Strictly decode the one-line, double-quoted `description:` scalar."""
    import json
    lines = [ln for ln in fm_lines if ln.startswith("description:")]
    assert len(lines) == 1
    return json.loads(lines[0][len("description:"):].strip())


# ---------------------------------------------------- description (writable, issue #177)
# `description` is the harness's recall surface: a SUPERSEDED/UPDATE body
# rewrite previously had no mechanical way to correct a description that now
# contradicts it. These tests cover the fourth WRITABLE_KEYS entry under the
# SAME guardrails as status/last_verified/supersedes above.

def test_apply_frontmatter_writes_description_preserves_other_fields():
    out = mg.apply_frontmatter(SAMPLE_FM, {"description": "Fixed: replaced the Y-splitter"})
    assert 'description: "Fixed: replaced the Y-splitter"\n' in out
    assert 'description: "Recurring low pressure at the far spigot"\n' not in out
    # Every other field byte-preserved.
    assert "name: garden-hose-leak\n" in out
    assert "  node_type: memory\n" in out
    assert "  type: project\n" in out
    assert "  originSessionId: cccc3333\n" in out
    fm, _ = mg.split_frontmatter(out)
    assert mg.read_metadata_field(fm, "type") == "project"


def test_apply_frontmatter_description_restamp_replaces_old_value_in_place():
    once = mg.apply_frontmatter(SAMPLE_FM, {"description": "first correction"})
    twice = mg.apply_frontmatter(once, {"description": "second correction"})
    assert twice.count("description:") == 1
    assert '"second correction"' in twice
    assert "first correction" not in twice


def test_apply_frontmatter_description_quotes_special_characters_and_parses_as_yaml():
    """description is free-text (unlike status's fixed enum / last_verified's
    ISO date), so it needs real quoting — a colon-space or embedded quote left
    unquoted would corrupt the YAML a real parser reads. Verify with an actual
    parser: the written scalar is a double-quoted string, and every escape the
    writer emits is one JSON and YAML double-quoted scalars share, so
    `json.loads` of the value is the strict reader here (no third-party YAML
    dependency is available to this suite)."""
    tricky = ('Contains: a colon-space, "double quotes", a backslash \\ '
              'and an em dash — end.')
    out = mg.apply_frontmatter(SAMPLE_FM, {"description": tricky})
    fm, _ = mg.split_frontmatter(out)
    parsed = _fm_description(fm)
    assert parsed == tricky
    # Exactly one physical output line for the field, even though the value
    # itself contains a colon that would otherwise read as a YAML separator.
    desc_lines = [ln for ln in fm if ln.startswith("description:")]
    assert len(desc_lines) == 1


def test_apply_frontmatter_description_serializes_embedded_newline_as_one_line():
    """If a plan ever supplies a description containing a literal newline, the
    write side must still collapse it to a single physical frontmatter line
    (json.dumps escapes \\n rather than emitting a real line break) — the
    byte-precise single-field-rewrite assumption has to hold on the way OUT
    regardless of what's handed in, even though real plans are expected to
    supply single-line text."""
    out = mg.apply_frontmatter(SAMPLE_FM, {"description": "line one\nline two"})
    fm, _ = mg.split_frontmatter(out)
    desc_lines = [ln for ln in fm if ln.startswith("description:")]
    assert len(desc_lines) == 1
    assert "\\n" in desc_lines[0]
    assert _fm_description(fm) == "line one\nline two"


def test_apply_frontmatter_replaces_old_block_scalar_description_without_orphaning_continuation():
    """D1 (material decision, see PR description): a description previously
    written as a YAML block scalar (`description: >` with indented
    continuation lines — the shape SKILL.md's own frontmatter uses) must be
    replaced WHOLE. Naively overwriting only the `description: >` line would
    leave its old continuation lines orphaned in the frontmatter block —
    corrupt YAML, and a violation of "every OTHER byte stays identical" for
    whatever field comes after it. This path is unreachable for
    status/last_verified/supersedes: this tool never writes any of them as a
    block scalar, so only description (free-text, and potentially written by
    a hand-edit or another tool before this ever runs) can hit it."""
    block_fm = (
        "---\n"
        "name: big-note\n"
        "description: >\n"
        "  A folded description that used\n"
        "  to span several lines like\n"
        "  SKILL.md's own frontmatter does.\n"
        "metadata:\n"
        "  type: project\n"
        "---\n"
        "\nBody text.\n"
    )
    out = mg.apply_frontmatter(block_fm, {"description": "One-line corrected summary"})
    assert 'description: "One-line corrected summary"\n' in out
    # Old continuation lines are GONE, not orphaned in the frontmatter block.
    assert "folded description" not in out
    assert "SKILL.md's own frontmatter" not in out
    # Sibling fields (before and after the replaced key) untouched.
    assert "name: big-note\n" in out
    assert "  type: project\n" in out
    assert out.endswith("\nBody text.\n")
    fm, _ = mg.split_frontmatter(out)
    assert _fm_description(fm) == "One-line corrected summary"
    assert any(ln == "  type: project\n" for ln in fm)


def test_apply_frontmatter_refuses_multiline_flow_scalar_description_rather_than_corrupt():
    """issue #186 review finding: a description written as a YAML
    FLOW (quoted) scalar folded across multiple lines has no `>`/`|` block
    indicator, so it does NOT match the one shape apply_frontmatter can
    safely consume whole. It must FAIL LOUD rather than silently overwrite
    only the first physical line and orphan the continuation as corrupt
    YAML — the original text must come back untouched (the caller never
    writes anything on this path; see the apply_plan-level proof below)."""
    flow_fm = (
        "---\n"
        "name: big-note\n"
        'description: "some text\n'
        '  continued"\n'
        "metadata:\n"
        "  type: project\n"
        "---\n"
        "\nBody text.\n"
    )
    with pytest.raises(ValueError, match="REFUSING"):
        mg.apply_frontmatter(flow_fm, {"description": "corrected one-liner"})


def test_apply_frontmatter_refuses_multiline_plain_scalar_description_rather_than_corrupt():
    """Same refusal for a PLAIN (unquoted) multi-line scalar continuing by
    indentation alone — even harder to bound correctly without a real YAML
    parser, since there is no delimiter marking where it ends at all."""
    plain_fm = (
        "---\n"
        "name: big-note\n"
        "description: some text\n"
        "  that continues here\n"
        "metadata:\n"
        "  type: project\n"
        "---\n"
        "\nBody text.\n"
    )
    with pytest.raises(ValueError, match="REFUSING"):
        mg.apply_frontmatter(plain_fm, {"description": "corrected one-liner"})


def test_apply_plan_refuses_rather_than_corrupts_a_multiline_flow_scalar_description(tmp_path):
    """Full-pipeline proof, not just the isolated splice: apply_plan raises
    BEFORE writing anything when the target's existing description is an
    unrecognizable multi-line shape — the file on disk is exactly as it was,
    matching this tool's report-first / refuse-over-corrupt design."""
    work = tmp_path / "memory"
    work.mkdir()
    bad = work / "weird.md"
    bad.write_text(
        "---\n"
        "name: weird\n"
        'description: "some text\n'
        '  continued"\n'
        "---\n"
        "\nBody.\n"
    )
    original = bad.read_text()
    sha = mg.file_sha256(str(bad))
    plan = {"files": [{
        "file": "weird.md", "verdict": "UPDATE", "sha256": sha,
        "frontmatter": {"description": "corrected"},
    }]}
    with pytest.raises(ValueError, match="REFUSING"):
        mg.apply_plan(plan, str(work), TODAY)
    assert bad.read_text() == original  # untouched


# ------------------------------------------------------------------ wikilinks

def test_extract_and_repair_wikilinks():
    text = "See [[old-mower-brand]] and [[hedge|the hedge note]]."
    assert mg.extract_wikilinks(text) == ["old-mower-brand", "hedge"]
    fixed = mg.repair_wikilinks(text, {"old-mower-brand": "archive/old-mower-brand"})
    assert "[[archive/old-mower-brand]]" in fixed
    # Alias preserved and untouched target left alone.
    assert "[[hedge|the hedge note]]" in fixed


def test_repair_wikilink_preserves_alias():
    text = "[[old-mower-brand|the mower]]"
    fixed = mg.repair_wikilinks(text, {"old-mower-brand": "archive/old-mower-brand"})
    assert fixed == "[[archive/old-mower-brand|the mower]]"


# ---------------------------------------------------------------------- index

def test_parse_and_rebuild_index():
    md = (
        "# Memory Index\n\n"
        "- [A](a.md) — old summary a\n"
        "- [B](b.md) — old summary b\n"
        "- [C](c.md) — keep me\n"
    )
    idx = mg.parse_index(md)
    assert set(idx) == {"a.md", "b.md", "c.md"}
    assert mg.index_summary(idx["a.md"]) == "old summary a"
    rebuilt = mg.rebuild_index(md, {"a.md": "- [A](a.md) — NEW a"}, {"b.md"})
    assert "- [A](a.md) — NEW a\n" in rebuilt
    assert "b.md" not in rebuilt
    assert "- [C](c.md) — keep me\n" in rebuilt


def test_rebuild_index_appends_line_for_unindexed_file():
    """An index_line for a file with NO existing MEMORY.md line must be added
    (after the last index line), not silently dropped."""
    md = (
        "# Memory Index\n\n"
        "- [A](a.md) — summary a\n"
        "\nTrailing non-index prose stays put.\n"
    )
    rebuilt = mg.rebuild_index(md, {"new.md": "- [New](new.md) — fresh"}, set())
    lines = rebuilt.splitlines()
    ai = lines.index("- [A](a.md) — summary a")
    assert lines[ai + 1] == "- [New](new.md) — fresh"
    assert rebuilt.endswith("Trailing non-index prose stays put.\n")


def test_rebuild_index_preserves_orphan_lines():
    """Index lines pointing at files that no longer exist are messy reality —
    rebuild must carry them through untouched (surfacing them is scan's job)."""
    md = "- [Ghost](ghost.md) — file long gone\n- [A](a.md) — a\n"
    rebuilt = mg.rebuild_index(md, {"a.md": "- [A](a.md) — new a"}, set())
    assert "- [Ghost](ghost.md) — file long gone\n" in rebuilt
    assert "- [A](a.md) — new a\n" in rebuilt


# ------------------------------------------------------------------------ age

def test_age_info_last_verified_beats_mtime(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("---\nlast_verified: 2026-01-01\n---\nbody\n")
    fm, _ = mg.split_frontmatter(p.read_text())
    days, src = mg.age_info(str(p), fm, TODAY)
    assert src == "last_verified"
    assert days == 188
    assert days > mg.UNVERIFIED_DAYS


def test_age_info_mtime_fallback(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("---\nname: x\n---\nbody\n")
    fm, _ = mg.split_frontmatter(p.read_text())
    days, src = mg.age_info(str(p), fm, TODAY)
    assert src == "mtime"
    assert days is not None


# -------------------------------------------------------------- compute_new

def test_compute_new_content_body_and_changelog():
    entry = {
        "verdict": "SUPERSEDED",
        "body": "\nNew single truth.\n",
        "changelog": ["step 1 — WRONG", "step 2 confirmed"],
        "frontmatter": {"status": "current"},
    }
    out = mg.compute_new_content(entry, SAMPLE_FM, TODAY)
    assert "New single truth." in out
    assert "### Changelog" in out
    assert "- step 1 — WRONG" in out
    # UPDATE/SUPERSEDED auto-stamps last_verified even if not in frontmatter dict.
    assert "last_verified: 2026-07-08\n" in out
    assert "status: current\n" in out
    # Original saga body is gone (replaced).
    assert "Recurring low pressure" in out  # description preserved
    # Idempotent-ish: does not double-append a Changelog heading.
    out2 = mg.compute_new_content(entry, out, TODAY)
    assert out2.count("### Changelog") == 1


def test_compute_new_content_keep_is_noop():
    entry = {"verdict": "KEEP"}
    out = mg.compute_new_content(entry, SAMPLE_FM, TODAY)
    assert out == SAMPLE_FM


# ------------------------------------------------------------ plan validation

def _write_plan(tmp_path, files):
    import json
    p = tmp_path / "plan.json"
    p.write_text(json.dumps({"files": files}))
    return str(p)


def test_validate_rejects_bad_verdict():
    with pytest.raises(ValueError):
        mg.validate_plan({"files": [{"file": "compost-bin-setup.md", "verdict": "NOPE"}]}, FIXTURE)


def test_validate_rejects_dead_without_reason():
    with pytest.raises(ValueError):
        mg.validate_plan({"files": [{"file": "old-mower-brand.md", "verdict": "DEAD"}]}, FIXTURE)


def test_validate_rejects_non_additive_frontmatter():
    with pytest.raises(ValueError):
        mg.validate_plan({"files": [{
            "file": "compost-bin-setup.md", "verdict": "UPDATE",
            "frontmatter": {"name": "hacked"}}]}, FIXTURE)


def test_validate_rejects_missing_file():
    with pytest.raises(ValueError):
        mg.validate_plan({"files": [{"file": "ghost.md", "verdict": "KEEP"}]}, FIXTURE)


def test_validate_rejects_duplicate():
    with pytest.raises(ValueError):
        mg.validate_plan({"files": [
            {"file": "compost-bin-setup.md", "verdict": "KEEP"},
            {"file": "compost-bin-setup.md", "verdict": "KEEP"}]}, FIXTURE)


# ------------------------------------------------------- stale-plan drift guard

def test_apply_refuses_stale_plan(tmp_path):
    """The report-review-apply gap can be hours. If a file changed on disk after
    the plan hash was recorded, apply must refuse — never silently overwrite."""
    import json
    plan = json.load(open(PLAN))
    work = tmp_path / "memory"
    shutil.copytree(FIXTURE, work)
    victim = work / "garden-hose-leak.md"
    victim.write_text(victim.read_text() + "\nNEWER FACT added after the report.\n")
    with pytest.raises(ValueError, match="STALE PLAN"):
        mg.apply_plan(plan, str(work), TODAY)
    # Refusal must happen before ANY mutation.
    assert "NEWER FACT" in victim.read_text()
    assert (work / "old-mower-brand.md").exists()  # DEAD move did not run


def test_apply_requires_hash_on_mutating_verdicts(tmp_path):
    work = tmp_path / "memory"
    shutil.copytree(FIXTURE, work)
    plan = {"files": [{"file": "compost-bin-setup.md", "verdict": "UPDATE",
                       "index_line": "[Compost](compost-bin-setup.md) — x"}]}
    with pytest.raises(ValueError, match="no sha256"):
        mg.apply_plan(plan, str(work), TODAY)
    # KEEP / UNVERIFIED>90d never mutate, so they need no hash.
    ok = {"files": [{"file": "compost-bin-setup.md", "verdict": "KEEP"}]}
    assert mg.apply_plan(ok, str(work), TODAY)["actions"] == []


def test_report_checks_provided_hashes_but_does_not_require():
    import json
    plan = json.load(open(PLAN))
    # Fixture plan hashes match the fixture files — report renders fine.
    assert "## Summary" in mg.render_report(plan, FIXTURE, TODAY)
    # A wrong hash fails even at report time.
    bad = {"files": [{"file": "compost-bin-setup.md", "verdict": "KEEP",
                      "sha256": "0" * 64}]}
    with pytest.raises(ValueError, match="STALE PLAN"):
        mg.render_report(bad, FIXTURE, TODAY)


def test_scan_emits_sha256_matching_disk():
    result = mg.scan(FIXTURE, TODAY)
    e = next(x for x in result["files"] if x["file"] == "compost-bin-setup.md")
    assert e["sha256"] == mg.file_sha256(os.path.join(FIXTURE, "compost-bin-setup.md"))


# ------------------------------------------------------------------- scanning

def test_scan_detects_pathologies():
    result = mg.scan(FIXTURE, TODAY)
    by = {e["file"]: e for e in result["files"]}
    assert result["dangling_links"] == []
    assert by["garden-hose-leak.md"]["signals"]["balloon_index"] is True
    assert by["garden-hose-leak.md"]["signals"]["correction_chain"] is True
    assert by["hedge-trimmer-schedule.md"]["signals"]["balloon_index"] is True
    assert by["patio-umbrella-warranty.md"]["signals"]["unverified_over_90d"] is True
    assert by["compost-bin-setup.md"]["signals"]["balloon_index"] is False
    # harness fields recovered
    assert by["garden-hose-leak.md"]["type"] == "project"
    # inbound links tracked (garden-hose is referenced by sprinkler-timer)
    assert "sprinkler-timer" in by["garden-hose-leak.md"]["inbound_links"]


# ---------------------------------------------------------- report == apply

def test_report_and_apply_agree_on_content(tmp_path):
    """The report IS the plan: content that report diffs must equal what apply
    writes. Verify compute_new_content is the single source for both."""
    import json
    plan = json.load(open(PLAN))
    work = tmp_path / "memory"
    shutil.copytree(FIXTURE, work)
    # capture proposed content for a rewritten file from the shared function
    entry = next(e for e in plan["files"] if e["file"] == "garden-hose-leak.md")
    old = (work / "garden-hose-leak.md").read_text()
    proposed = mg.compute_new_content(entry, old, TODAY)
    mg.apply_plan(plan, str(work), TODAY)
    written = (work / "garden-hose-leak.md").read_text()
    assert written == proposed


def test_full_apply_against_fixture(tmp_path):
    import json
    plan = json.load(open(PLAN))
    work = tmp_path / "memory"
    shutil.copytree(FIXTURE, work)
    result = mg.apply_plan(plan, str(work), TODAY)

    # DEAD archived, not deleted.
    assert not (work / "old-mower-brand.md").exists()
    assert (work / "archive" / "old-mower-brand.md").exists()
    tomb = (work / "archive" / "old-mower-brand.md").read_text()
    assert "status: archived" in tomb
    assert "# archived 2026-07-08:" in tomb

    # Inbound wikilink repaired to the archive path (no dangling link).
    hedge = (work / "hedge-trimmer-schedule.md").read_text()
    assert "[[archive/old-mower-brand]]" in hedge
    assert result["warnings"] == []

    # Index rebuilt: ballooned lines shrunk, dead line dropped.
    md = (work / "MEMORY.md").read_text()
    assert "old-mower-brand.md" not in md
    assert "cracked brass Y-splitter (fixed 2026-05-30)" in md
    assert "quarter when he plans the yard work weekends" not in md  # balloon gone

    # Additive frontmatter: metadata.type still readable everywhere it existed.
    fm, _ = mg.split_frontmatter((work / "garden-hose-leak.md").read_text())
    assert mg.read_metadata_field(fm, "type") == "project"
    assert mg.read_fm_field(fm, "supersedes") == '["sprinkler-timer"]'


# ------------------------------------------ description byte-diff regression (issue #177)
# The seam this row closes: "a gardener rewrite lands a corrected `description`
# field, byte-diff-proven for every untouched field." Both tests run the FULL
# apply_plan pipeline against a copied fixture dir (not just the isolated
# apply_frontmatter splice above) and prove byte-identity two ways: a
# line-by-line diff of the touched file, and a whole-directory sha256 snapshot
# of every sibling file.

def test_apply_plan_description_only_change_leaves_everything_else_byte_identical(tmp_path):
    work = tmp_path / "memory"
    shutil.copytree(FIXTURE, work)
    before = _dir_snapshot(work)
    target = work / "compost-bin-setup.md"
    old_lines = target.read_text().splitlines(keepends=True)
    desc_idx = next(i for i, l in enumerate(old_lines) if l.startswith("description:"))

    NEW_DESC = "Two-bin hot-compost rotation, CORRECTED: now three bins as of 2026-07"
    plan = {"files": [{
        "file": "compost-bin-setup.md",
        # KEEP: no verdict-triggered side effect (no auto last_verified stamp),
        # so the ONLY line that can legitimately change is description itself.
        "verdict": "KEEP",
        "rationale": "description drifted after a bin count change; nothing else about this note is wrong",
        "frontmatter": {"description": NEW_DESC},
    }]}
    result = mg.apply_plan(plan, str(work), TODAY)
    assert any("updated compost-bin-setup.md" in a for a in result["actions"])

    new_lines = target.read_text().splitlines(keepends=True)
    assert len(new_lines) == len(old_lines)  # replaced in place, nothing added/removed
    changed = [i for i in range(len(old_lines)) if old_lines[i] != new_lines[i]]
    assert changed == [desc_idx]
    assert new_lines[desc_idx] == 'description: "%s"\n' % NEW_DESC
    for i in range(len(old_lines)):
        if i != desc_idx:
            assert new_lines[i] == old_lines[i], "byte drift at untouched line %d" % i

    # Every OTHER file in the memory dir is untouched — whole-directory proof.
    after = _dir_snapshot(work)
    for path, h in before.items():
        if path == "compost-bin-setup.md":
            continue
        assert after[path] == h, "unexpected mutation outside the target file: %s" % path
    assert after["compost-bin-setup.md"] != before["compost-bin-setup.md"]


def test_apply_plan_superseded_rewrite_corrects_description_and_preserves_everything_else(tmp_path):
    """The real issue #177 scenario: a SUPERSEDED rewrite that
    replaces the body ALSO corrects a description the new body no longer
    supports, in the same apply — without touching name/metadata or any
    sibling file."""
    work = tmp_path / "memory"
    shutil.copytree(FIXTURE, work)
    before = _dir_snapshot(work)
    target = work / "garden-hose-leak.md"
    sha = mg.file_sha256(str(target))
    old_fm, _ = mg.split_frontmatter(target.read_text())
    old_name = mg.read_fm_field(old_fm, "name")
    old_type = mg.read_metadata_field(old_fm, "type")
    old_session = mg.read_metadata_field(old_fm, "originSessionId")

    NEW_DESC = "Far-spigot low pressure — RESOLVED 2026-05-30: cracked brass Y-splitter, replaced"
    plan = {"files": [{
        "file": "garden-hose-leak.md",
        "verdict": "SUPERSEDED",
        "sha256": sha,
        "rationale": "resolved saga; description described an open investigation the new body no longer supports",
        "frontmatter": {"status": "current", "description": NEW_DESC},
        "body": "\nFixed 2026-05-30: cracked brass Y-splitter at the far spigot, replaced.\n",
    }]}
    mg.apply_plan(plan, str(work), TODAY)

    new_fm, new_body = mg.split_frontmatter(target.read_text())
    assert mg.read_fm_field(new_fm, "description") == NEW_DESC
    assert "cracked brass Y-splitter at the far spigot, replaced" in new_body
    # Every OTHER frontmatter field untouched or correctly additive-stamped.
    assert mg.read_fm_field(new_fm, "name") == old_name
    assert mg.read_metadata_field(new_fm, "type") == old_type
    assert mg.read_metadata_field(new_fm, "originSessionId") == old_session
    assert mg.read_fm_field(new_fm, "status") == "current"
    assert mg.read_fm_field(new_fm, "last_verified") == TODAY.isoformat()

    # Every sibling file in the memory dir is completely untouched.
    after = _dir_snapshot(work)
    for path, h in before.items():
        if path == "garden-hose-leak.md":
            continue
        assert after[path] == h, "unexpected mutation outside the target file: %s" % path


def test_description_write_then_scan_round_trips_quotes_and_backslashes(tmp_path):
    """issue #186 review finding: `_serialize_value` JSON-quotes
    `description` on write, so `read_fm_field` must JSON-DECODE it on read
    (not just strip the surrounding quote characters) or a write->scan round
    trip on a description containing a quote/backslash silently returns the
    literal escape sequences instead of the real text. Exercises the FULL
    write (apply_plan) -> read (scan, which is what a real gardener run
    reads back) path, not just the two functions in isolation."""
    work = tmp_path / "memory"
    shutil.copytree(FIXTURE, work)
    target = work / "compost-bin-setup.md"
    tricky = r'Bin A holds "greens" (a \ separates prod/staging notes)'
    plan = {"files": [{
        "file": "compost-bin-setup.md",
        "verdict": "KEEP",
        "frontmatter": {"description": tricky},
    }]}
    mg.apply_plan(plan, str(work), TODAY)

    # scan() is what a real run reads the corrected value back through.
    result = mg.scan(str(work), TODAY)
    entry = next(e for e in result["files"] if e["file"] == "compost-bin-setup.md")
    assert entry["description"] == tricky

    # And directly via read_fm_field, the lower-level path scan uses.
    fm, _ = mg.split_frontmatter(target.read_text())
    assert mg.read_fm_field(fm, "description") == tricky


def test_read_fm_field_falls_back_on_double_quoted_but_invalid_json():
    """A value that merely LOOKS double-quoted but isn't valid JSON (e.g. an
    unescaped stray backslash from a hand edit) must degrade to the old bare
    quote-strip rather than raise — this is a read-only path over
    potentially hand-edited files, so graceful degradation beats a hard
    failure on scan."""
    fm = ['description: "trailing backslash \\"\n']
    # Not valid JSON (odd trailing backslash) — falls back to strip-quotes.
    assert mg.read_fm_field(fm, "description") == 'trailing backslash \\'


def test_render_report_smoke():
    import json
    plan = json.load(open(PLAN))
    text = mg.render_report(plan, FIXTURE, TODAY)
    for token in ("DEAD", "SUPERSEDED", "UPDATE", "UNVERIFIED>90d", "KEEP",
                  "## Summary", "```diff", "Wikilink repairs"):
        assert token in text


# =========================================================================== #
# Index-load-cost budget + topic-sharding proposal (read-only, schedulable)
# =========================================================================== #

# A garden-themed taxonomy so the invented fixture notes classify predictably —
# mirrors the real fleet/infra/automation/secrets default taxonomy in shape.
GARDEN_TAXONOMY = [
    ("watering", "Watering & irrigation", ["hose", "sprinkler", "spigot"]),
    ("tools", "Yard tools & equipment", ["mower", "trimmer", "hedge"]),
    ("compost", "Compost & soil", ["compost"]),
]


def _dir_snapshot(d):
    """Map every path under d to its bytes-hash — for proving nothing mutated."""
    import hashlib
    snap = {}
    for root, _, files in os.walk(d):
        for name in files:
            p = os.path.join(root, name)
            with open(p, "rb") as fh:
                snap[os.path.relpath(p, d)] = hashlib.sha256(fh.read()).hexdigest()
    return snap


# ------------------------------------------------------------- token estimate

def test_estimate_tokens_chars_over_four_ceil():
    assert mg.estimate_tokens("") == 0
    assert mg.estimate_tokens("a") == 1        # ceil(1/4)
    assert mg.estimate_tokens("abcd") == 1     # exactly 4 chars
    assert mg.estimate_tokens("abcde") == 2    # ceil(5/4)
    assert mg.estimate_tokens("x" * 400) == 100


def test_measure_text_counts():
    m = mg.measure_text("line one\nline two\n")
    assert m["chars"] == 18
    assert m["lines"] == 2
    assert m["est_tokens"] == mg.estimate_tokens("line one\nline two\n")


# ------------------------------------------------------------- classification

def test_classify_shard_first_keyword_wins_else_core():
    assert mg.classify_shard("garden-hose-leak", GARDEN_TAXONOMY) == "watering"
    assert mg.classify_shard("sprinkler-timer", GARDEN_TAXONOMY) == "watering"
    assert mg.classify_shard("old-mower-brand", GARDEN_TAXONOMY) == "tools"
    assert mg.classify_shard("hedge-trimmer-schedule", GARDEN_TAXONOMY) == "tools"
    assert mg.classify_shard("compost-bin-setup", GARDEN_TAXONOMY) == "compost"
    # Unmatched note stays in the always-loaded core.
    assert mg.classify_shard("patio-umbrella-warranty", GARDEN_TAXONOMY) is None


def test_default_taxonomy_classifies_real_topic_shapes():
    # Sanity on the shipped default taxonomy (anonymized real stems).
    assert mg.classify_shard("fleet-roadmap", mg.DEFAULT_SHARD_TAXONOMY) == "fleet"
    assert mg.classify_shard("infra-living-map", mg.DEFAULT_SHARD_TAXONOMY) == "infra"
    assert mg.classify_shard("ai-coding-tasks-pipeline", mg.DEFAULT_SHARD_TAXONOMY) == "automation"
    assert mg.classify_shard("op-tcc-appdata-gate", mg.DEFAULT_SHARD_TAXONOMY) == "secrets"
    assert mg.classify_shard("patio-umbrella", mg.DEFAULT_SHARD_TAXONOMY) is None


def test_default_taxonomy_classifies_generic_note_stems():
    tax = mg.DEFAULT_SHARD_TAXONOMY
    assert [name for name, _label, _keys in tax] == [
        "fleet", "infra", "automation", "secrets",
    ]
    assert mg.classify_shard("vpn-exit-node-setup", tax) == "infra"
    assert mg.classify_shard("media-server-update", tax) == "infra"
    assert mg.classify_shard("nas-backup-plan", tax) == "infra"
    assert mg.classify_shard("password-manager-migration", tax) == "secrets"
    assert mg.classify_shard("sync-folder-layout", tax) == "secrets"
    assert mg.classify_shard("podcast-feed-refresh", tax) == "automation"
    assert mg.classify_shard("streaming-library-index", tax) == "automation"
    assert mg.classify_shard("broker-trade-digest", tax) == "automation"


def test_default_taxonomy_keywords_do_not_match_inside_longer_words():
    tax = mg.DEFAULT_SHARD_TAXONOMY
    # Short generic words such as `feed` or `server` would capture these
    # unrelated stems, so the defaults carry no such keyword.
    for stem in (
        "feedback-self-review",
        "observer-cache",
        "breastfeed-support-notes",
        "home-serverless-migration",
    ):
        assert mg.classify_shard(stem, tax) is None
    # As whole tokens they classify, wherever they sit in the stem.
    for stem, shard in (
        ("rss-feed", "automation"),
        ("feed-refresh", "automation"),
        ("home-server", "infra"),
        ("server-maintenance", "infra"),
        ("home_server.setup", "infra"),
    ):
        assert mg.classify_shard(stem, tax) == shard


def test_multi_token_keyword_matches_a_contiguous_run_anywhere():
    tax = [("pipes", "Pipes", ["night-batch"])]
    assert mg.classify_shard("night-batch-notes", tax) == "pipes"
    assert mg.classify_shard("old-night-batch-notes", tax) == "pipes"
    assert mg.classify_shard("old-night-batch", tax) == "pipes"
    assert mg.classify_shard("night-old-batch", tax) is None
    assert mg.classify_shard("nights-batch", tax) is None


def test_default_taxonomy_has_zero_no_person_deny_hits():
    # Same grep flags as skills/topic/tests/test_public_vocabulary.py:_grep.
    blob = "\n".join(
        "\n".join((name, label, *keys))
        for name, label, keys in mg.DEFAULT_SHARD_TAXONOMY
    ).encode("utf-8")
    plant = "zz" + "no-such-taxonomy-term"
    plant_result = subprocess.run(
        ["grep", "-a", "-i", "-n", "-E", "-e", plant],
        input=blob,
        capture_output=True,
    )
    assert plant_result.returncode == 1, plant_result.stdout
    deny = os.environ.get("NO_PERSON_DENY", "").strip()
    if deny:
        probe = subprocess.run(
            ["grep", "-a", "-i", "-n", "-E", "-e", deny],
            input=b"",
            capture_output=True,
        )
        if probe.returncode == 2:
            detail = probe.stderr.decode("utf-8", "replace").strip()
            raise AssertionError(
                "NO_PERSON_DENY is not a valid extended regular expression: %s"
                % detail
            )
        result = subprocess.run(
            ["grep", "-a", "-i", "-n", "-E", "-e", deny],
            input=blob,
            capture_output=True,
        )
        assert result.returncode in (0, 1), result.stderr.decode(
            "utf-8", "replace"
        )
        hits = result.stdout.decode("utf-8", "replace").splitlines()
        assert not hits, (
            "NO_PERSON_DENY hits in default taxonomy:\n" + "\n".join(hits)
        )


# --------------------------------------------------------------- taxonomy I/O

def test_load_taxonomy_from_json(tmp_path):
    import json
    p = tmp_path / "tax.json"
    p.write_text(json.dumps({"shards": [
        {"name": "watering", "label": "Watering", "keywords": ["hose", "SPRINKLER"]},
        {"name": "tools", "keywords": ["mower"]},
    ]}))
    tax = mg.load_taxonomy(str(p))
    assert tax[0] == ("watering", "Watering", ["hose", "sprinkler"])  # lowercased
    assert tax[1] == ("tools", "tools", ["mower"])  # label defaults to name


def test_load_taxonomy_rejects_malformed(tmp_path):
    import json
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"shards": [{"name": "x"}]}))  # no keywords
    with pytest.raises(ValueError):
        mg.load_taxonomy(str(p))


def _write_taxonomy(path, name, keyword="stop-limit"):
    import json
    path.write_text(json.dumps({"shards": [
        {"name": name, "label": "%s taxonomy" % name, "keywords": [keyword]},
    ]}))


def _seed_stop_limit_store(tmp_path):
    work = tmp_path / "memory"
    work.mkdir()
    (work / "MEMORY.md").write_text(
        "# Memory Index\n\n"
        "- [Stop-limit notes](stop-limit.md) — Must follow the store taxonomy.\n")
    return work


def _run_budget_and_shard(work, capsys, explicit=None):
    common = ["--dir", str(work), "--today", "2026-07-08"]
    if explicit is not None:
        common += ["--taxonomy", str(explicit)]

    budget_rc = mg.main(
        ["budget", "--max-lines", "1", "--max-tokens", "100000"] + common)
    budget_io = capsys.readouterr()
    shard_rc = mg.main(["shard"] + common)
    shard_io = capsys.readouterr()
    return budget_rc, budget_io, shard_rc, shard_io


def test_taxonomy_precedence_explicit_wins_store_local(tmp_path, capsys):
    work = _seed_stop_limit_store(tmp_path)
    local = work / "shard-taxonomy.json"
    explicit = tmp_path / "explicit-taxonomy.json"
    _write_taxonomy(local, "local")
    _write_taxonomy(explicit, "explicit")

    budget_rc, budget_io, shard_rc, shard_io = _run_budget_and_shard(
        work, capsys, explicit)

    assert budget_rc == mg.OVER_BUDGET_EXIT
    assert shard_rc == 0
    source = "explicit --taxonomy: %s" % explicit
    assert "Taxonomy source: **%s**" % source in budget_io.out
    assert "Taxonomy source: %s" % source in shard_io.out
    assert "MEMORY.explicit.md" in budget_io.out
    assert "MEMORY.explicit.md" in shard_io.out
    assert "MEMORY.local.md" not in budget_io.out + shard_io.out


def test_taxonomy_precedence_store_local_wins_defaults_stop_limit_regression(
        tmp_path, capsys):
    work = _seed_stop_limit_store(tmp_path)
    local = work / "shard-taxonomy.json"
    _write_taxonomy(local, "mechanics")

    budget_rc, budget_io, shard_rc, shard_io = _run_budget_and_shard(
        work, capsys)

    assert budget_rc == mg.OVER_BUDGET_EXIT
    assert shard_rc == 0
    source = "store-local: %s" % local
    assert "Taxonomy source: **%s**" % source in budget_io.out
    assert "Taxonomy source: %s" % source in shard_io.out
    assert "MEMORY.mechanics.md" in budget_io.out
    assert "MEMORY.mechanics.md" in shard_io.out
    # The built-in "op-" substring must not misfile st[op-l]imit as secrets.
    assert "MEMORY.secrets.md" not in budget_io.out + shard_io.out


def test_taxonomy_precedence_defaults_only_when_no_file_exists(tmp_path, capsys):
    work = _seed_stop_limit_store(tmp_path)
    # A note the built-in defaults file under secrets by a whole-token keyword.
    with open(work / "MEMORY.md", "a") as index:
        index.write("- [Password vault](password-vault.md) — Defaults shard this.\n")

    budget_rc, budget_io, shard_rc, shard_io = _run_budget_and_shard(
        work, capsys)

    assert budget_rc == mg.OVER_BUDGET_EXIT
    assert shard_rc == 0
    assert "Taxonomy source: **built-in defaults**" in budget_io.out
    assert "Taxonomy source: built-in defaults" in shard_io.out
    assert "MEMORY.secrets.md" in budget_io.out
    assert "MEMORY.secrets.md" in shard_io.out


def test_taxonomy_precedence_malformed_store_local_refuses_without_fallback(
        tmp_path, capsys):
    work = _seed_stop_limit_store(tmp_path)
    local = work / "shard-taxonomy.json"
    local.write_text("{not valid JSON")

    budget_rc = mg.main([
        "budget", "--dir", str(work), "--max-lines", "1",
        "--today", "2026-07-08"])
    budget_io = capsys.readouterr()
    shard_rc = mg.main([
        "shard", "--dir", str(work), "--today", "2026-07-08"])
    shard_io = capsys.readouterr()

    assert budget_rc == 2
    assert shard_rc == 2
    assert "error:" in budget_io.err
    assert "error:" in shard_io.err
    assert "built-in defaults" not in (
        budget_io.out + budget_io.err + shard_io.out + shard_io.err)
    assert "MEMORY.secrets.md" not in budget_io.out + shard_io.out


# ------------------------------------------------------------ shard proposal

def test_propose_shards_groups_and_shrinks_core():
    md = mg._read(os.path.join(FIXTURE, "MEMORY.md"))
    prop = mg.propose_shards(md, GARDEN_TAXONOMY)
    shards = {s["name"]: s for s in prop["shards"]}
    assert set(shards) == {"watering", "tools", "compost"}
    assert shards["watering"]["count"] == 2   # hose + sprinkler
    assert shards["tools"]["count"] == 2       # mower + hedge-trimmer
    assert shards["compost"]["count"] == 1
    assert shards["watering"]["file"] == "MEMORY.watering.md"
    # One unmatched note (patio umbrella) stays in the core.
    assert prop["core_kept_notes"] == 1
    # A JIT pointer per non-empty shard, each naming its shard file.
    assert len(prop["pointers"]) == 3
    for s in prop["shards"]:
        assert any(s["file"] in ptr and "load JIT" in ptr for ptr in prop["pointers"])
    # The core index preview is much cheaper than the whole index (the point).
    assert prop["core_measure"]["est_tokens"] < mg.measure_text(md)["est_tokens"]
    # No note is lost: every shard bullet + core bullets == original bullets.
    total = sum(s["count"] for s in prop["shards"]) + prop["core_kept_notes"]
    assert total == len(mg.parse_index(md))


def test_shard_pointers_appear_in_core_preview():
    md = mg._read(os.path.join(FIXTURE, "MEMORY.md"))
    prop = mg.propose_shards(md, GARDEN_TAXONOMY)
    for ptr in prop["pointers"]:
        assert ptr in prop["core_index_preview"]
    # The core header (title) is carried through.
    assert "# Memory Index" in prop["core_index_preview"]


# --------------------------------------------------------------- compute_budget

def test_compute_budget_under_is_clean_no_proposal():
    res = mg.compute_budget(FIXTURE, max_tokens=100000, max_lines=100000,
                            taxonomy=GARDEN_TAXONOMY, today=TODAY)
    assert res["over_budget"] is False
    assert res["over_tokens"] is False and res["over_lines"] is False
    assert res["proposal"] is None
    assert res["index_entries"] == len(mg.parse_index(mg._read(
        os.path.join(FIXTURE, "MEMORY.md"))))


def test_compute_budget_over_tokens_attaches_proposal():
    res = mg.compute_budget(FIXTURE, max_tokens=10, max_lines=100000,
                            taxonomy=GARDEN_TAXONOMY, today=TODAY)
    assert res["over_budget"] is True
    assert res["over_tokens"] is True and res["over_lines"] is False
    assert res["proposal"] is not None
    assert res["proposal"]["shards"]


def test_compute_budget_over_lines_only():
    res = mg.compute_budget(FIXTURE, max_tokens=100000, max_lines=1,
                            taxonomy=GARDEN_TAXONOMY, today=TODAY)
    assert res["over_budget"] is True
    assert res["over_lines"] is True and res["over_tokens"] is False
    assert res["proposal"] is not None


def test_compute_budget_missing_memory_md(tmp_path):
    with pytest.raises(ValueError, match="no MEMORY.md"):
        mg.compute_budget(str(tmp_path), 2500, 40, GARDEN_TAXONOMY, TODAY)


# ------------------------------------------------------------ alert + report

def test_budget_alert_line_over_and_under():
    over = mg.compute_budget(FIXTURE, 10, 100000, GARDEN_TAXONOMY, TODAY)
    assert "OVER BUDGET" in mg.budget_alert_line(over)
    assert "run the memory gardener" in mg.budget_alert_line(over)
    under = mg.compute_budget(FIXTURE, 100000, 100000, GARDEN_TAXONOMY, TODAY)
    assert "within budget" in mg.budget_alert_line(under)


def test_render_budget_report_over_has_tables_and_pointers():
    res = mg.compute_budget(FIXTURE, 10, 100000, GARDEN_TAXONOMY, TODAY)
    text = mg.render_budget_report(res)
    for token in ("load-cost budget check", "OVER BUDGET", "## Current cost",
                  "## Proposed topic-sharding", "MEMORY.watering.md",
                  "load JIT", "supervised", "```markdown"):
        assert token in text


def test_render_budget_report_under_says_no_sharding():
    res = mg.compute_budget(FIXTURE, 100000, 100000, GARDEN_TAXONOMY, TODAY)
    text = mg.render_budget_report(res)
    assert "within budget" in text
    assert "no sharding proposed" in text
    assert "Proposed topic-sharding" not in text


# ------------------------------------------------- READ-ONLY / schedulable proof

def test_budget_never_mutates_memory_dir(tmp_path):
    """The schedulable check must be provably read-only: measuring an OVER-budget
    dir leaves every file byte-identical and creates no shard/archive files."""
    work = tmp_path / "memory"
    shutil.copytree(FIXTURE, work)
    before = _dir_snapshot(str(work))
    res = mg.compute_budget(str(work), 10, 1, GARDEN_TAXONOMY, TODAY)  # over on both
    mg.render_budget_report(res)
    after = _dir_snapshot(str(work))
    assert before == after                      # nothing changed, nothing added
    assert not (work / "MEMORY.watering.md").exists()   # proposal only, not written
    assert not (work / "archive").exists()


def test_budget_subcommand_has_no_apply_or_plan_flag():
    """budget carries no mutating surface — no --plan, no --apply."""
    import io
    import contextlib
    # Unknown flags make argparse exit(2); capture to keep the suite quiet.
    for bad in (["budget", "--dir", FIXTURE, "--apply"],
                ["budget", "--dir", FIXTURE, "--plan", "x.json"]):
        with pytest.raises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                mg.main(bad)


def test_main_budget_exit_code_over_vs_under(tmp_path, capsys):
    work = tmp_path / "memory"
    shutil.copytree(FIXTURE, work)
    tax = tmp_path / "tax.json"
    tax.write_text(_dumps_garden_taxonomy())
    # Over-budget -> distinct OVER_BUDGET_EXIT (not the error code 2).
    rc_over = mg.main(["budget", "--dir", str(work), "--max-tokens", "10",
                       "--taxonomy", str(tax), "--today", "2026-07-08"])
    assert rc_over == mg.OVER_BUDGET_EXIT
    assert mg.OVER_BUDGET_EXIT != 2
    # Under-budget -> clean 0.
    rc_under = mg.main(["budget", "--dir", str(work), "--max-tokens", "100000",
                        "--max-lines", "100000", "--taxonomy", str(tax),
                        "--today", "2026-07-08"])
    assert rc_under == 0
    # And the copied dir is still untouched by both runs.
    assert (work / "MEMORY.md").read_text() == open(
        os.path.join(FIXTURE, "MEMORY.md")).read()


def test_main_budget_json_is_valid(tmp_path, capsys):
    import json
    work = tmp_path / "memory"
    shutil.copytree(FIXTURE, work)
    tax = tmp_path / "tax.json"
    tax.write_text(_dumps_garden_taxonomy())
    mg.main(["budget", "--dir", str(work), "--max-tokens", "10",
             "--taxonomy", str(tax), "--json", "--today", "2026-07-08"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["over_budget"] is True
    assert payload["proposal"]["shards"]


def _dumps_garden_taxonomy():
    import json
    return json.dumps({"shards": [
        {"name": n, "label": l, "keywords": k} for n, l, k in GARDEN_TAXONOMY]})


# =========================================================================== #
# codex round-2 findings: no-loss preservation, taxonomy validation gaps,
# non-positive budget ceilings (all must be clean refusals, never tracebacks
# or silently-wrong accounting; the read-only/no-`--apply` posture is unchanged)
# =========================================================================== #

def test_propose_shards_preserves_interleaved_non_index_content():
    """Headings/prose/comments/malformed-bullets that appear AFTER the first
    index bullet must survive in the proposal — not be silently dropped."""
    text = (
        "# Memory Index\n"
        "\n"
        "- [Garden hose leak](garden-hose-leak.md) — leak note\n"
        "\n"
        "## Non-index heading after first bullet\n"
        "\n"
        "Some prose paragraph here that isn't a bullet.\n"
        "\n"
        "<!-- a comment -->\n"
        "\n"
        "- not a real bullet (malformed, no link)\n"
        "\n"
        "- [Old mower brand](old-mower-brand.md) — mower note\n"
    )
    prop = mg.propose_shards(text, GARDEN_TAXONOMY)

    for must_have in (
        "## Non-index heading after first bullet",
        "Some prose paragraph here that isn't a bullet.",
        "<!-- a comment -->",
        "- not a real bullet (malformed, no link)",
    ):
        assert must_have in prop["core_index_preview"], must_have

    # Explicit accounting, not silent absorption.
    assert prop["non_index_lines_retained"] == 9

    # No-loss invariant: header (2 lines, both before the first bullet) +
    # shard lines + retained core-kept bullets + retained non-index lines ==
    # every line in the original text.
    shard_line_count = sum(s["count"] for s in prop["shards"])
    total_accounted = (2 + shard_line_count
                       + prop["core_kept_notes"] + prop["non_index_lines_retained"])
    assert total_accounted == len(text.splitlines())

    # Both notes still classify correctly despite the interleaving.
    assert {s["name"] for s in prop["shards"]} == {"watering", "tools"}


def test_propose_shards_no_loss_on_real_fixture():
    """Same no-loss invariant against the real (garden-themed) fixture, which
    has blank-line-separated bullets but no interleaved prose — regression
    guard so the fix doesn't change behavior on the common case."""
    md = mg._read(os.path.join(FIXTURE, "MEMORY.md"))
    prop = mg.propose_shards(md, GARDEN_TAXONOMY)
    shard_line_count = sum(s["count"] for s in prop["shards"])
    assert shard_line_count + prop["core_kept_notes"] == len(mg.parse_index(md))
    # Blank lines between fixture bullets are non-index content after the
    # first bullet — they're retained, not silently dropped.
    assert prop["non_index_lines_retained"] >= 0


# --------------------------------------------------------------- taxonomy validation

def test_load_taxonomy_rejects_missing_shards_key(tmp_path):
    import json
    p = tmp_path / "no_shards_key.json"
    p.write_text(json.dumps({"not_shards": []}))
    with pytest.raises(ValueError):
        mg.load_taxonomy(str(p))


def test_load_taxonomy_rejects_non_object_shard_entry(tmp_path):
    import json
    p = tmp_path / "bad_entry.json"
    p.write_text(json.dumps({"shards": [
        "just-a-string", {"name": "x", "keywords": ["y"]}]}))
    with pytest.raises(ValueError):
        mg.load_taxonomy(str(p))


def test_load_taxonomy_rejects_duplicate_shard_names(tmp_path):
    import json
    p = tmp_path / "dup.json"
    p.write_text(json.dumps({"shards": [
        {"name": "watering", "keywords": ["hose"]},
        {"name": "watering", "keywords": ["sprinkler"]},
    ]}))
    with pytest.raises(ValueError):
        mg.load_taxonomy(str(p))


def test_load_taxonomy_rejects_empty_string_keyword():
    """An empty-string keyword would substring-match every filename stem
    (`"" in s` is always True) — must be rejected, not silently accepted."""
    import json
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "empty_kw.json")
        with open(p, "w") as f:
            json.dump({"shards": [
                {"name": "everything", "keywords": ["hose", ""]}]}, f)
        with pytest.raises(ValueError):
            mg.load_taxonomy(p)


def test_main_budget_bad_taxonomy_is_clean_exit_2_not_traceback(tmp_path):
    import json
    work = tmp_path / "memory"
    shutil.copytree(FIXTURE, work)
    bad = tmp_path / "bad_tax.json"
    bad.write_text(json.dumps({"not_shards": []}))
    rc = mg.main(["budget", "--dir", str(work), "--taxonomy", str(bad),
                 "--today", "2026-07-08"])
    assert rc == 2


# --------------------------------------------------------- non-positive ceilings

def test_compute_budget_rejects_non_positive_ceilings():
    with pytest.raises(ValueError):
        mg.compute_budget(FIXTURE, max_tokens=-1, max_lines=40,
                          taxonomy=GARDEN_TAXONOMY, today=TODAY)
    with pytest.raises(ValueError):
        mg.compute_budget(FIXTURE, max_tokens=2500, max_lines=0,
                          taxonomy=GARDEN_TAXONOMY, today=TODAY)
    with pytest.raises(ValueError):
        mg.compute_budget(FIXTURE, max_tokens=0, max_lines=40,
                          taxonomy=GARDEN_TAXONOMY, today=TODAY)


def test_main_budget_rejects_negative_ceiling_exit_2_not_bogus_alert(tmp_path):
    """A negative ceiling used to make even an empty MEMORY.md 'over budget'.
    It must now be a clean config-error refusal (exit 2), never a fabricated
    over-budget verdict and never a traceback."""
    work = tmp_path / "memory"
    work.mkdir()
    (work / "MEMORY.md").write_text("")  # empty index
    rc = mg.main(["budget", "--dir", str(work), "--max-tokens", "-5",
                 "--today", "2026-07-08"])
    assert rc == 2
    assert rc != mg.OVER_BUDGET_EXIT


# =========================================================================== #
# shard — EXECUTE the topic-split (dry-run default, --apply sha256-guarded).
# The writing sibling of `budget`: budget proposes, shard writes. These cover
# the plan==budget-proposal parity, the no-loss invariant against the real
# written files, note-file byte-identity, the drift/blind-apply/clobber guards,
# and the on-disk backup.
# =========================================================================== #

def _bullets(text):
    """Index bullet lines only (`- [Title](file.md) — ...`)."""
    return [ln for ln in text.splitlines() if mg.INDEX_LINE_RE.match(ln)]


def _seed(tmp_path):
    """A fresh writable copy of the fixture memory dir + its MEMORY.md sha."""
    work = tmp_path / "memory"
    shutil.copytree(FIXTURE, work)
    sha = mg.file_sha256(str(work / "MEMORY.md"))
    return work, sha


# --------------------------------------------- (a) dry-run == budget proposal

def test_shard_dryrun_matches_budget_proposal():
    """The dry-run must reuse propose_shards so it is byte-identical to what
    `budget` already proposes for the same dir — no re-derivation."""
    text, prop, files = mg.build_shard_layout(FIXTURE, GARDEN_TAXONOMY)
    budget = mg.compute_budget(FIXTURE, max_tokens=10, max_lines=100000,
                               taxonomy=GARDEN_TAXONOMY, today=TODAY)
    assert prop == budget["proposal"]              # same proposal object
    by_file = dict(files)
    # Core file content is exactly the proposal's core preview.
    assert by_file["MEMORY.md"] == prop["core_index_preview"]
    # Each shard file is exactly propose_shards' shard_file_body.
    for s in prop["shards"]:
        assert by_file[s["file"]] == mg.shard_file_body(s["label"], s["lines"])
        assert by_file[s["file"]].startswith("# Memory — %s\n" % s["label"])


def test_shard_dryrun_writes_nothing(tmp_path):
    work, _ = _seed(tmp_path)
    before = _dir_snapshot(str(work))
    tax = tmp_path / "tax.json"
    tax.write_text(_dumps_garden_taxonomy())
    rc = mg.main(["shard", "--dir", str(work), "--taxonomy", str(tax),
                  "--today", "2026-07-11"])
    assert rc == 0
    assert _dir_snapshot(str(work)) == before          # nothing written
    assert not (work / "MEMORY.watering.md").exists()
    assert not (work / "archive").exists()


# ------------------------------------------ (b) apply writes + no-loss reproduce

def test_shard_apply_writes_files_and_reproduces_every_bullet(tmp_path):
    work, sha = _seed(tmp_path)
    original_bullets = _bullets((work / "MEMORY.md").read_text())

    res = mg.execute_shard(str(work), GARDEN_TAXONOMY, TODAY,
                           expect_sha256=sha, force=False)
    assert any("rewrote MEMORY.md" in a for a in res["actions"])
    assert res["after_tokens"] < res["before_tokens"]

    # Shard files written with the expected note counts.
    assert (work / "MEMORY.watering.md").exists()
    assert (work / "MEMORY.tools.md").exists()
    assert (work / "MEMORY.compost.md").exists()

    # No-loss against the ACTUAL written files: {new core (minus JIT pointers)
    # + all shard files} reproduces every original bullet, exactly.
    prop = mg.propose_shards(
        mg._read(str(work / "archive" / mg._shard_backup_name(TODAY))),
        GARDEN_TAXONOMY)
    pointer_set = set(prop["pointers"])
    produced = []
    for name in ("MEMORY.md", "MEMORY.watering.md", "MEMORY.tools.md",
                 "MEMORY.compost.md"):
        for b in _bullets((work / name).read_text()):
            if name == "MEMORY.md" and b in pointer_set:
                continue
            produced.append(b)
    assert sorted(produced) == sorted(original_bullets)

    # New core carries the JIT pointers + the unmatched note, but not the
    # sharded bullets.
    core = (work / "MEMORY.md").read_text()
    assert "MEMORY.watering.md" in core
    assert "patio-umbrella-warranty.md" in core       # unmatched -> stays core
    assert "garden-hose-leak.md" not in core          # moved into a shard


def test_shard_apply_via_main_cli(tmp_path):
    work, sha = _seed(tmp_path)
    tax = tmp_path / "tax.json"
    tax.write_text(_dumps_garden_taxonomy())
    rc = mg.main(["shard", "--dir", str(work), "--taxonomy", str(tax),
                  "--apply", "--expect-sha256", sha, "--today", "2026-07-11"])
    assert rc == 0
    assert (work / "MEMORY.tools.md").exists()


# ------------------------------------------------ (c) note files byte-identical

def test_shard_apply_leaves_note_files_byte_identical(tmp_path):
    """Sharding touches ONLY index files. Every real note *.md must be
    byte-identical before/after; only MEMORY.md changes and the new
    MEMORY.<shard>.md + the archive backup appear."""
    work, sha = _seed(tmp_path)
    notes = [n for n in os.listdir(work) if n.endswith(".md") and n != "MEMORY.md"]
    before = {n: (work / n).read_bytes() for n in notes}

    mg.execute_shard(str(work), GARDEN_TAXONOMY, TODAY,
                     expect_sha256=sha, force=False)

    for n in notes:
        assert (work / n).read_bytes() == before[n], n   # untouched
    # Exactly the expected new artifacts appeared; nothing was deleted.
    now = set(os.listdir(work))
    assert now == set(notes) | {"MEMORY.md", "MEMORY.watering.md",
                                "MEMORY.tools.md", "MEMORY.compost.md", "archive"}


# ------------------------------------------------ (d) drift / blind-apply guards

def test_shard_apply_refuses_without_expect_sha256(tmp_path):
    work, _ = _seed(tmp_path)
    before = _dir_snapshot(str(work))
    with pytest.raises(ValueError, match="requires --expect-sha256"):
        mg.execute_shard(str(work), GARDEN_TAXONOMY, TODAY,
                         expect_sha256=None, force=False)
    assert _dir_snapshot(str(work)) == before          # nothing written


def test_shard_apply_refuses_on_stale_hash(tmp_path):
    work, _ = _seed(tmp_path)
    before = _dir_snapshot(str(work))
    with pytest.raises(ValueError, match="STALE PLAN"):
        mg.execute_shard(str(work), GARDEN_TAXONOMY, TODAY,
                         expect_sha256="0" * 64, force=False)
    assert _dir_snapshot(str(work)) == before          # byte-identical


def test_main_shard_apply_wrong_hash_exit_2(tmp_path):
    work, _ = _seed(tmp_path)
    tax = tmp_path / "tax.json"
    tax.write_text(_dumps_garden_taxonomy())
    before = _dir_snapshot(str(work))
    rc = mg.main(["shard", "--dir", str(work), "--taxonomy", str(tax), "--apply",
                  "--expect-sha256", "0" * 64, "--today", "2026-07-11"])
    assert rc == 2
    assert _dir_snapshot(str(work)) == before


# ------------------------------------------------- (e) no silent clobber / force

def test_shard_apply_refuses_preexisting_shard_without_force(tmp_path):
    work, sha = _seed(tmp_path)
    (work / "MEMORY.watering.md").write_text("# pre-existing, do not clobber\n")
    before = _dir_snapshot(str(work))
    with pytest.raises(ValueError, match="refusing to overwrite"):
        mg.execute_shard(str(work), GARDEN_TAXONOMY, TODAY,
                         expect_sha256=sha, force=False)
    assert _dir_snapshot(str(work)) == before          # untouched, no backup

    # --force overrides it and completes the split.
    res = mg.execute_shard(str(work), GARDEN_TAXONOMY, TODAY,
                           expect_sha256=sha, force=True)
    assert any("wrote MEMORY.watering.md" in a for a in res["actions"])
    assert "# Memory — Watering & irrigation" in (work / "MEMORY.watering.md").read_text()


# ------------------------------------------------------------- (f) backup made

def test_shard_apply_backs_up_memory_md(tmp_path):
    work, sha = _seed(tmp_path)
    original = (work / "MEMORY.md").read_text()
    mg.execute_shard(str(work), GARDEN_TAXONOMY, TODAY,
                     expect_sha256=sha, force=False)
    backup = work / "archive" / "MEMORY.pre-shard-2026-07-08.md"
    assert backup.exists()
    assert backup.read_text() == original              # exact pre-split copy


# --------------------------------------------------- no-loss invariant is armed

def test_check_no_loss_raises_on_dropped_bullet():
    """A layout that drops a bullet must be rejected before any write."""
    md = mg._read(os.path.join(FIXTURE, "MEMORY.md"))
    _text, prop, files = mg.build_shard_layout(FIXTURE, GARDEN_TAXONOMY)
    # Corrupt a shard file's content by removing its bullets — simulate loss.
    broken = []
    for rel, content in files:
        if rel == "MEMORY.watering.md":
            broken.append((rel, "# Memory — Watering & irrigation\n\n"))  # bullets gone
        else:
            broken.append((rel, content))
    with pytest.raises(ValueError, match="NO-LOSS INVARIANT VIOLATED"):
        mg.check_no_loss(md, prop, broken)


def test_shard_no_shards_dryrun_is_noop_and_apply_refuses(tmp_path):
    """With a taxonomy that matches nothing, the dry run says so (exit 0) and
    --apply refuses (there is nothing to split)."""
    work, sha = _seed(tmp_path)
    tax = tmp_path / "tax.json"
    tax.write_text(_dumps_garden_taxonomy().replace("hose", "zzz")
                   .replace("sprinkler", "zzz").replace("spigot", "zzz")
                   .replace("mower", "zzz").replace("trimmer", "zzz")
                   .replace("hedge", "zzz").replace("compost", "zzz"))
    rc = mg.main(["shard", "--dir", str(work), "--taxonomy", str(tax),
                  "--today", "2026-07-11"])
    assert rc == 0                                     # dry-run no-op
    with pytest.raises(ValueError, match="nothing to split"):
        no_tax = mg.load_taxonomy(str(tax))
        mg.execute_shard(str(work), no_tax, TODAY, expect_sha256=sha, force=False)


# =========================================================================== #
# Adversarial-review round-2: re-application data-loss defects. All five must
# hold WHILE the first-run behavior + existing tests stay green.
# =========================================================================== #

# -------- (1) re-shard against an already-sharded dir REFUSES, loses nothing

def test_reshard_of_already_sharded_core_refuses_and_loses_nothing(tmp_path):
    """The exact scenario that silently lost 16 notes: after a first split, the
    core holds JIT pointers whose stems keyword-match their own topic. A SECOND
    --apply must REFUSE (not rewrite a shard file down to one pointer), leaving
    the dir byte-identical and every note intact."""
    work, sha = _seed(tmp_path)
    # First split (legitimate).
    mg.execute_shard(str(work), GARDEN_TAXONOMY, TODAY,
                     expect_sha256=sha, force=False)
    watering_before = (work / "MEMORY.watering.md").read_bytes()
    assert watering_before.count(b"- [") == 2          # 2 real notes present

    after_first = _dir_snapshot(str(work))
    new_core_sha = mg.file_sha256(str(work / "MEMORY.md"))  # the sharded core

    # Second --apply with the CURRENT (sharded) core hash so we get past the
    # drift guard and hit the re-shard refusal specifically.
    with pytest.raises(ValueError, match="ALREADY SHARDED"):
        mg.execute_shard(str(work), GARDEN_TAXONOMY, TODAY,
                         expect_sha256=new_core_sha, force=False)
    # And --force must NOT override it.
    with pytest.raises(ValueError, match="ALREADY SHARDED"):
        mg.execute_shard(str(work), GARDEN_TAXONOMY, TODAY,
                         expect_sha256=new_core_sha, force=True)

    assert _dir_snapshot(str(work)) == after_first     # nothing changed
    assert (work / "MEMORY.watering.md").read_bytes() == watering_before
    assert (work / "MEMORY.watering.md").read_bytes().count(b"- [") == 2


def test_main_reshard_exit_2_via_cli(tmp_path):
    work, sha = _seed(tmp_path)
    tax = tmp_path / "tax.json"
    tax.write_text(_dumps_garden_taxonomy())
    mg.main(["shard", "--dir", str(work), "--taxonomy", str(tax), "--apply",
             "--expect-sha256", sha, "--today", "2026-07-08"])
    new_core_sha = mg.file_sha256(str(work / "MEMORY.md"))
    before = _dir_snapshot(str(work))
    rc = mg.main(["shard", "--dir", str(work), "--taxonomy", str(tax), "--apply",
                  "--expect-sha256", new_core_sha, "--force", "--today", "2026-07-08"])
    assert rc == 2                                     # refused
    assert _dir_snapshot(str(work)) == before          # byte-identical


# -------- (2) generated-pointer-shaped line is NEVER classified into a shard

def test_shard_pointer_line_never_classified_into_a_shard():
    """A bullet linking to MEMORY.<topic>.md (or carrying the JIT suffix) is a
    pointer, not a note — propose_shards must never fold it into a shard even
    when its stem keyword-matches the taxonomy."""
    # DEFAULT taxonomy: `MEMORY.fleet` would match the "fleet" keyword.
    pointer = ("- [MEMORY.fleet.md](MEMORY.fleet.md) — Fleet / Overview · "
               "11 notes · ~500 tok · load JIT when the topic comes up")
    text = ("# Memory Index\n\n%s\n- [Notes](notes.md) — meta repo\n"
            % pointer)
    prop = mg.propose_shards(text, mg.DEFAULT_SHARD_TAXONOMY)
    # No shard captured the pointer.
    for s in prop["shards"]:
        assert pointer not in s["lines"]
    assert not any(mg._is_shard_pointer_line(ln)
                   for s in prop["shards"] for ln in s["lines"])
    # The pointer is retained in the core, and it isn't counted as a note.
    assert pointer in prop["core_index_preview"]

    # The detector itself: pointers True, a real note bullet False.
    assert mg._is_shard_pointer_line(pointer) is True
    assert mg._is_shard_pointer_line(
        "- [Garden hose leak](garden-hose-leak.md) — leak note") is False


# =========================================================================== #
# issue #183 finding 2 (BP9) — _is_shard_pointer_line's second signal
# (a bullet that isn't a MEMORY.<name>.md link) must key off a SENTINEL only
# the generator itself writes, not a substring of JIT_POINTER_SUFFIX's
# human-readable text. Before this fix, a REAL note bullet whose own summary
# happened to contain that exact phrase verbatim was misclassified as a
# pointer and silently excluded from BOTH sides of the no-loss comparison.
# The FIRST signal (the link target literally matching MEMORY.<name>.md,
# SHARD_TARGET_RE) is already structural and untouched — every real,
# on-disk generated pointer line satisfies it regardless of this fix, so no
# existing fixture needed to change. Named seam: the producer/consumer pair
# _shard_pointer (writes) / _is_shard_pointer_line (reads) — the "pointer
# line" concept's own interface — plus check_extend_no_loss's observable
# behavior, which is what the false-positive actually endangers. No mocking:
# real strings through the real functions.
# =========================================================================== #

def test_is_shard_pointer_line_does_not_misclassify_a_real_note_containing_the_suffix_text():
    """A real note bullet — NOT a link to a MEMORY.<name>.md shard file —
    whose own summary happens to contain the literal JIT_POINTER_SUFFIX
    phrase verbatim must NOT be classified as a pointer. Only a line the
    generator itself produced (carrying its sentinel) may match on
    text alone."""
    coincidental = ("- [Sprinkler oddity](sprinkler-oddity.md) — the harness "
                    "docs say to load JIT when the topic comes up, purely "
                    "coincidental prose, not a generated pointer")
    assert mg.JIT_POINTER_SUFFIX in coincidental          # the trap is real
    assert mg._is_shard_pointer_line(coincidental) is False

    # A GENERATED pointer (carrying the sentinel) is still correctly caught —
    # the fix narrows the second signal, it doesn't remove it.
    generated = mg._shard_pointer("watering", "Watering & irrigation", 1, 20)
    assert mg._is_shard_pointer_line(generated) is True
    # And the first (structural, link-target) signal alone still catches an
    # ordinary generated pointer even with the suffix text stripped off —
    # proving the two signals are genuinely independent.
    link_only = "- [MEMORY.watering.md](MEMORY.watering.md) — Watering"
    assert mg._is_shard_pointer_line(link_only) is True


def test_propose_shards_classifies_a_real_note_whose_summary_contains_the_suffix_text():
    """Concrete before/after: propose_shards must fold such a note into its
    matching shard (by filename-stem keyword, same as any other note) — not
    leave it stranded, unclassified, in the core body the way a
    misclassified pointer would be."""
    text = ("# Memory Index\n\n"
            "- [Sprinkler oddity](sprinkler-oddity.md) — a real note; the "
            "harness docs say to load JIT when the topic comes up, purely "
            "coincidental prose\n")
    prop = mg.propose_shards(text, GARDEN_TAXONOMY)
    watering = next(s for s in prop["shards"] if s["name"] == "watering")
    assert any("sprinkler-oddity.md" in ln for ln in watering["lines"])
    assert prop["core_kept_notes"] == 0


def test_check_extend_no_loss_still_catches_a_dropped_note_whose_summary_contains_suffix_text():
    """Adversarial: BEFORE this fix, _is_shard_pointer_line's text-substring
    signal excluded a coincidental-phrase note from BOTH sides of
    check_extend_no_loss's comparison (original AND produced), so a bug that
    silently dropped it — landed in neither the new core nor any shard —
    went uncaught (both multisets empty, "balanced"). After the fix, the
    note is compared like any other note, and a drop IS caught."""
    coincidental = ("- [Sprinkler oddity](sprinkler-oddity.md) — the harness "
                    "docs say to load JIT when the topic comes up, purely "
                    "coincidental prose")
    core_before = "# Memory Index\n\n%s\n" % coincidental
    core_after_dropped = "# Memory Index\n\n"        # the note vanished, landed nowhere
    with pytest.raises(ValueError, match="NO-LOSS INVARIANT VIOLATED"):
        mg.check_extend_no_loss(
            [("MEMORY.md", core_before)],
            [("MEMORY.md", core_after_dropped)])


def test_is_shard_pointer_line_does_not_misclassify_a_note_with_a_stray_sentinel_mid_prose():
    """Adversarial follow-up (post-merge review of this same fix): a bare
    `JIT_POINTER_SENTINEL in ln` substring check is STILL a text-content
    match, just against a different string than before — a real note whose
    body legitimately contains a stray U+2063 ANYWHERE (plausible: PDF-
    extraction copy/paste, some ICU/office-doc pipelines emit it) would be
    misclassified exactly the same way the human-readable-suffix bug was.
    The fix must anchor to the SPECIFIC shape `_shard_pointer` actually
    generates — JIT_POINTER_SUFFIX immediately followed by
    JIT_POINTER_SENTINEL, at the END of the line — not "contains the
    character somewhere". A stray sentinel mid-sentence, nowhere near that
    trailing shape, must not trigger it."""
    stray_mid_prose = ("- [Copy paste artifact](copy-paste-artifact.md) — "
                       "pasted from a PDF export that silently inserted a"
                       + mg.JIT_POINTER_SENTINEL +
                       "stray invisible separator mid-sentence, nothing to "
                       "do with sharding")
    assert mg.JIT_POINTER_SENTINEL in stray_mid_prose      # the trap is real
    assert not stray_mid_prose.endswith(
        mg.JIT_POINTER_SUFFIX + mg.JIT_POINTER_SENTINEL)    # not the generated shape
    assert mg._is_shard_pointer_line(stray_mid_prose) is False

    # A genuinely generated pointer (sentinel at the trailing, generated
    # position) is still correctly caught.
    generated = mg._shard_pointer("watering", "Watering & irrigation", 1, 20)
    assert mg._is_shard_pointer_line(generated) is True

    # And the no-loss invariant must not silently drop it either: a bug that
    # loses this note (present before, gone after) must still raise.
    core_before = "# Memory Index\n\n%s\n" % stray_mid_prose
    core_after_dropped = "# Memory Index\n\n"
    with pytest.raises(ValueError, match="NO-LOSS INVARIANT VIOLATED"):
        mg.check_extend_no_loss(
            [("MEMORY.md", core_before)],
            [("MEMORY.md", core_after_dropped)])


# -------- (3)+(4) atomic multi-file commit: mid-write failure leaves dir clean

def _flaky_replace(monkeypatch, fail_after):
    """Patch os.replace so the (fail_after + 1)-th call raises instead of
    replacing — simulates a crash/SIGKILL landing exactly BETWEEN two commits
    of a multi-file atomic-write loop (as opposed to
    test_*_apply_is_atomic_on_midwrite_failure above, which fails during
    STAGING, before any os.replace has run at all). The first `fail_after`
    calls replace for real; the loop never reaches a (fail_after + 2)-th call,
    since a real crash does not get caught and continue."""
    real_replace = os.replace
    state = {"n": 0}

    def flaky(src, dst, *a, **kw):
        state["n"] += 1
        if state["n"] == fail_after + 1:
            raise OSError("simulated crash before commit #%d" % state["n"])
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", flaky)


# issue #183 finding 1 (BM10): execute_shard/execute_extend already
# stage every output to a temp sibling before committing ANY of it, but the
# COMMIT ORDER used to replace MEMORY.md (the core index) EARLY — before the
# shard file(s) its own new pointer lines point at. A crash/SIGKILL landing
# between the core's os.replace and a shard's os.replace left the store
# genuinely corrupt: the core already claims a note lives in a shard that
# does not yet hold (or does not yet exist with) that note's bullet — the
# note becomes unreachable through EITHER file. The recoverability invariant
# these tests assert: at every possible crash point, every note scan() found
# indexed BEFORE the run must still be in_index=True AFTER a crash — the
# store must always be fully-old or fully-new, never a state with an
# orphaned note. (Covers "kill after file 1 of N" through "kill after N-1 of
# N, i.e. every file but the index" — fail_after == N itself is the ordinary
# success path, already covered elsewhere.)

@pytest.mark.parametrize("fail_after", [1, 2, 3, 4])
def test_shard_apply_crash_mid_commit_leaves_every_note_recoverable(
        tmp_path, monkeypatch, fail_after):
    work, sha = _seed(tmp_path)
    before = mg.scan(str(work), TODAY)
    assert all(f["in_index"] for f in before["files"]), \
        "fixture sanity: every note must start indexed"

    _flaky_replace(monkeypatch, fail_after)
    with pytest.raises(OSError, match="simulated crash"):
        mg.execute_shard(str(work), GARDEN_TAXONOMY, TODAY,
                         expect_sha256=sha, force=False)
    monkeypatch.undo()

    after = mg.scan(str(work), TODAY)
    orphaned = [f["file"] for f in after["files"] if not f["in_index"]]
    assert orphaned == [], (
        "crash after %d of 5 commit(s) orphaned note(s) %r — the store is "
        "neither fully old nor fully new" % (fail_after, orphaned))


def test_shard_apply_is_atomic_on_midwrite_failure(tmp_path, monkeypatch):
    """A failure while staging outputs must leave the real dir byte-identical to
    the start with no partial files and no committed backup (no-loss validated
    in memory THEN written atomically)."""
    work, sha = _seed(tmp_path)
    before = _dir_snapshot(str(work))

    import builtins
    real_open = builtins.open
    state = {"n": 0}

    def flaky_open(path, mode="r", *args, **kwargs):
        if "w" in str(mode) and str(path).endswith(".tmp-shard"):
            state["n"] += 1
            if state["n"] == 2:                        # fail on the 2nd output
                raise OSError("simulated disk failure on 2nd staged output")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", flaky_open)
    with pytest.raises(OSError, match="simulated disk failure"):
        mg.execute_shard(str(work), GARDEN_TAXONOMY, TODAY,
                         expect_sha256=sha, force=False)
    monkeypatch.undo()

    assert _dir_snapshot(str(work)) == before          # nothing committed
    # No leftover temp files anywhere under the dir.
    leftovers = [os.path.join(r, f)
                 for r, _d, fs in os.walk(work) for f in fs
                 if f.endswith(".tmp-shard")]
    assert leftovers == []
    # No committed backup either (staging never reached os.replace).
    if (work / "archive").exists():
        assert [f for f in os.listdir(work / "archive")
                if f.startswith("MEMORY.pre-shard")] == []


# -------- (2, backup) --force never overwrites an existing pre-shard backup

def test_force_never_overwrites_existing_backup(tmp_path):
    """The backup is sacrosanct: a pre-existing pre-shard backup is left intact
    and the new one lands on a `-2` name — even under --force."""
    work, sha = _seed(tmp_path)
    (work / "archive").mkdir()
    prior = work / "archive" / "MEMORY.pre-shard-2026-07-08.md"
    prior.write_text("SENTINEL prior backup — must not be clobbered\n")
    original_core = (work / "MEMORY.md").read_text()

    res = mg.execute_shard(str(work), GARDEN_TAXONOMY, TODAY,
                           expect_sha256=sha, force=True)

    # Prior backup untouched.
    assert prior.read_text() == "SENTINEL prior backup — must not be clobbered\n"
    # New backup went to the non-colliding -2 name and holds the true original.
    bumped = work / "archive" / "MEMORY.pre-shard-2026-07-08-2.md"
    assert bumped.exists()
    assert bumped.read_text() == original_core
    assert res["backup"].endswith("MEMORY.pre-shard-2026-07-08-2.md")
    # The split still happened.
    assert (work / "MEMORY.watering.md").exists()


# -------- (5) single-read guard: layout built from the same bytes hashed

def test_shard_apply_reads_memory_md_exactly_once(tmp_path, monkeypatch):
    """No TOCTOU re-read: MEMORY.md is opened once (the hash-and-build read);
    the rewrite goes through a temp + os.replace, never re-opening MEMORY.md."""
    work, sha = _seed(tmp_path)
    memory_md = str(work / "MEMORY.md")

    import builtins
    real_open = builtins.open
    reads = {"n": 0}

    def counting_open(path, mode="r", *args, **kwargs):
        if os.path.abspath(str(path)) == os.path.abspath(memory_md) \
                and "w" not in str(mode) and "a" not in str(mode):
            reads["n"] += 1
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    mg.execute_shard(str(work), GARDEN_TAXONOMY, TODAY,
                     expect_sha256=sha, force=False)
    monkeypatch.undo()
    assert reads["n"] == 1                              # read once, only once

    # And build_shard_layout builds from the passed bytes verbatim (not a
    # re-read): a text that differs from disk is used as-is.
    injected = "# Memory Index\n\n- [Hose](garden-hose-leak.md) — x\n"
    t, _prop, files = mg.build_shard_layout(str(work), GARDEN_TAXONOMY,
                                            text=injected)
    assert t == injected
    assert dict(files)["MEMORY.md"] != (work / "MEMORY.md").read_text()


# =========================================================================== #
# issue #183 finding 4 (BP9) — port execute_extend's O_EXCL|O_NOFOLLOW
# staging opener to execute_shard's own (older) staging loop, for parity.
# Mirrors test_extend_apply_refuses_to_follow_a_preexisting_symlink_at_tmp_path
# / test_extend_apply_does_not_delete_a_preexisting_tmp_path_it_did_not_create
# exactly, against execute_shard instead of execute_extend. Named seam:
# execute_shard's staging-file-open call — no internal collaborator mocked,
# a real symlink is planted on the real filesystem.
# =========================================================================== #

def test_shard_apply_refuses_to_follow_a_preexisting_symlink_at_tmp_path(tmp_path):
    """SECURITY: if a symlink already sits at a staging `.tmp-shard` path
    (e.g. pointing outside the memory dir), execute_shard must refuse to
    open through it — never silently truncate/overwrite the symlink's
    target. Nothing is written; the real files stay untouched. Before this
    fix execute_shard's staging loop used a plain `open(tmp, "w")`, which
    silently follows and truncates a pre-existing symlink at that
    predictable path (execute_extend already carried this fix; execute_shard
    did not)."""
    work, sha = _seed(tmp_path)
    outside_target = tmp_path / "outside-sentinel.md"
    outside_target.write_text("SENTINEL — must never be touched\n")
    original_core = (work / "MEMORY.md").read_text()

    core_tmp = work / "MEMORY.md.tmp-shard"
    core_tmp.symlink_to(outside_target)

    with pytest.raises(OSError):
        mg.execute_shard(str(work), GARDEN_TAXONOMY, TODAY,
                         expect_sha256=sha, force=False)

    # The symlink's target must be untouched — the whole point of the fix:
    # O_EXCL|O_NOFOLLOW refuses to open through a pre-existing symlink at
    # all, rather than following it and truncating whatever it points to.
    assert outside_target.read_text() == "SENTINEL — must never be touched\n"
    assert (work / "MEMORY.md").read_text() == original_core   # untouched
    assert not (work / "MEMORY.watering.md").exists()          # split never landed
    # No committed backup landed — the failure happened before any real
    # (non-tmp) file was ever replaced into place (core is staged/replaced
    # LAST, but the STAGING loop itself fails before any os.replace runs).
    archive = work / "archive"
    committed_backups = list(archive.glob("MEMORY.pre-shard*")) if archive.exists() else []
    assert committed_backups == []


def test_shard_apply_does_not_delete_a_preexisting_tmp_path_it_did_not_create(tmp_path):
    """Cleanup-on-failure must only remove tmp files THIS run actually
    created (staged AFTER a successful open) — never a pre-existing artifact
    (like the planted symlink above) that happened to occupy a `.tmp-shard`
    path. Before this fix, execute_shard appended to `staged` BEFORE
    calling open(), so a failed open on a pre-existing path still queued
    that path for `os.remove` in the except handler."""
    work, sha = _seed(tmp_path)
    core_tmp = work / "MEMORY.md.tmp-shard"
    core_tmp.symlink_to(work / "MEMORY.md")   # any pre-existing symlink

    with pytest.raises(OSError):
        mg.execute_shard(str(work), GARDEN_TAXONOMY, TODAY,
                         expect_sha256=sha, force=False)

    # The planted symlink itself must still be exactly there — cleanup never
    # touched a path it didn't create.
    assert core_tmp.is_symlink()


# =========================================================================== #
# K4 — harness-layer budgets (the runtime's per-file memory load limit):
# per-file, all-stores truncation check (`filebudget`),
# pointer-memory refactor (`pointerize`), and wikilink hyphen/underscore
# normalization. All new surfaces below; nothing above this line was touched.
# =========================================================================== #

def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _lines_text(n, filler="x"):
    """A body of exactly n lines (n-1 newlines + a final unterminated line,
    matching len(text.splitlines()) == n)."""
    return "\n".join("%s%d" % (filler, i) for i in range(n))


def _symlink(link_path, target_path):
    """Create a real symlink at link_path -> target_path, for the K4
    symlink-escape hardening tests below. The guard being tested is a
    POSIX/Syncthing concern; a platform that cannot create the symlink fails
    here, loudly, rather than passing without exercising the guard."""
    os.symlink(str(target_path), str(link_path),
               target_is_directory=os.path.isdir(str(target_path)))


# --------------------------------------------------------------- measure_file

def test_measure_file_counts_bytes_not_chars(tmp_path):
    """These notes are full of multi-byte UTF-8 (em dashes); the harness's own
    ceiling is byte-based, so char count must never be used as a stand-in."""
    p = tmp_path / "x.md"
    text = "— " * 100  # em dash is 3 bytes in UTF-8, 1 char in Python str
    p.write_text(text, encoding="utf-8")
    m = mg.measure_file(str(p))
    raw_len = len(text.encode("utf-8"))
    assert m["bytes"] == raw_len
    assert m["bytes"] > len(text)  # proves bytes != chars for this content


def test_measure_file_line_count_matches_splitlines(tmp_path):
    p = tmp_path / "x.md"
    p.write_text(_lines_text(37), encoding="utf-8")
    assert mg.measure_file(str(p))["lines"] == 37


# --------------------------------------------------------- iter_memory_files

def test_iter_memory_files_skips_dotdirs(tmp_path):
    """Syncthing internals (.stfolder, .stversions) must never be treated as
    memory content, even though they can contain *.md files."""
    d = tmp_path / "memory"
    _write(d / "real-note.md", "hello\n")
    _write(d / ".stversions" / "real-note~20260101-000000.md", "old\n")
    _write(d / ".stfolder" / "marker.md", "marker\n")
    found = {os.path.join(*parts) for _p, parts in mg.iter_memory_files(str(d))}
    assert found == {"real-note.md"}


def test_iter_memory_files_finds_docs_and_archive_subdirs(tmp_path):
    d = tmp_path / "memory"
    _write(d / "note.md", "hello\n")
    _write(d / "docs" / "note-full.md", "full\n")
    _write(d / "archive" / "old.md", "old\n")
    found = {os.path.join(*parts) for _p, parts in mg.iter_memory_files(str(d))}
    assert found == {"note.md", os.path.join("docs", "note-full.md"),
                     os.path.join("archive", "old.md")}


def test_iter_memory_files_follows_symlinked_docs_dir(tmp_path):
    """The K4 hardening finding: os.walk's default followlinks=False means a
    symlinked docs/ is never descended into, so its *.md files go completely
    dark — not reported as `excluded` (counted, auditable) but silently
    invisible, contradicting the "excluded paths are always walked and
    reported" design. iter_memory_files must still yield files reached
    through a symlinked top-level docs/ (tagged with the normal docs/...
    rel_parts prefix, same as a real docs/ dir would be)."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    outside = tmp_path / "outside-docs-target"
    _write(outside / "big-full.md", "full\n")
    _symlink(memory_dir / "docs", outside)

    found = {os.path.join(*parts) for _p, parts in mg.iter_memory_files(str(memory_dir))}
    assert os.path.join("docs", "big-full.md") in found


def test_iter_memory_files_does_not_follow_an_unrelated_symlinked_subdir(tmp_path):
    """Precision check: the exception is scoped to the top-level docs/ and
    archive/ names specifically, not "any symlinked directory" — an
    arbitrary symlinked subdirectory elsewhere in the tree must still be
    left alone (the general symlink-loop-avoidance default), matching
    os.walk's own followlinks=False posture everywhere else."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    outside = tmp_path / "outside-unrelated-target"
    _write(outside / "hidden.md", "hidden\n")
    _symlink(memory_dir / "some-other-dir", outside)

    found = {os.path.join(*parts) for _p, parts in mg.iter_memory_files(str(memory_dir))}
    assert os.path.join("some-other-dir", "hidden.md") not in found


def test_is_excluded_subpath():
    assert mg._is_excluded_subpath(("docs", "x-full.md")) is True
    assert mg._is_excluded_subpath(("archive", "old.md")) is True
    assert mg._is_excluded_subpath(("note.md",)) is False
    assert mg._is_excluded_subpath(()) is False


# ----------------------------------------------------- check_file_budgets: OR

def test_check_file_budgets_or_is_load_bearing(tmp_path):
    """The ticket's own case: a file can be over on ONE axis while comfortably
    under the other. Both must independently trigger OVER — the OR is not
    decorative."""
    d = tmp_path / "memory"
    # Over on LINES only: 210 short lines, well under 25000 bytes.
    _write(d / "over-lines.md", _lines_text(210))
    # Over on BYTES only: a handful of very long lines, well under 200 lines.
    _write(d / "over-bytes.md", "y" * 30000 + "\n")
    # Under on both axes.
    _write(d / "fine.md", _lines_text(10))

    res = mg.check_file_budgets([str(d)], str(tmp_path))
    by_path = {os.path.basename(e["path"]): e for e in res["entries"]}
    assert by_path["over-lines.md"]["status"] == "OVER"
    assert by_path["over-bytes.md"]["status"] == "OVER"
    assert by_path["fine.md"]["status"] == "ok"
    assert res["over_budget"] is True
    assert res["counts"]["over"] == 2


# -------------------------------------------------------------- warn band

def test_check_file_budgets_warn_band_180_to_200(tmp_path):
    d = tmp_path / "memory"
    _write(d / "warn-183.md", _lines_text(183))    # in the warn band
    _write(d / "warn-200.md", _lines_text(200))    # top of warn band, not over
    _write(d / "under-170.md", _lines_text(170))   # below the warn band
    _write(d / "over-201.md", _lines_text(201))    # over, not warn

    res = mg.check_file_budgets([str(d)], str(tmp_path))
    by_path = {os.path.basename(e["path"]): e for e in res["entries"]}
    assert by_path["warn-183.md"]["status"] == "WARN"
    assert by_path["warn-200.md"]["status"] == "WARN"
    assert by_path["under-170.md"]["status"] == "ok"
    assert by_path["over-201.md"]["status"] == "OVER"
    assert res["warn_only"] is False  # an OVER file is also present
    assert res["counts"]["warn"] == 2


def test_check_file_budgets_warn_only_when_nothing_over(tmp_path):
    d = tmp_path / "memory"
    _write(d / "warn-190.md", _lines_text(190))
    res = mg.check_file_budgets([str(d)], str(tmp_path))
    assert res["over_budget"] is False
    assert res["warn_only"] is True


# --------------------------------------------------- docs/ and archive/ excl.

def test_check_file_budgets_excludes_docs_and_archive_from_over_and_warn(tmp_path):
    """The K4 finding: a naive scan flags docs/*-full.md and archive/*.md as
    false positives (they're the refactor's own output / frozen backups).
    Excluded files are still walked and reported (status=excluded), just never
    counted as OVER or WARN."""
    d = tmp_path / "memory"
    _write(d / "docs" / "big-full.md", _lines_text(500))       # would be OVER
    _write(d / "archive" / "MEMORY.pre-shard-2026-07-25.md", _lines_text(300))
    _write(d / "live-note.md", _lines_text(10))                # unaffected

    res = mg.check_file_budgets([str(d)], str(tmp_path))
    by_path = {e["path"].replace("\\", "/"): e for e in res["entries"]}
    assert by_path[os.path.relpath(str(d / "docs" / "big-full.md"), str(tmp_path)).replace("\\", "/")]["status"] == "excluded"
    assert res["counts"]["over"] == 0     # neither excluded file counts as OVER
    assert res["counts"]["warn"] == 0
    assert res["counts"]["excluded"] == 2
    assert res["over_budget"] is False


def test_check_file_budgets_reports_symlinked_docs_dir_as_excluded(tmp_path):
    """Integration proof (not just the iter_memory_files unit): a symlinked
    docs/ is walked, measured, and shows up in the filebudget scan tagged
    `excluded` — visible and auditable — instead of silently vanishing from
    the count entirely. Uses 500 lines (would be OVER if it were ever
    counted) to make an accidental "it's just not flagged" false pass
    impossible to confuse with the real bug (invisible == absent from
    `entries` altogether)."""
    d = tmp_path / "memory"
    d.mkdir()
    _write(d / "live-note.md", _lines_text(10))
    outside = tmp_path / "outside-docs-target"
    _write(outside / "big-full.md", _lines_text(500))
    _symlink(d / "docs", outside)

    res = mg.check_file_budgets([str(d)], str(tmp_path))
    matches = [e for e in res["entries"]
               if os.path.basename(e["path"]) == "big-full.md"]
    assert len(matches) == 1, (
        "symlinked docs/ file missing from scan entries — silently invisible")
    entry = matches[0]
    assert entry["status"] == "excluded"
    assert entry["lines"] == 500
    assert res["counts"]["excluded"] == 1
    assert res["counts"]["over"] == 0   # excluded, so 500 lines never flags OVER


# ------------------------------------------------------- multi-store scanning

def test_discover_memory_stores_finds_all_and_skips_missing(tmp_path):
    root = tmp_path / "projects"
    (root / "proj-a" / "memory").mkdir(parents=True)
    (root / "proj-b" / "memory").mkdir(parents=True)
    (root / "proj-c").mkdir(parents=True)   # no memory/ subdir — skipped
    found = mg.discover_memory_stores(str(root))
    assert found == [str(root / "proj-a" / "memory"), str(root / "proj-b" / "memory")]


def test_discover_memory_stores_missing_root_returns_empty(tmp_path):
    assert mg.discover_memory_stores(str(tmp_path / "does-not-exist")) == []


def test_check_file_budgets_aggregates_across_multiple_stores(tmp_path):
    root = tmp_path / "projects"
    _write(root / "proj-a" / "memory" / "note.md", _lines_text(10))
    _write(root / "proj-b" / "memory" / "note.md", _lines_text(210))  # over
    dirs = mg.discover_memory_stores(str(root))
    res = mg.check_file_budgets(dirs, str(root))
    assert res["store_count"] == 2
    assert res["counts"]["over"] == 1
    over_entry = next(e for e in res["entries"] if e["status"] == "OVER")
    assert over_entry["path"] == os.path.join("proj-b", "memory", "note.md")


# --------------------------------------------------------- config validation

def test_check_file_budgets_rejects_non_positive_ceilings(tmp_path):
    d = tmp_path / "memory"
    _write(d / "x.md", "hi\n")
    for kwargs in ({"max_lines": 0}, {"max_bytes": -1}, {"warn_lines": 0}):
        with pytest.raises(ValueError):
            mg.check_file_budgets([str(d)], str(tmp_path), **kwargs)


def test_check_file_budgets_rejects_warn_above_max(tmp_path):
    d = tmp_path / "memory"
    _write(d / "x.md", "hi\n")
    with pytest.raises(ValueError):
        mg.check_file_budgets([str(d)], str(tmp_path), max_lines=100, warn_lines=150)


# ------------------------------------------------------------------ alerting

def test_file_budget_alert_line_all_three_states(tmp_path):
    clean = mg.check_file_budgets([], str(tmp_path))
    assert "within" in mg.file_budget_alert_line(clean)

    d = tmp_path / "memory"
    _write(d / "warn.md", _lines_text(185))
    warn = mg.check_file_budgets([str(d)], str(tmp_path))
    line = mg.file_budget_alert_line(warn)
    assert "approaching" in line and "OVER" not in line

    _write(d / "over.md", _lines_text(210))
    over = mg.check_file_budgets([str(d)], str(tmp_path))
    assert "OVER" in mg.file_budget_alert_line(over)


def test_render_file_budget_report_smoke(tmp_path):
    d = tmp_path / "memory"
    _write(d / "over.md", _lines_text(210))
    _write(d / "docs" / "big-full.md", _lines_text(500))
    res = mg.check_file_budgets([str(d)], str(tmp_path))
    text = mg.render_file_budget_report(res)
    for token in ("harness truncation check", "## Flagged", "OVER lines=210",
                  "## Excluded", "load-bearing"):
        assert token in text


# ------------------------------------------------------- read-only proof

def test_filebudget_never_mutates_any_store(tmp_path):
    d = tmp_path / "memory"
    _write(d / "over.md", _lines_text(210))
    _write(d / "docs" / "big-full.md", _lines_text(500))
    before = _dir_snapshot(str(tmp_path))
    mg.check_file_budgets([str(d)], str(tmp_path))
    mg.render_file_budget_report(mg.check_file_budgets([str(d)], str(tmp_path)))
    after = _dir_snapshot(str(tmp_path))
    assert before == after


def test_filebudget_subcommand_has_no_apply_flag():
    import io
    import contextlib
    with pytest.raises(SystemExit):
        with contextlib.redirect_stderr(io.StringIO()):
            mg.main(["filebudget", "--apply"])


# ------------------------------------------------------------- CLI exit codes

def test_main_filebudget_exit_codes(tmp_path):
    d = tmp_path / "projects" / "proj" / "memory"
    _write(d / "clean.md", _lines_text(10))
    rc_clean = mg.main(["filebudget", "--dir", str(d)])
    assert rc_clean == 0

    _write(d / "warn.md", _lines_text(185))
    rc_warn = mg.main(["filebudget", "--dir", str(d)])
    assert rc_warn == mg.FILEBUDGET_WARN_EXIT
    assert rc_warn not in (0, mg.OVER_BUDGET_EXIT)

    _write(d / "over.md", _lines_text(210))
    rc_over = mg.main(["filebudget", "--dir", str(d)])
    assert rc_over == mg.OVER_BUDGET_EXIT


def test_main_filebudget_scans_all_stores_by_default(tmp_path, capsys):
    import json
    root = tmp_path / "projects"
    _write(root / "proj-a" / "memory" / "note.md", _lines_text(210))
    rc = mg.main(["filebudget", "--projects-root", str(root), "--json"])
    assert rc == mg.OVER_BUDGET_EXIT
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["over"] == 1


def test_main_filebudget_json_is_valid(tmp_path, capsys):
    import json
    d = tmp_path / "memory"
    _write(d / "x.md", _lines_text(5))
    mg.main(["filebudget", "--dir", str(d), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["ok"] == 1


# =========================================================================== #
# pointerize — the docs/<name>-full.md extraction ("--apply" plan field)
# =========================================================================== #

def test_docs_target_path_normal_stem(tmp_path):
    target = mg.docs_target_path(str(tmp_path), "some-note")
    assert target == os.path.join(str(tmp_path), "docs", "some-note-full.md")


def test_docs_target_path_rejects_separator_in_stem(tmp_path):
    with pytest.raises(ValueError, match="path separator"):
        mg.docs_target_path(str(tmp_path), "sub/evil")


def test_docs_target_path_never_creates_anything(tmp_path):
    """Pure function: computing the target path must not touch disk."""
    before = _dir_snapshot(str(tmp_path))
    mg.docs_target_path(str(tmp_path), "some-note")
    assert _dir_snapshot(str(tmp_path)) == before


def test_docs_target_path_rejects_symlinked_docs_dir(tmp_path):
    """The K4 adversarial-review finding: if memory_dir/docs is ITSELF a
    symlink pointing outside the memory tree, os.path.realpath() resolves
    BOTH target_real and docs_dir_real THROUGH that same symlink, so the
    commonpath escape-check is satisfied vacuously (they agree on the
    external directory) even though the write would land entirely outside
    memory/ — exactly what this guard exists to prevent, since Syncthing's
    .stignore only un-ignores memory/ and a write elsewhere reads as a
    DELETE on the peer. The guard must catch the symlink itself, before any
    realpath resolution papers over it."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    outside = tmp_path / "outside-the-memory-tree"
    outside.mkdir()
    _symlink(memory_dir / "docs", outside)

    with pytest.raises(ValueError, match="symlink"):
        mg.docs_target_path(str(memory_dir), "some-note")


def test_docs_target_path_accepts_real_docs_dir_alongside_unrelated_symlink(tmp_path):
    """Sanity check on the fix's precision: a REAL docs/ directory must keep
    working normally even when some unrelated symlink also exists in
    memory_dir — the new guard checks the docs/ component specifically, not
    "any symlink anywhere in memory_dir"."""
    memory_dir = tmp_path / "memory"
    (memory_dir / "docs").mkdir(parents=True)
    other_target = tmp_path / "some-other-real-dir"
    other_target.mkdir()
    _symlink(memory_dir / "unrelated-link", other_target)

    target = mg.docs_target_path(str(memory_dir), "some-note")
    assert target == os.path.join(str(memory_dir), "docs", "some-note-full.md")


def test_compute_pointerize_output_basic(tmp_path):
    entry = {"file": "note.md", "pointerize": {"full_body": "The full text.\n"}}
    rel, content = mg.compute_pointerize_output(entry, str(tmp_path))
    assert rel == os.path.join("docs", "note-full.md")
    assert content == "The full text.\n"


def test_compute_pointerize_output_none_when_absent(tmp_path):
    assert mg.compute_pointerize_output({"file": "note.md"}, str(tmp_path)) is None


def test_compute_pointerize_output_adds_trailing_newline(tmp_path):
    entry = {"file": "note.md", "pointerize": {"full_body": "no trailing newline"}}
    _rel, content = mg.compute_pointerize_output(entry, str(tmp_path))
    assert content == "no trailing newline\n"


# ------------------------------------------------- validate_plan integration

def test_validate_plan_rejects_pointerize_on_keep_verdict():
    with pytest.raises(ValueError, match="UPDATE or SUPERSEDED"):
        mg.validate_plan({"files": [{
            "file": "compost-bin-setup.md", "verdict": "KEEP",
            "pointerize": {"full_body": "x"},
        }]}, FIXTURE)


def test_validate_plan_rejects_pointerize_missing_full_body():
    with pytest.raises(ValueError, match="full_body"):
        mg.validate_plan({"files": [{
            "file": "compost-bin-setup.md", "verdict": "UPDATE",
            "pointerize": {},
        }]}, FIXTURE)


# --------------------------------- the outside-memory/ guard, proven to fire

def test_pointerize_guard_fires_end_to_end_via_apply_plan(tmp_path):
    """The hard invariant: a plan entry whose derived stem contains a path
    separator must be refused BEFORE any write, anywhere in the plan — proven
    against a REAL nested file (not just a synthetic stem), through the same
    apply_plan() entrypoint a real run would use."""
    work = tmp_path / "memory"
    _write(work / "sub" / "dir.md", "---\nname: x\n---\nbody\n")
    sha = mg.file_sha256(str(work / "sub" / "dir.md"))
    before = _dir_snapshot(str(work))

    plan = {"files": [{
        "file": "sub/dir.md", "verdict": "UPDATE", "sha256": sha,
        "body": "pointer body", "pointerize": {"full_body": "the full text"},
    }]}
    with pytest.raises(ValueError, match="path separator"):
        mg.apply_plan(plan, str(work), TODAY)
    assert _dir_snapshot(str(work)) == before   # nothing written, anywhere
    assert not (work / "docs").exists()


def test_apply_plan_refuses_pointerize_through_symlinked_docs_dir(tmp_path):
    """The K4 hardening finding, proven end-to-end through the same
    apply_plan() entrypoint a real run would use (not just the isolated
    docs_target_path unit): memory_dir/docs is a REAL symlink pointing
    outside the memory tree, built with the same shape a real Syncthing peer
    would see. apply_plan must refuse before writing ANYTHING — proven with
    directory snapshots of both the memory tree AND the symlink target,
    since a successful escape would write into the latter, not the former."""
    memory_dir = tmp_path / "memory"
    _write(memory_dir / "big-note.md", "---\nname: big-note\n---\nbody\n")
    sha = mg.file_sha256(str(memory_dir / "big-note.md"))
    outside = tmp_path / "outside-the-memory-tree"
    outside.mkdir()
    _symlink(memory_dir / "docs", outside)

    before_memory = _dir_snapshot(str(memory_dir))
    before_outside = _dir_snapshot(str(outside))

    plan = {"files": [{
        "file": "big-note.md", "verdict": "UPDATE", "sha256": sha,
        "body": "pointer body", "pointerize": {"full_body": "the full text"},
    }]}
    with pytest.raises(ValueError, match="symlink"):
        mg.apply_plan(plan, str(memory_dir), TODAY)

    assert _dir_snapshot(str(memory_dir)) == before_memory   # nothing written
    assert _dir_snapshot(str(outside)) == before_outside      # nothing escaped


def test_pointerize_guard_fires_via_render_report_too(tmp_path):
    """report also validates the FULL plan before rendering anything — same
    guard, same refusal, before any output (including the report file)."""
    work = tmp_path / "memory"
    _write(work / "sub" / "dir.md", "---\nname: x\n---\nbody\n")
    sha = mg.file_sha256(str(work / "sub" / "dir.md"))
    plan = {"files": [{
        "file": "sub/dir.md", "verdict": "UPDATE", "sha256": sha,
        "body": "pointer body", "pointerize": {"full_body": "the full text"},
    }]}
    with pytest.raises(ValueError, match="path separator"):
        mg.render_report(plan, str(work), TODAY)


# ------------------------------------------------------------ apply round-trip

def test_apply_plan_pointerize_writes_docs_file_and_pointer_body(tmp_path):
    """The full mechanized version of the 2026-07-25 hand sweep: original
    basename keeps a short pointer body; the full content lands at
    docs/<stem>-full.md; frontmatter (name/description/metadata) is preserved
    byte-identical except the additive last_verified stamp."""
    work = tmp_path / "memory"
    original = (
        "---\n"
        "name: big-note\n"
        'description: "a note that grew too large"\n'
        "metadata:\n"
        "  type: project\n"
        "---\n"
        "\nThe original oversized body lives here.\n"
    )
    _write(work / "big-note.md", original)
    sha = mg.file_sha256(str(work / "big-note.md"))

    pointer_body = (
        "\n**Pointer memory.** The full document is at "
        "[docs/big-note-full.md](docs/big-note-full.md).\n"
    )
    # Carries the original body VERBATIM (plus a new heading) — a pointerize is
    # a move, and check_pointerize_no_loss now enforces that.
    full_body = ("# Full detail\n\nThe original oversized body lives here.\n"
                 "\nEverything that used to live inline.\n")
    plan = {"files": [{
        "file": "big-note.md", "verdict": "UPDATE", "sha256": sha,
        "rationale": "oversized note pointerized",
        "body": pointer_body,
        "pointerize": {"full_body": full_body},
    }]}

    result = mg.apply_plan(plan, str(work), TODAY)
    assert any("pointerized big-note.md" in a for a in result["actions"])

    # Pointer stays at the ORIGINAL basename.
    pointer_on_disk = (work / "big-note.md").read_text()
    assert "Pointer memory" in pointer_on_disk
    assert "name: big-note\n" in pointer_on_disk          # frontmatter preserved
    assert 'description: "a note that grew too large"\n' in pointer_on_disk
    assert "  type: project\n" in pointer_on_disk
    assert "last_verified: %s\n" % TODAY.isoformat() in pointer_on_disk

    # Full body extracted to docs/<stem>-full.md, untouched content.
    full_on_disk = (work / "docs" / "big-note-full.md").read_text()
    assert full_on_disk == full_body


def test_render_report_previews_pointerize_without_writing(tmp_path):
    work = tmp_path / "memory"
    _write(work / "big-note.md", "---\nname: big-note\n---\nold body\n")
    sha = mg.file_sha256(str(work / "big-note.md"))
    plan = {"files": [{
        "file": "big-note.md", "verdict": "UPDATE", "sha256": sha,
        "body": "pointer body",
        "pointerize": {"full_body": "full detail here\nold body\n"},
    }]}
    before = _dir_snapshot(str(work))
    text = mg.render_report(plan, str(work), TODAY)
    assert "Pointerize" in text
    assert "docs/big-note-full.md" in text
    assert "full detail here" in text
    assert _dir_snapshot(str(work)) == before     # report writes nothing


def test_apply_plan_pointerize_overwrite_is_visible_in_report(tmp_path):
    work = tmp_path / "memory"
    _write(work / "big-note.md", "---\nname: big-note\n---\nold body\n")
    _write(work / "docs" / "big-note-full.md", "PRE-EXISTING\n")
    sha = mg.file_sha256(str(work / "big-note.md"))
    plan = {"files": [{
        "file": "big-note.md", "verdict": "UPDATE", "sha256": sha,
        "body": "pointer body",
        "pointerize": {"full_body": "new full body\nold body\n"},
    }]}
    text = mg.render_report(plan, str(work), TODAY)
    assert "OVERWRITES an existing file" in text
    # The report hands over the hash apply will demand to confirm the clobber.
    assert "sha256" in text
    assert mg.file_sha256(str(work / "docs" / "big-note-full.md")) in text


# =========================================================================== #
# pointerize DATA-SAFETY — atomicity, no-loss, destination drift
# =========================================================================== #
# The pointerize path used to overwrite the source with the short pointer stub
# FIRST and write docs/<stem>-full.md SECOND, unstaged, unbacked and without a
# try/except, and never compared `full_body` (model-authored prose) against the
# source at all. Two data-loss defects were reproduced through the real CLI:
# a docs/ that is a regular file, and a non-writable docs/ — each destroying
# the body permanently while `report` previewed the first case green.
#
# These mirror the safety pattern execute_shard already had in this same file:
# backup to archive/, stage every output, os.replace only after ALL succeed,
# and a no-loss invariant that refuses before writing anything.

_BIG_FM = (
    "---\n"
    "name: big-note\n"
    'description: "a note that grew too large"\n'
    "metadata:\n"
    "  type: project\n"
    "---\n"
)


def _big_note(n_facts=400):
    """A realistically oversized fact note — the shape that gets pointerized."""
    facts = "".join("- fact %03d: a durable detail worth keeping.\n" % i
                    for i in range(n_facts))
    return _BIG_FM + "\n# Big note\n\n" + facts


def _pointerize_plan(work, fname, full_body, pointer_body="pointer body\n",
                     docs_sha256=None):
    pz = {"full_body": full_body}
    if docs_sha256 is not None:
        pz["docs_sha256"] = docs_sha256
    return {"files": [{
        "file": fname, "verdict": "UPDATE",
        "sha256": mg.file_sha256(str(work / fname)),
        "body": pointer_body, "pointerize": pz,
    }]}


def _faithful_full_body(original):
    """What a CORRECT pointerize extracts: the original body, verbatim."""
    _fm, body = mg.split_frontmatter(original)
    return body


# ------------------------------------------------------- (1) ATOMICITY

def test_pointerize_apply_is_atomic_on_midwrite_failure(tmp_path, monkeypatch):
    """A failure partway through staging must leave the directory
    byte-identical to the start: the source body still intact, no docs file,
    no committed backup, no leftover temps. Before the fix the source was
    already overwritten with the pointer stub by this point and the body was
    unrecoverable."""
    work = tmp_path / "memory"
    original = _big_note()
    _write(work / "big-note.md", original)
    plan = _pointerize_plan(work, "big-note.md", _faithful_full_body(original))
    before = _dir_snapshot(str(work))

    real_stage = mg._stage_sibling
    state = {"n": 0}

    def flaky_stage(final_path, text):
        state["n"] += 1
        if state["n"] == 2:                       # fail on the 2nd staged output
            raise OSError("simulated disk failure on 2nd staged output")
        return real_stage(final_path, text)

    monkeypatch.setattr(mg, "_stage_sibling", flaky_stage)
    with pytest.raises(OSError, match="simulated disk failure"):
        mg.apply_plan(plan, str(work), TODAY)
    monkeypatch.undo()

    assert _dir_snapshot(str(work)) == before      # nothing committed at all
    assert (work / "big-note.md").read_text() == original   # body survived
    assert not (work / "docs" / "big-note-full.md").exists()
    leftovers = [os.path.join(r, f)
                 for r, _d, fs in os.walk(work) for f in fs
                 if f.endswith(".tmp-pointerize")]
    assert leftovers == []
    if (work / "archive").exists():
        assert [f for f in os.listdir(work / "archive")
                if ".pre-pointerize-" in f] == []


def test_pointerize_apply_backs_up_the_original_before_overwriting(tmp_path):
    """The successful path leaves an undo: the true pre-pointerize original in
    archive/. An existing backup is sacrosanct — the new one claims the `-2`
    name rather than destroying the only copy (same rule as the shard backup)."""
    work = tmp_path / "memory"
    original = _big_note(20)
    _write(work / "big-note.md", original)
    prior = work / "archive" / ("big-note.pre-pointerize-%s.md"
                                % TODAY.isoformat())
    _write(prior, "SENTINEL prior backup — must not be clobbered\n")
    plan = _pointerize_plan(work, "big-note.md", _faithful_full_body(original))

    result = mg.apply_plan(plan, str(work), TODAY)

    # Prior backup untouched; the new one went to the non-colliding -2 name.
    assert prior.read_text() == "SENTINEL prior backup — must not be clobbered\n"
    bumped = work / "archive" / ("big-note.pre-pointerize-%s-2.md"
                                 % TODAY.isoformat())
    assert bumped.exists()
    assert bumped.read_text() == original          # exact pre-pointerize copy
    assert any("pre-pointerize" in a for a in result["actions"])
    # ...and the move itself still happened.
    assert "fact 000" in (work / "docs" / "big-note-full.md").read_text()
    assert "pointer body" in (work / "big-note.md").read_text()


def test_pointerize_writes_no_backup_when_the_source_is_unchanged(tmp_path):
    """The backup exists to make OVERWRITTEN content recoverable. If the plan
    leaves the source text as-is, nothing was moved out of it and there is
    nothing to recover — so no archive noise is generated."""
    work = tmp_path / "memory"
    # last_verified already stamped, so UPDATE's additive stamp is a no-op and
    # with no `body` in the plan the source text comes out byte-identical.
    original = ("---\nname: big-note\nlast_verified: %s\n---\n"
                "\nfact one\nfact two\n" % TODAY.isoformat())
    _write(work / "big-note.md", original)
    plan = {"files": [{
        "file": "big-note.md", "verdict": "UPDATE",
        "sha256": mg.file_sha256(str(work / "big-note.md")),
        "pointerize": {"full_body": "fact one\nfact two\n"},
    }]}
    mg.apply_plan(plan, str(work), TODAY)
    assert (work / "big-note.md").read_text() == original     # unchanged
    assert (work / "docs" / "big-note-full.md").exists()
    assert not (work / "archive").exists()


# --------------------------------- (2) NO-LOSS — full_body vs the source

def test_pointerize_refuses_when_full_body_drops_the_original_content(tmp_path):
    """REPRODUCED DEFECT 2: `full_body` is model-authored prose that was never
    compared against the source. A 400-fact note collapsed to a ~200-byte
    summary applied cleanly with no warnings and the facts were gone. The
    no-loss invariant now refuses, writing NOTHING."""
    work = tmp_path / "memory"
    original = _big_note(400)
    _write(work / "big-note.md", original)
    assert len(original) > 12000                   # genuinely oversized

    summary = ("# Big note\n\nA collection of durable project details "
               "covering roughly four hundred separate facts, summarized "
               "here for brevity.\n")
    assert len(summary) < 250                      # the collapse, to scale
    plan = _pointerize_plan(work, "big-note.md", summary)
    before = _dir_snapshot(str(work))

    with pytest.raises(ValueError, match="NO-LOSS INVARIANT VIOLATED"):
        mg.apply_plan(plan, str(work), TODAY)

    assert _dir_snapshot(str(work)) == before      # nothing written
    assert (work / "big-note.md").read_text() == original
    assert not (work / "docs").exists()


def test_pointerize_no_loss_allows_added_prose_but_not_dropped_facts(tmp_path):
    """Containment, not equality: the pointer stub is new prose by design and
    the extraction may gain a heading, so EXTRA lines are fine — only MISSING
    ones are a defect. (check_no_loss's shard twin demands exact equality;
    this deliberately does not.)"""
    original = _BIG_FM + "\nfact one\nfact two\n"
    faithful = "# Extracted\n\nfact one\nfact two\n\n_moved 2026-07-08._\n"
    # Extra lines everywhere -> fine.
    mg.check_pointerize_no_loss(original, "a brand new pointer stub\n", faithful)
    # Drop exactly one fact -> refuses.
    with pytest.raises(ValueError, match="NO-LOSS INVARIANT VIOLATED"):
        mg.check_pointerize_no_loss(
            original, "a brand new pointer stub\n", "# Extracted\n\nfact one\n")
    # Content retained on the POINTER instead of the extraction also counts.
    mg.check_pointerize_no_loss(original, "fact one\nfact two\n", "# Extracted\n")


def test_render_report_refuses_a_lossy_pointerize_exactly_like_apply(tmp_path):
    """Report/apply parity in the SAFE direction. The reviewer's finding was
    that `report` previewed a destructive pointerize green — both entrypoints
    now run the identical check, so a plan that apply would refuse can never
    be reviewed as fine."""
    work = tmp_path / "memory"
    original = _big_note(50)
    _write(work / "big-note.md", original)
    plan = _pointerize_plan(work, "big-note.md", "a one-line summary\n")
    before = _dir_snapshot(str(work))

    with pytest.raises(ValueError, match="NO-LOSS INVARIANT VIOLATED"):
        mg.render_report(plan, str(work), TODAY)
    assert _dir_snapshot(str(work)) == before      # report still writes nothing


# ------------------------------- (3) DESTINATION DRIFT GUARD

def test_pointerize_refuses_when_docs_destination_changed_under_the_plan(tmp_path):
    """The source has always been drift-guarded by `sha256`; the DESTINATION
    had no guard, so a docs/<stem>-full.md that changed during the
    report-review-apply gap was clobbered silently. It must now be DETECTED,
    not overwritten."""
    work = tmp_path / "memory"
    original = _big_note(20)
    _write(work / "big-note.md", original)
    _write(work / "docs" / "big-note-full.md", "REVIEWED AT PLAN TIME\n")

    # Plan authored against the destination as reviewed.
    plan = _pointerize_plan(
        work, "big-note.md", _faithful_full_body(original),
        docs_sha256=mg.file_sha256(str(work / "docs" / "big-note-full.md")))

    # ...then somebody else edits the destination before apply runs.
    (work / "docs" / "big-note-full.md").write_text("EDITED BY SOMEONE ELSE\n")
    before = _dir_snapshot(str(work))

    with pytest.raises(ValueError, match="STALE PLAN.*destination"):
        mg.apply_plan(plan, str(work), TODAY)

    assert _dir_snapshot(str(work)) == before
    assert ((work / "docs" / "big-note-full.md").read_text()
            == "EDITED BY SOMEONE ELSE\n")         # not clobbered
    assert (work / "big-note.md").read_text() == original

    # A destination that VANISHED under the plan is drift too.
    (work / "docs" / "big-note-full.md").unlink()
    with pytest.raises(ValueError, match="STALE PLAN.*no longer exists"):
        mg.apply_plan(plan, str(work), TODAY)


def test_pointerize_apply_refuses_an_unacknowledged_existing_destination(tmp_path):
    """The other drift direction: the destination did not exist when the report
    was reviewed (so the plan carries no docs_sha256) but exists by apply time.
    Apply must refuse rather than silently clobber a file nobody reviewed."""
    work = tmp_path / "memory"
    original = _big_note(20)
    _write(work / "big-note.md", original)
    plan = _pointerize_plan(work, "big-note.md", _faithful_full_body(original))

    # Report is clean — the destination genuinely does not exist yet.
    mg.render_report(plan, str(work), TODAY)

    # It appears during the review gap.
    _write(work / "docs" / "big-note-full.md", "APPEARED AFTER THE REPORT\n")
    before = _dir_snapshot(str(work))

    with pytest.raises(ValueError, match="no docs_sha256"):
        mg.apply_plan(plan, str(work), TODAY)
    assert _dir_snapshot(str(work)) == before
    assert ((work / "docs" / "big-note-full.md").read_text()
            == "APPEARED AFTER THE REPORT\n")

    # Acknowledging it with the hash the report prints lets it proceed.
    ok = _pointerize_plan(
        work, "big-note.md", _faithful_full_body(original),
        docs_sha256=mg.file_sha256(str(work / "docs" / "big-note-full.md")))
    mg.apply_plan(ok, str(work), TODAY)
    assert "fact 000" in (work / "docs" / "big-note-full.md").read_text()


# --------------- REPRODUCED DEFECT 1 — the two real filesystem failures

def test_pointerize_refuses_when_docs_is_a_regular_file_source_survives(tmp_path):
    """REPRODUCED DEFECT 1a: `docs` present as a regular file. The old order
    (overwrite source -> then write docs) raised FileExistsError only AFTER the
    body was already destroyed. The extraction directory is now created before
    anything is staged, so this fails with the source fully intact."""
    work = tmp_path / "memory"
    original = _big_note(30)
    _write(work / "big-note.md", original)
    _write(work / "docs", "I am a regular file, not a directory\n")
    plan = _pointerize_plan(work, "big-note.md", _faithful_full_body(original))
    before = _dir_snapshot(str(work))

    with pytest.raises((FileExistsError, NotADirectoryError, OSError)):
        mg.apply_plan(plan, str(work), TODAY)

    assert _dir_snapshot(str(work)) == before
    assert (work / "big-note.md").read_text() == original   # body NOT destroyed
    assert (work / "docs").read_text() == "I am a regular file, not a directory\n"


def test_pointerize_refuses_when_docs_is_unwritable_source_survives(tmp_path):
    """(Run as a normal user: root ignores directory permissions.) REPRODUCED DEFECT 1b: a non-writable docs/. Previously PermissionError
    surfaced as a raw traceback only after the source had been replaced by the
    pointer stub. Staging every output before any os.replace means the failure
    now happens with nothing committed."""
    work = tmp_path / "memory"
    original = _big_note(30)
    _write(work / "big-note.md", original)
    docs = work / "docs"
    docs.mkdir(parents=True)
    plan = _pointerize_plan(work, "big-note.md", _faithful_full_body(original))
    before = _dir_snapshot(str(work))

    os.chmod(docs, 0o500)                          # r-x: cannot create in docs/
    try:
        with pytest.raises(PermissionError):
            mg.apply_plan(plan, str(work), TODAY)
    finally:
        os.chmod(docs, 0o700)

    assert _dir_snapshot(str(work)) == before
    assert (work / "big-note.md").read_text() == original   # body NOT destroyed
    assert list(docs.iterdir()) == []               # no partial extraction
    leftovers = [os.path.join(r, f)
                 for r, _d, fs in os.walk(work) for f in fs
                 if f.endswith(".tmp-pointerize")]
    assert leftovers == []


# =========================================================================== #
# pointerize commit hardening — second-pass review findings
# =========================================================================== #

# ------------------------- (4) COMMIT ORDER: source is replaced LAST

@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_pointerize_source_is_intact_after_failure_at_every_replace(
        tmp_path, monkeypatch, fail_at):
    """A multi-file commit cannot be atomic on POSIX — os.replace is atomic per
    FILE. So the ordering must make every reachable partial state safe, and the
    source must be replaced LAST.

    The regression: the order was backup -> source -> destination, so a failure
    on the THIRD replace left the source pointerized at a destination that did
    not exist — the exact data-loss shape, reachable by a crash between two
    replaces and NOT undoable by the cleanup path (it removes temps; it cannot
    un-replace). Now the source survives a failure at ANY position."""
    work = tmp_path / "memory"
    original = _big_note(20)
    _write(work / "big-note.md", original)
    plan = _pointerize_plan(work, "big-note.md", _faithful_full_body(original))

    real_replace = os.replace
    state = {"n": 0}

    def flaky_replace(src, dst, *a, **k):
        state["n"] += 1
        if state["n"] == fail_at:
            raise OSError("injected failure at replace #%d" % fail_at)
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", flaky_replace)
    with pytest.raises(OSError, match="injected failure"):
        mg.apply_plan(plan, str(work), TODAY)
    monkeypatch.undo()

    # The invariant that matters at EVERY failure point.
    assert (work / "big-note.md").read_text() == original
    leftovers = [f for _r, _d, fs in os.walk(work) for f in fs
                 if f.endswith(".tmp-pointerize")]
    assert leftovers == []


def test_pointerize_partial_commit_leaves_only_additive_artifacts(
        tmp_path, monkeypatch):
    """Spelling out the worst reachable partial state: a failure on the LAST
    replace (the source) leaves the backup and the extraction on disk and the
    source untouched. Additive, harmless, and idempotently retryable — never a
    stub pointing at nothing."""
    work = tmp_path / "memory"
    original = _big_note(20)
    _write(work / "big-note.md", original)
    plan = _pointerize_plan(work, "big-note.md", _faithful_full_body(original))

    real_replace = os.replace
    state = {"n": 0}

    def flaky_replace(src, dst, *a, **k):
        state["n"] += 1
        if state["n"] == 3:
            raise OSError("injected failure on the source replace")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", flaky_replace)
    with pytest.raises(OSError):
        mg.apply_plan(plan, str(work), TODAY)
    monkeypatch.undo()

    assert (work / "big-note.md").read_text() == original      # never stubbed
    # the extraction landed first, so the body is recoverable from TWO places
    assert (work / "docs" / "big-note-full.md").exists()
    backups = [f for f in os.listdir(work / "archive")
               if ".pre-pointerize-" in f]
    assert len(backups) == 1
    assert (work / "archive" / backups[0]).read_text() == original


# ------------------------- (5) TEMP FILES: exclusive, unguessable, no follow

def test_pointerize_does_not_consume_a_preexisting_temp_file(tmp_path):
    """A predictable `<final>.tmp-pointerize` opened with a truncating
    `open(..., "w")` silently destroyed a stale temp left by a crashed run and
    let two concurrent gardeners interleave bytes in the SAME staging file.
    Staging is now exclusively created under a random name, so a pre-existing
    temp is left completely alone."""
    work = tmp_path / "memory"
    original = _big_note(20)
    _write(work / "big-note.md", original)
    stale = work / "big-note.md.tmp-pointerize"
    _write(stale, "somebody else's staged data\n")

    plan = _pointerize_plan(work, "big-note.md", _faithful_full_body(original))
    mg.apply_plan(plan, str(work), TODAY)

    assert stale.exists()
    assert stale.read_text() == "somebody else's staged data\n"


def test_pointerize_staging_does_not_follow_a_symlinked_temp_path(tmp_path):
    """The worst consequence of the predictable temp name: a symlink sitting at
    it made the staging write FOLLOW it, landing the extracted body OUTSIDE
    memory/ — walking straight around the `docs/` symlink-escape guard that
    exists because a write outside memory/ reads as a DELETE on the Syncthing
    peer. Exclusive creation cannot follow a symlink."""
    work = tmp_path / "memory"
    outside = tmp_path / "outside"
    original = _big_note(20)
    _write(work / "big-note.md", original)
    victim = outside / "victim.md"
    _write(victim, "IMPORTANT UNRELATED FILE\n")
    (work / "docs").mkdir(parents=True)
    os.symlink(str(victim), str(work / "docs" / "big-note-full.md.tmp-pointerize"))

    plan = _pointerize_plan(work, "big-note.md", _faithful_full_body(original))
    mg.apply_plan(plan, str(work), TODAY)

    # nothing outside memory/ was written through the symlink
    assert victim.read_text() == "IMPORTANT UNRELATED FILE\n"
    # and the real extraction landed inside memory/, as a regular file
    dest = work / "docs" / "big-note-full.md"
    assert dest.is_file() and not dest.is_symlink()
    assert _faithful_full_body(original) in dest.read_text()


# ------------------------- (6) DESTINATION DRIFT RE-CHECKED AT COMMIT TIME

def test_pointerize_refuses_a_destination_that_appears_during_staging(
        tmp_path, monkeypatch):
    """`validate_plan` checks the destination hash at PLAN time, which leaves a
    window: a concurrent writer that creates the destination while we stage was
    silently clobbered by the commit. The hash is now re-verified immediately
    before the destination is replaced, so the write refuses instead."""
    work = tmp_path / "memory"
    original = _big_note(20)
    _write(work / "big-note.md", original)
    plan = _pointerize_plan(work, "big-note.md", _faithful_full_body(original))
    dest = work / "docs" / "big-note-full.md"

    real_stage = mg._stage_sibling

    def racing_stage(final_path, text):
        tmp = real_stage(final_path, text)
        if not dest.exists():                     # a concurrent writer, mid-stage
            _write(dest, "CONCURRENT CONTENT NOBODY REVIEWED\n")
        return tmp

    monkeypatch.setattr(mg, "_stage_sibling", racing_stage)
    with pytest.raises(ValueError, match="would OVERWRITE an existing"):
        mg.apply_plan(plan, str(work), TODAY)
    monkeypatch.undo()

    assert dest.read_text() == "CONCURRENT CONTENT NOBODY REVIEWED\n"
    assert (work / "big-note.md").read_text() == original
    leftovers = [f for _r, _d, fs in os.walk(work) for f in fs
                 if f.endswith(".tmp-pointerize")]
    assert leftovers == []


# ------------------------- (7) BACKUP RESERVATION (never-overwrite TOCTOU)

def test_pointerize_backup_name_is_reserved_not_merely_chosen(tmp_path):
    """The never-overwrite invariant had a TOCTOU hole: the name was picked
    with `os.path.exists` and committed with `os.replace` much later, so a
    same-day backup appearing in between was destroyed by the very commit that
    promised never to overwrite one. The name is now claimed with O_EXCL at
    selection time, so a racing writer sees it taken and cannot land there."""
    work = tmp_path / "memory"
    original = _big_note(20)
    _write(work / "big-note.md", original)
    archive = work / "archive"
    archive.mkdir()
    stem = "big-note.pre-pointerize-%s" % TODAY.isoformat()

    reserved = mg._reserve_pointerize_backup_path(str(archive), "big-note", TODAY)
    assert os.path.basename(reserved) == stem + ".md"
    assert os.path.exists(reserved)               # claimed immediately

    # a second caller cannot get the same name
    second = mg._reserve_pointerize_backup_path(str(archive), "big-note", TODAY)
    assert os.path.basename(second) == stem + "-2.md"
    assert second != reserved

    # and a real run therefore skips both rather than clobbering them
    plan = _pointerize_plan(work, "big-note.md", _faithful_full_body(original))
    mg.apply_plan(plan, str(work), TODAY)
    assert os.path.basename(
        [f for f in os.listdir(archive)
         if f.startswith(stem) and os.path.getsize(archive / f) > 0][0]
    ) == stem + "-3.md"


def test_pointerize_failed_commit_releases_its_backup_reservation(
        tmp_path, monkeypatch):
    """A reservation the commit never fills is our own 0-byte placeholder — it
    must not accumulate in archive/ as permanent noise. A backup that DID get
    real content is left alone (never deleted)."""
    work = tmp_path / "memory"
    original = _big_note(20)
    _write(work / "big-note.md", original)
    plan = _pointerize_plan(work, "big-note.md", _faithful_full_body(original))

    monkeypatch.setattr(mg, "_stage_sibling",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError("fail before anything is committed")))
    with pytest.raises(OSError):
        mg.apply_plan(plan, str(work), TODAY)
    monkeypatch.undo()

    assert (work / "big-note.md").read_text() == original
    if (work / "archive").exists():
        assert [f for f in os.listdir(work / "archive")
                if ".pre-pointerize-" in f] == []


# ------------------------- (8) NO-LOSS: indentation is content

def test_no_loss_refuses_flattened_nested_bullets(tmp_path):
    """Stripping every line before comparing let a whole class of loss through:
    re-flowing a nested list keeps every line's TEXT while destroying which
    child belongs to which parent. Here that is which password belongs to which
    server — the fact itself."""
    original = _BIG_FM + ("\n- prod\n    - password: hunter2\n"
                          "- staging\n    - password: swordfish\n")
    flattened = ("- prod\n- password: hunter2\n"
                 "- staging\n- password: swordfish\n")
    with pytest.raises(ValueError, match="NO-LOSS INVARIANT VIOLATED"):
        mg.check_pointerize_no_loss(original, "pointer stub\n", flattened)
    # the faithful move, indentation intact, still passes
    _fm, body = mg.split_frontmatter(original)
    mg.check_pointerize_no_loss(original, "pointer stub\n", body)


def test_no_loss_refuses_a_de_indented_code_block(tmp_path):
    """Same defect, the other meaning-bearing indentation: a line lifted out of
    a fenced code block reads identically after stripping but no longer runs."""
    original = _BIG_FM + "\n```python\ndef f():\n    return 1\n```\n"
    broken = "```python\ndef f():\nreturn 1\n```\n"
    with pytest.raises(ValueError, match="NO-LOSS INVARIANT VIOLATED"):
        mg.check_pointerize_no_loss(original, "pointer stub\n", broken)


def test_no_loss_tolerates_trailing_whitespace_and_crlf(tmp_path):
    """The other side of the line: trailing whitespace and the newline
    convention carry no fact and are rewritten silently by editors, so
    normalizing them is correct — refusing on them would be a false alarm that
    blocks a faithful move."""
    original = _BIG_FM + "\nfact one   \nfact two\n"
    mg.check_pointerize_no_loss(original, "pointer stub\n", "fact one\nfact two\n")
    crlf = _BIG_FM + "\r\nfact one\r\nfact two\r\n"
    mg.check_pointerize_no_loss(crlf, "pointer stub\n", "fact one\nfact two\n")
    # ...but a genuinely dropped line is still caught through CRLF
    with pytest.raises(ValueError, match="NO-LOSS INVARIANT VIOLATED"):
        mg.check_pointerize_no_loss(crlf, "pointer stub\n", "fact one\n")


def test_no_loss_counts_duplicates_as_a_multiset(tmp_path):
    """A line appearing twice in the source and once in the output is a lost
    fact, not a satisfied one — set semantics would miss it."""
    original = _BIG_FM + "\n- token: abc123\n- other\n- token: abc123\n"
    with pytest.raises(ValueError, match="NO-LOSS INVARIANT VIOLATED"):
        mg.check_pointerize_no_loss(
            original, "pointer stub\n", "- token: abc123\n- other\n")
    _fm, body = mg.split_frontmatter(original)
    mg.check_pointerize_no_loss(original, "pointer stub\n", body)


# =========================================================================== #
# wikilink hyphen/underscore normalization — suggestion-only, never auto-fix
# =========================================================================== #

def test_link_norm_key_folds_case_and_separators():
    assert mg._link_norm_key("Feedback-Self-Review") == "feedback-self-review"
    assert mg._link_norm_key("feedback_self_review") == "feedback-self-review"


def test_find_hyphen_underscore_mismatches_true_positive(tmp_path):
    """The real shape from the ticket: [[feedback-self-review]] (hyphens) when
    the file on disk is feedback_self_review.md (underscores)."""
    d = tmp_path / "memory"
    _write(d / "feedback_self_review.md", "---\nname: x\n---\nbody\n")
    _write(d / "referrer.md",
          "---\nname: r\n---\nSee [[feedback-self-review]] for detail.\n")
    suggestions = mg.find_hyphen_underscore_mismatches(str(d))
    assert suggestions == [{
        "file": "referrer.md",
        "target": "feedback-self-review",
        "suggested": ["feedback_self_review"],
        "ambiguous": False,
    }]


def test_find_hyphen_underscore_mismatches_ignores_resolving_links(tmp_path):
    d = tmp_path / "memory"
    _write(d / "note-a.md", "---\nname: a\n---\nbody\n")
    _write(d / "referrer.md", "[[note-a]] resolves exactly already.\n")
    assert mg.find_hyphen_underscore_mismatches(str(d)) == []


def test_find_hyphen_underscore_mismatches_never_fabricates_a_prefix_repair(tmp_path):
    """A link missing an entire prefix (not a pure separator swap) must NOT be
    suggested — matches the real [[deploy-preflight-html-encoding-sniffer]]
    case, which points at a differently-PREFIXED file in a DIFFERENT store and
    must stay a plain dangling link, not a fabricated repair."""
    d = tmp_path / "memory"
    _write(d / "feedback_deploy_preflight_html_encoding_sniffer.md",
          "---\nname: x\n---\nbody\n")
    _write(d / "referrer.md",
          "See [[deploy-preflight-html-encoding-sniffer]] for the gate.\n")
    assert mg.find_hyphen_underscore_mismatches(str(d)) == []


def test_find_hyphen_underscore_mismatches_ambiguous_when_two_live_matches(tmp_path):
    d = tmp_path / "memory"
    _write(d / "x-y.md", "---\nname: a\n---\nbody\n")
    _write(d / "x_y.md", "---\nname: b\n---\nbody\n")
    _write(d / "referrer.md", "[[X_Y]] should be ambiguous.\n")
    suggestions = mg.find_hyphen_underscore_mismatches(str(d))
    assert len(suggestions) == 1
    assert suggestions[0]["ambiguous"] is True
    assert set(suggestions[0]["suggested"]) == {"x-y", "x_y"}


def test_find_hyphen_underscore_mismatches_scoped_to_one_store(tmp_path):
    """Wikilinks are expected to resolve within their own project's store —
    same scoping as dangling_links. A file that would match in a DIFFERENT
    store must not be suggested."""
    d = tmp_path / "memory"
    _write(d / "referrer.md", "[[some-target]] here.\n")
    other = tmp_path / "other-store" / "memory"
    _write(other / "some_target.md", "---\nname: x\n---\nbody\n")
    assert mg.find_hyphen_underscore_mismatches(str(d)) == []


def test_scan_includes_hyphen_underscore_repairs_key():
    """Regression guard on the shipped invented fixture: it has no
    hyphen/underscore-fixable dangling links today (matches the real corpus
    finding for the other stores)."""
    result = mg.scan(FIXTURE, TODAY)
    assert result["hyphen_underscore_repairs"] == []


def test_scan_surfaces_a_real_hyphen_underscore_mismatch(tmp_path):
    """End-to-end: scan()'s new key actually surfaces a genuine mismatch, not
    just an empty list on every input."""
    d = tmp_path / "memory"
    _write(d / "project_onboarding_overhaul.md", "---\nname: x\n---\nbody\n")
    _write(d / "referrer.md", "[[project-onboarding-overhaul]] work.\n")
    result = mg.scan(str(d), TODAY)
    assert result["hyphen_underscore_repairs"] == [{
        "file": "referrer.md",
        "target": "project-onboarding-overhaul",
        "suggested": ["project_onboarding_overhaul"],
        "ambiguous": False,
    }]


# =========================================================================== #
# Shard-aware scan + fact-note listing + budget reporting (issue #176)
# =========================================================================== #
#
# Prior to this fix, scan()'s in_index only ever consulted MEMORY.md's OWN
# index, so every shard-resident note (one whose only listing lives inside a
# MEMORY.<topic>.md shard file) read in_index=False even when its shard was
# correctly pointed to from the core index. Separately, list_fact_files()
# scanned MEMORY.<topic>.md shard files themselves as if they were individual
# fact notes. And compute_budget() only ever measured MEMORY.md, never the
# shard files that already exist on disk. All three fixes are read-only
# reporting fixes — no frontmatter write path is touched.

def test_list_fact_files_excludes_shard_files_not_just_memory_md():
    files = mg.list_fact_files(SHARDED_FIXTURE)
    assert "MEMORY.watering.md" not in files
    assert "MEMORY.tools.md" not in files
    assert "MEMORY.orphan.md" not in files
    assert "MEMORY.md" not in files
    # Real fact notes (including ones that live only inside a shard's own
    # index) are still listed — they are real files on disk either way.
    for real in ("core-note.md", "garden-hose-leak.md", "sprinkler-timer.md",
                 "old-mower-brand.md", "orphan-note.md"):
        assert real in files


def test_scan_excludes_shard_files_from_entries():
    """scan()'s per-file entries are fact notes only — a shard file is
    structure, not a note, and must never appear as one of its own entries."""
    result = mg.scan(SHARDED_FIXTURE, TODAY)
    names = {e["file"] for e in result["files"]}
    assert "MEMORY.watering.md" not in names
    assert "MEMORY.tools.md" not in names
    assert "MEMORY.orphan.md" not in names


def test_scan_in_index_true_for_note_resident_in_a_correctly_pointed_shard():
    """The core fix: a note whose ONLY listing is inside MEMORY.watering.md
    (itself correctly pointed to by a JIT pointer line in MEMORY.md's own
    index) must read in_index=True — not just notes listed directly in
    MEMORY.md."""
    result = mg.scan(SHARDED_FIXTURE, TODAY)
    by = {e["file"]: e for e in result["files"]}
    assert by["garden-hose-leak.md"]["in_index"] is True
    assert by["sprinkler-timer.md"]["in_index"] is True
    assert by["old-mower-brand.md"]["in_index"] is True
    # A note listed directly in the core index still works as before.
    assert by["core-note.md"]["in_index"] is True


def test_scan_in_index_false_for_note_in_an_unreferenced_orphan_shard():
    """Material decision (Dn): a shard-resident note counts as indexed only
    when its SHARD is itself reachable via a pointer line in MEMORY.md's core
    index — not merely by existing as a MEMORY.<topic>.md file on disk. An
    orphan shard (no pointer line pointing at it) still leaves its notes
    genuinely un-indexed, because nothing would ever load that shard JIT."""
    result = mg.scan(SHARDED_FIXTURE, TODAY)
    by = {e["file"]: e for e in result["files"]}
    assert by["orphan-note.md"]["in_index"] is False


def test_compute_budget_reports_live_shard_sizes_alongside_core():
    """budget's per-shard reporting is unconditional — it reports on shard
    files that ALREADY exist on disk, regardless of whether MEMORY.md itself
    is currently over or under its own ceiling (report-only; the ceiling
    still applies only to MEMORY.md, never to the shard files)."""
    res = mg.compute_budget(SHARDED_FIXTURE, max_tokens=100000, max_lines=100000,
                            taxonomy=GARDEN_TAXONOMY, today=TODAY)
    assert res["over_budget"] is False  # ceiling still applies only to MEMORY.md
    shards = {s["file"]: s for s in res["live_shards"]}
    # All three shard files present on disk are reported, including the
    # orphan one — this is a physical inventory, not an index-correctness
    # check (that distinction is scan()'s job, tested above).
    assert set(shards) == {"MEMORY.watering.md", "MEMORY.tools.md", "MEMORY.orphan.md"}
    for name, s in shards.items():
        expected = mg.measure_text(mg._read(os.path.join(SHARDED_FIXTURE, name)))
        assert s["measure"] == expected


def test_compute_budget_live_shards_empty_when_store_has_no_shards():
    """No new gating: an un-sharded store (the plain FIXTURE) reports an empty
    live_shards list, not an error or a behavior change."""
    res = mg.compute_budget(FIXTURE, max_tokens=100000, max_lines=100000,
                            taxonomy=GARDEN_TAXONOMY, today=TODAY)
    assert res["live_shards"] == []


def test_render_budget_report_includes_live_shard_sizes():
    res = mg.compute_budget(SHARDED_FIXTURE, 100000, 100000, GARDEN_TAXONOMY, TODAY)
    text = mg.render_budget_report(res)
    assert "MEMORY.watering.md" in text
    assert "MEMORY.tools.md" in text
    assert "MEMORY.orphan.md" in text


def test_render_budget_report_omits_live_shard_section_when_unsharded():
    res = mg.compute_budget(FIXTURE, 100000, 100000, GARDEN_TAXONOMY, TODAY)
    text = mg.render_budget_report(res)
    assert "Existing topic shards" not in text


def test_budget_never_mutates_sharded_memory_dir(tmp_path):
    """The new per-shard measurement is read-only, same guarantee as the rest
    of budget."""
    work = tmp_path / "memory_sharded"
    shutil.copytree(SHARDED_FIXTURE, work)
    before = _dir_snapshot(str(work))
    res = mg.compute_budget(str(work), 100000, 100000, GARDEN_TAXONOMY, TODAY)
    mg.render_budget_report(res)
    after = _dir_snapshot(str(work))
    assert before == after


# =========================================================================== #
# shard --extend — sanctioned "move overflow lines into an EXISTING shard"
# (issue #175, BK4). Distinct precondition from `shard`: `shard`
# refuses when the core IS already sharded; `extend` refuses when it is NOT.
# Named seams: (a) propose/dry-run — prints what would move, touches nothing;
# (b) --apply execute — core shrinks by exactly the moved lines, target shard
# gains them verbatim; (c) drift guard — refuses on a stale --expect-sha256;
# (d) no-loss — every original bullet still exists somewhere after the move,
# including an adversarial case proving the assertion actually catches a
# silent drop.
# =========================================================================== #

def _seed_extend(tmp_path):
    """A fresh writable copy of the extend fixture memory dir + its core sha."""
    work = tmp_path / "memory_extend"
    shutil.copytree(EXTEND_FIXTURE, work)
    sha = mg.file_sha256(str(work / "MEMORY.md"))
    return work, sha


# --------------------------------------------------- (a) propose / dry-run

def test_propose_extend_identifies_overflow_into_existing_shard_only():
    prop = mg.propose_extend(EXTEND_FIXTURE, GARDEN_TAXONOMY)
    assert prop["already_sharded"] is True
    assert {s["name"] for s in prop["shards"]} == {"watering"}

    watering = next(s for s in prop["shards"] if s["name"] == "watering")
    assert watering["moved_count"] == 1
    assert any("Sprinkler timer" in ln for ln in watering["lines"])
    assert prop["moved_total"] == 1

    # The moved note's bullet leaves the core and lands in its shard.
    assert "sprinkler-timer.md" not in prop["new_core_text"]
    assert "sprinkler-timer.md" in prop["new_shard_texts"]["MEMORY.watering.md"]
    # A note classifying to a shard with NO existing file is left alone —
    # extend never creates a new shard.
    assert "compost-bin-setup.md" in prop["new_core_text"]
    assert "MEMORY.compost.md" not in prop["new_shard_texts"]
    # An unmatched note is untouched either way.
    assert "patio-umbrella-warranty.md" in prop["new_core_text"]
    # The untouched (tools) shard never appears in new_shard_texts at all.
    assert "MEMORY.tools.md" not in prop["new_shard_texts"]


def test_propose_extend_refreshes_only_the_touched_pointer():
    prop = mg.propose_extend(EXTEND_FIXTURE, GARDEN_TAXONOMY)
    # Watering pointer refreshed to 2 notes (1 original + 1 moved-in).
    assert "· 2 notes ·" in prop["new_core_text"]
    assert "MEMORY.watering.md" in prop["new_core_text"]
    # Tools pointer line is byte-identical to the original (still 1 note).
    original_tools_line = next(
        ln for ln in prop["core_text"].splitlines() if "MEMORY.tools.md" in ln)
    assert original_tools_line in prop["new_core_text"]


def test_propose_extend_not_sharded_yet():
    prop = mg.propose_extend(FIXTURE, GARDEN_TAXONOMY)   # plain, un-sharded
    assert prop["already_sharded"] is False
    assert prop["shards"] == []
    assert prop["new_core_text"] == prop["core_text"]    # untouched


def test_render_extend_dryrun_not_sharded_yet():
    text = mg.render_extend_dryrun(FIXTURE, GARDEN_TAXONOMY)
    assert "not sharded yet" in text
    assert "shard --apply" in text


def test_render_extend_dryrun_nothing_to_move(tmp_path):
    work = tmp_path / "memory_extend"
    shutil.copytree(EXTEND_FIXTURE, work)
    (work / "sprinkler-timer.md").unlink()
    core = (work / "MEMORY.md").read_text()
    kept = [ln for ln in core.splitlines() if "sprinkler-timer.md" not in ln]
    (work / "MEMORY.md").write_text("\n".join(kept) + "\n")

    text = mg.render_extend_dryrun(str(work), GARDEN_TAXONOMY)
    assert "nothing to move" in text
    assert "never creates a new shard" in text


def test_render_extend_dryrun_shows_summary_and_would_write_blocks():
    text = mg.render_extend_dryrun(EXTEND_FIXTURE, GARDEN_TAXONOMY)
    for token in ("shard EXTEND dry run", "## Summary", "Watering & irrigation",
                  "WOULD WRITE `MEMORY.md`", "WOULD WRITE `MEMORY.watering.md`",
                  "--expect-sha256", "```markdown"):
        assert token in text
    # The untouched tools shard has no "WOULD WRITE" block of its own.
    assert "WOULD WRITE `MEMORY.tools.md`" not in text


def test_extend_dryrun_writes_nothing(tmp_path):
    work, _ = _seed_extend(tmp_path)
    before = _dir_snapshot(str(work))
    tax = tmp_path / "tax.json"
    tax.write_text(_dumps_garden_taxonomy())
    rc = mg.main(["shard", "--extend", "--dir", str(work), "--taxonomy", str(tax),
                  "--today", "2026-07-08"])
    assert rc == 0
    assert _dir_snapshot(str(work)) == before
    assert not (work / "archive").exists()


# --------------------------------------------- (b) --apply execute, verbatim

def test_extend_apply_moves_overflow_and_updates_pointer(tmp_path):
    work, sha = _seed_extend(tmp_path)
    tools_before = (work / "MEMORY.tools.md").read_bytes()
    compost_note_before = (work / "compost-bin-setup.md").read_bytes()

    res = mg.execute_extend(str(work), GARDEN_TAXONOMY, TODAY, expect_sha256=sha)
    assert res["moved_total"] == 1
    assert any("moved 1 note(s) into MEMORY.watering.md" in a for a in res["actions"])

    core = (work / "MEMORY.md").read_text()
    assert "sprinkler-timer.md" not in core              # moved out
    assert "compost-bin-setup.md" in core                # no existing shard -> left alone
    assert "patio-umbrella-warranty.md" in core           # never matched -> untouched
    assert "· 2 notes ·" in core                          # watering pointer refreshed
    assert "· 1 note ·" in core                           # tools pointer unchanged

    watering = (work / "MEMORY.watering.md").read_text()
    assert "garden-hose-leak.md" in watering              # original note, still there
    assert "sprinkler-timer.md" in watering               # newly moved-in note

    # The untouched shard and every real note file are byte-identical.
    assert (work / "MEMORY.tools.md").read_bytes() == tools_before
    assert (work / "compost-bin-setup.md").read_bytes() == compost_note_before


def test_extend_apply_matches_dryrun_exactly(tmp_path):
    """Rehearsal parity: propose_extend's computed new_core_text /
    new_shard_texts (what render_extend_dryrun's 'WOULD WRITE' blocks print
    verbatim) must be BYTE-IDENTICAL to what execute_extend actually writes."""
    work, sha = _seed_extend(tmp_path)
    prop = mg.propose_extend(str(work), GARDEN_TAXONOMY)

    mg.execute_extend(str(work), GARDEN_TAXONOMY, TODAY, expect_sha256=sha)

    assert (work / "MEMORY.md").read_text() == prop["new_core_text"]
    assert prop["new_shard_texts"]                        # sanity: something moved
    for shard_file, expected in prop["new_shard_texts"].items():
        assert (work / shard_file).read_text() == expected
    backup = work / "archive" / mg._shard_backup_name(TODAY)
    assert backup.read_text() == prop["core_text"]


def test_main_shard_extend_dryrun_and_apply(tmp_path):
    work, sha = _seed_extend(tmp_path)
    tax = tmp_path / "tax.json"
    tax.write_text(_dumps_garden_taxonomy())

    rc = mg.main(["shard", "--extend", "--dir", str(work), "--taxonomy", str(tax),
                  "--today", "2026-07-08"])
    assert rc == 0
    assert not (work / "archive").exists()                # dry-run: nothing written

    rc = mg.main(["shard", "--extend", "--dir", str(work), "--taxonomy", str(tax),
                  "--apply", "--expect-sha256", sha, "--today", "2026-07-08"])
    assert rc == 0
    assert "sprinkler-timer.md" not in (work / "MEMORY.md").read_text()
    assert "sprinkler-timer.md" in (work / "MEMORY.watering.md").read_text()


def test_shard_help_mentions_extend():
    """--extend must be discoverable via --help (row's own requirement)."""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        mg.main(["shard", "--help"])
    assert "--extend" in buf.getvalue()


# --------------------------------------------------------- (c) drift guards

def test_extend_apply_refuses_without_expect_sha256(tmp_path):
    work, _ = _seed_extend(tmp_path)
    before = _dir_snapshot(str(work))
    with pytest.raises(ValueError, match="requires --expect-sha256"):
        mg.execute_extend(str(work), GARDEN_TAXONOMY, TODAY, expect_sha256=None)
    assert _dir_snapshot(str(work)) == before


def test_extend_apply_refuses_on_stale_hash(tmp_path):
    work, _ = _seed_extend(tmp_path)
    before = _dir_snapshot(str(work))
    with pytest.raises(ValueError, match="STALE PLAN"):
        mg.execute_extend(str(work), GARDEN_TAXONOMY, TODAY, expect_sha256="0" * 64)
    assert _dir_snapshot(str(work)) == before


def test_main_extend_apply_wrong_hash_exit_2(tmp_path):
    work, _ = _seed_extend(tmp_path)
    tax = tmp_path / "tax.json"
    tax.write_text(_dumps_garden_taxonomy())
    before = _dir_snapshot(str(work))
    rc = mg.main(["shard", "--extend", "--dir", str(work), "--taxonomy", str(tax),
                  "--apply", "--expect-sha256", "0" * 64, "--today", "2026-07-08"])
    assert rc == 2
    assert _dir_snapshot(str(work)) == before


def test_extend_apply_refuses_when_core_not_sharded(tmp_path):
    """The INVERSE of shard's own already-sharded refusal: extend requires a
    core that IS already sharded."""
    work = tmp_path / "memory"
    shutil.copytree(FIXTURE, work)
    sha = mg.file_sha256(str(work / "MEMORY.md"))
    before = _dir_snapshot(str(work))
    with pytest.raises(ValueError, match="NOT SHARDED"):
        mg.execute_extend(str(work), GARDEN_TAXONOMY, TODAY, expect_sha256=sha)
    assert _dir_snapshot(str(work)) == before


def test_extend_apply_refuses_when_nothing_to_move(tmp_path):
    work = tmp_path / "memory_extend"
    shutil.copytree(EXTEND_FIXTURE, work)
    (work / "sprinkler-timer.md").unlink()
    core = (work / "MEMORY.md").read_text()
    kept = [ln for ln in core.splitlines() if "sprinkler-timer.md" not in ln]
    (work / "MEMORY.md").write_text("\n".join(kept) + "\n")
    sha = mg.file_sha256(str(work / "MEMORY.md"))
    before = _dir_snapshot(str(work))
    with pytest.raises(ValueError, match="nothing to extend"):
        mg.execute_extend(str(work), GARDEN_TAXONOMY, TODAY, expect_sha256=sha)
    assert _dir_snapshot(str(work)) == before


def test_extend_apply_backs_up_core(tmp_path):
    work, sha = _seed_extend(tmp_path)
    original = (work / "MEMORY.md").read_text()
    mg.execute_extend(str(work), GARDEN_TAXONOMY, TODAY, expect_sha256=sha)
    backup = work / "archive" / "MEMORY.pre-shard-2026-07-08.md"
    assert backup.exists()
    assert backup.read_text() == original


def test_extend_apply_never_overwrites_existing_backup(tmp_path):
    work, sha = _seed_extend(tmp_path)
    (work / "archive").mkdir()
    prior = work / "archive" / "MEMORY.pre-shard-2026-07-08.md"
    prior.write_text("SENTINEL prior backup — must not be clobbered\n")
    original_core = (work / "MEMORY.md").read_text()

    res = mg.execute_extend(str(work), GARDEN_TAXONOMY, TODAY, expect_sha256=sha)

    assert prior.read_text() == "SENTINEL prior backup — must not be clobbered\n"
    bumped = work / "archive" / "MEMORY.pre-shard-2026-07-08-2.md"
    assert bumped.exists()
    assert bumped.read_text() == original_core
    assert res["backup"].endswith("MEMORY.pre-shard-2026-07-08-2.md")


def test_extend_apply_reads_memory_md_at_most_twice(tmp_path, monkeypatch):
    """Was 'exactly once' before BQ8: guard 1's single read still builds the
    hash AND the whole plan from ONE set of bytes (no re-read between
    hashing and proposing — the original TOCTOU this test was written to
    catch). BQ8 added a SECOND, deliberate read — guard 6's apply-time
    revalidation, immediately before MEMORY.md's own os.replace in the
    commit loop — which is a new invariant (closing a different, later
    race window), not a regression of this one. Exactly 2, never more:
    a 3rd would mean something re-read it again besides those two guards."""
    work, sha = _seed_extend(tmp_path)
    memory_md = str(work / "MEMORY.md")

    import builtins
    real_open = builtins.open
    reads = {"n": 0}

    def counting_open(path, mode="r", *args, **kwargs):
        if os.path.abspath(str(path)) == os.path.abspath(memory_md) \
                and "w" not in str(mode) and "a" not in str(mode):
            reads["n"] += 1
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    mg.execute_extend(str(work), GARDEN_TAXONOMY, TODAY, expect_sha256=sha)
    monkeypatch.undo()
    assert reads["n"] == 2


# =========================================================================== #
# BK4 codex round-2 findings — dry-run single-read parity + symlink-safe
# staging. Both scoped to code THIS row introduced (render_extend_dryrun /
# execute_extend's own staging loop) — execute_shard's pre-existing sibling
# code is untouched, tracked as a separate follow-up.
# =========================================================================== #

def test_extend_dryrun_reads_memory_md_exactly_once(tmp_path, monkeypatch):
    """The dry-run used to read MEMORY.md separately for the rendered
    proposal (inside propose_extend) vs. the displayed sha256 (file_sha256)
    — two reads that could observe different content if the file changed
    between them. Now both come from ONE read, mirroring execute_extend's
    own single-read discipline."""
    work, _ = _seed_extend(tmp_path)
    memory_md = str(work / "MEMORY.md")

    import builtins
    real_open = builtins.open
    reads = {"n": 0}

    def counting_open(path, mode="r", *args, **kwargs):
        if os.path.abspath(str(path)) == os.path.abspath(memory_md) \
                and "w" not in str(mode) and "a" not in str(mode):
            reads["n"] += 1
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    mg.render_extend_dryrun(str(work), GARDEN_TAXONOMY)
    monkeypatch.undo()
    assert reads["n"] == 1


def test_extend_dryrun_hash_matches_the_text_used_for_the_proposal(tmp_path):
    """The displayed sha256 must be over the exact same bytes the rendered
    proposal was built from, not a second independently-read copy."""
    work, sha = _seed_extend(tmp_path)
    text = mg.render_extend_dryrun(str(work), GARDEN_TAXONOMY)
    assert sha in text
    assert sha == mg.file_sha256(str(work / "MEMORY.md"))


def test_extend_apply_refuses_to_follow_a_preexisting_symlink_at_tmp_path(tmp_path):
    """SECURITY: if a symlink already sits at a staging `.tmp-extend` path
    (e.g. pointing outside the memory dir), execute_extend must refuse to
    open through it — never silently truncate/overwrite the symlink's
    target. Nothing is written; the real files stay untouched."""
    work, sha = _seed_extend(tmp_path)
    outside_target = tmp_path / "outside-sentinel.md"
    outside_target.write_text("SENTINEL — must never be touched\n")
    original_core = (work / "MEMORY.md").read_text()

    core_tmp = work / "MEMORY.md.tmp-extend"
    core_tmp.symlink_to(outside_target)

    with pytest.raises(OSError):
        mg.execute_extend(str(work), GARDEN_TAXONOMY, TODAY, expect_sha256=sha)

    # The symlink's target must be untouched — the whole point of the fix:
    # O_EXCL|O_NOFOLLOW refuses to open through a pre-existing symlink at
    # all, rather than following it and truncating whatever it points to.
    assert outside_target.read_text() == "SENTINEL — must never be touched\n"
    assert (work / "MEMORY.md").read_text() == original_core   # untouched
    # No committed backup landed — the failure happened before any real
    # (non-tmp) file was ever replaced into place.
    archive = work / "archive"
    committed_backups = list(archive.glob("MEMORY.pre-shard*")) if archive.exists() else []
    assert committed_backups == []


def test_extend_apply_does_not_delete_a_preexisting_tmp_path_it_did_not_create(tmp_path):
    """Cleanup-on-failure must only remove tmp files THIS run actually
    created (staged AFTER success) — never a pre-existing artifact (like the
    planted symlink above) that happened to occupy a `.tmp-extend` path."""
    work, sha = _seed_extend(tmp_path)
    core_tmp = work / "MEMORY.md.tmp-extend"
    core_tmp.symlink_to(work / "MEMORY.md")   # any pre-existing symlink

    with pytest.raises(OSError):
        mg.execute_extend(str(work), GARDEN_TAXONOMY, TODAY, expect_sha256=sha)

    # The planted symlink itself must still be exactly there — cleanup never
    # touched a path it didn't create.
    assert core_tmp.is_symlink()


# issue #183 finding 1 (BM10) — execute_extend's own sibling of the
# execute_shard crash-recoverability tests above (see _flaky_replace and the
# comment beside test_shard_apply_crash_mid_commit_leaves_every_note_recoverable
# for the shared invariant). extend's fixture touches only the watering shard,
# so there are 3 commits (backup, core, watering) — fail_after in [1, 2]
# covers every crash point short of full success.

@pytest.mark.parametrize("fail_after", [1, 2])
def test_extend_apply_crash_mid_commit_leaves_every_note_recoverable(
        tmp_path, monkeypatch, fail_after):
    work, sha = _seed_extend(tmp_path)
    before = mg.scan(str(work), TODAY)
    assert all(f["in_index"] for f in before["files"]), \
        "fixture sanity: every note must start indexed"

    _flaky_replace(monkeypatch, fail_after)
    with pytest.raises(OSError, match="simulated crash"):
        mg.execute_extend(str(work), GARDEN_TAXONOMY, TODAY, expect_sha256=sha)
    monkeypatch.undo()

    after = mg.scan(str(work), TODAY)
    orphaned = [f["file"] for f in after["files"] if not f["in_index"]]
    assert orphaned == [], (
        "crash after %d of 3 commit(s) orphaned note(s) %r — the store is "
        "neither fully old nor fully new" % (fail_after, orphaned))


# issue #183 finding 1 follow-up (codex second-review of BM10, PR
# #192): core-last ordering keeps every note REACHABLE across a mid-apply
# crash (the test above), but a crash landing specifically between the
# touched shard's commit and the (never-reached) core commit leaves a shape
# that a plain retry mishandles — the shard already holds the moved bullet,
# but MEMORY.md was never rewritten to drop it, so re-hashing the unchanged
# core and re-running --apply proposes moving the SAME bullet again.
# check_extend_no_loss is a multiset check and can't catch this (the
# duplicate already sits on both sides of the before/after comparison), so
# without a dedicated guard the retry would succeed and silently duplicate
# the note in the shard. This asserts the guard refuses instead, and that
# the refusal happens BEFORE any write (no new backup, no mutated shard, no
# leftover temp file — the dir is byte-identical to its post-crash state).
def test_extend_retry_after_shard_committed_core_not_refuses_instead_of_duplicating(
        tmp_path, monkeypatch):
    work, sha = _seed_extend(tmp_path)

    # outputs order = [backup, watering-shard, core]; fail_after=2 lands the
    # crash exactly between the shard's commit and the core's.
    _flaky_replace(monkeypatch, 2)
    with pytest.raises(OSError, match="simulated crash"):
        mg.execute_extend(str(work), GARDEN_TAXONOMY, TODAY, expect_sha256=sha)
    monkeypatch.undo()

    # Fixture sanity: this is the exact partial-crash shape the guard exists
    # for — the shard already has the bullet, the (unchanged) core still
    # lists it too.
    assert "Sprinkler timer" in (work / "MEMORY.watering.md").read_text()
    assert "Sprinkler timer" in (work / "MEMORY.md").read_text()

    post_crash_snapshot = _dir_snapshot(str(work))
    # MEMORY.md is untouched by the crashed run (core commits last), so its
    # sha256 for a plain retry is unchanged from the original --expect-sha256.
    retry_sha = mg.file_sha256(str(work / "MEMORY.md"))
    assert retry_sha == sha

    with pytest.raises(ValueError, match="POSSIBLE PARTIAL RETRY"):
        mg.execute_extend(str(work), GARDEN_TAXONOMY, TODAY, expect_sha256=retry_sha)

    # Refused before any write: no new backup, no duplicated bullet, no
    # leftover .tmp-extend file — dir identical to its post-crash state.
    assert _dir_snapshot(str(work)) == post_crash_snapshot
    assert (work / "MEMORY.watering.md").read_text().count("Sprinkler timer") == 1


def test_extend_apply_is_atomic_on_midwrite_failure(tmp_path, monkeypatch):
    work, sha = _seed_extend(tmp_path)
    before = _dir_snapshot(str(work))

    import builtins
    real_open = builtins.open
    state = {"n": 0}

    def flaky_open(path, mode="r", *args, **kwargs):
        if "w" in str(mode) and str(path).endswith(".tmp-extend"):
            state["n"] += 1
            if state["n"] == 2:                        # fail on the 2nd output
                raise OSError("simulated disk failure on 2nd staged output")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", flaky_open)
    with pytest.raises(OSError, match="simulated disk failure"):
        mg.execute_extend(str(work), GARDEN_TAXONOMY, TODAY, expect_sha256=sha)
    monkeypatch.undo()

    assert _dir_snapshot(str(work)) == before
    leftovers = [os.path.join(r, f)
                 for r, _d, fs in os.walk(work) for f in fs
                 if f.endswith(".tmp-extend")]
    assert leftovers == []
    if (work / "archive").exists():
        assert [f for f in os.listdir(work / "archive")
                if f.startswith("MEMORY.pre-shard")] == []


# BQ8 (issue #208's own PR body — codex HIGH finding on this
# function, confirmed unmodified by that diff): the guards above all
# validate the ONE snapshot read at the very top of execute_extend, but
# everything between that read and the commit loop — propose_extend's
# classification, check_extend_no_loss's traversal — is a real window in
# which the store could be mutated by something else. Without a recheck
# immediately before each file's own os.replace, that mutation would be
# silently clobbered by output built from the stale snapshot instead of
# being caught.
def test_extend_apply_racing_shard_mutation_fails_closed(tmp_path, monkeypatch):
    """A concurrent writer that appends to the target shard AFTER
    execute_extend's snapshot read but BEFORE its commit loop must be
    detected and refused — not silently overwritten. Races in via
    check_extend_no_loss, the last snapshot-derived computation before the
    commit loop begins, so the injected mutation lands squarely inside the
    window guard 6 exists to close."""
    work, sha = _seed_extend(tmp_path)
    before = _dir_snapshot(str(work))
    watering = work / "MEMORY.watering.md"
    racing_content = watering.read_text() + "- [Race note] — landed mid-apply\n"

    real_check = mg.check_extend_no_loss

    def racing_check(*args, **kwargs):
        result = real_check(*args, **kwargs)
        watering.write_text(racing_content)   # concurrent mutation, mid-apply
        return result

    monkeypatch.setattr(mg, "check_extend_no_loss", racing_check)
    with pytest.raises(ValueError, match="STORE CHANGED MID-APPLY"):
        mg.execute_extend(str(work), GARDEN_TAXONOMY, TODAY, expect_sha256=sha)
    monkeypatch.undo()

    # Fails CLOSED: the racing shard write survives untouched (not
    # clobbered), the core is never rewritten, and no partial/leftover
    # output lands anywhere.
    assert watering.read_text() == racing_content
    leftovers = [os.path.join(r, f)
                 for r, _d, fs in os.walk(work) for f in fs
                 if f.endswith(".tmp-extend")]
    assert leftovers == []
    # No partial write to any DATA-bearing file: the core must never be
    # rewritten once a touched shard has drifted (backup commits first in
    # commit order — issue #183 finding 1 — so an extra pre-change
    # backup landing under archive/ before the drift is caught is the one
    # tolerated additive artifact, same as execute_shard/execute_extend's
    # other mid-commit-failure tests above; it is a verbatim copy of the
    # untouched original, never the ONLY committed output, so no note is
    # ever lost or duplicated).
    after = _dir_snapshot(str(work))
    changed = {k for k in after if before.get(k) != after[k]}
    assert "MEMORY.md" not in changed, "core must never be rewritten " \
        "once a touched shard has drifted mid-apply"
    unexpected = {k for k in changed
                  if k != "MEMORY.watering.md" and not k.startswith("archive" + os.sep)}
    assert unexpected == set(), unexpected


# ------------------------------------------------------- (d) no-loss invariant

def test_check_extend_no_loss_raises_on_dropped_bullet():
    """Adversarial: a produced set where the moved bullet is removed from the
    core but never added anywhere else — silently dropped, exactly the class
    of bug this invariant exists to catch. Calls check_extend_no_loss
    directly, proving the assertion itself is armed."""
    core = mg._read(os.path.join(EXTEND_FIXTURE, "MEMORY.md"))
    watering = mg._read(os.path.join(EXTEND_FIXTURE, "MEMORY.watering.md"))
    tools = mg._read(os.path.join(EXTEND_FIXTURE, "MEMORY.tools.md"))
    original_pairs = [("MEMORY.md", core),
                      ("MEMORY.watering.md", watering),
                      ("MEMORY.tools.md", tools)]

    broken_core = "\n".join(
        ln for ln in core.splitlines() if "sprinkler-timer.md" not in ln) + "\n"
    produced_pairs = [("MEMORY.md", broken_core),
                      ("MEMORY.watering.md", watering),   # bug: never gained the line
                      ("MEMORY.tools.md", tools)]

    with pytest.raises(ValueError, match="NO-LOSS INVARIANT VIOLATED"):
        mg.check_extend_no_loss(original_pairs, produced_pairs)


def test_check_extend_no_loss_passes_on_a_correct_move():
    """Positive control: a correctly-produced set (bullet removed from the
    core, added verbatim to its shard) passes cleanly — proves the invariant
    doesn't false-positive on the legitimate case."""
    core = mg._read(os.path.join(EXTEND_FIXTURE, "MEMORY.md"))
    watering = mg._read(os.path.join(EXTEND_FIXTURE, "MEMORY.watering.md"))
    tools = mg._read(os.path.join(EXTEND_FIXTURE, "MEMORY.tools.md"))
    sprinkler_line = next(ln for ln in core.splitlines() if "sprinkler-timer.md" in ln)

    new_core = "\n".join(
        ln for ln in core.splitlines() if "sprinkler-timer.md" not in ln) + "\n"
    new_watering = watering.rstrip("\n") + "\n" + sprinkler_line + "\n"

    original_pairs = [("MEMORY.md", core),
                      ("MEMORY.watering.md", watering),
                      ("MEMORY.tools.md", tools)]
    produced_pairs = [("MEMORY.md", new_core),
                      ("MEMORY.watering.md", new_watering),
                      ("MEMORY.tools.md", tools)]
    mg.check_extend_no_loss(original_pairs, produced_pairs)   # must not raise


def test_check_extend_no_loss_ignores_pointer_text_changes():
    """A touched shard's pointer TEXT changing (count refreshed) is NOT a
    note gain/loss — the invariant must not false-positive on that alone."""
    original_pairs = [
        ("MEMORY.md",
         "- [MEMORY.watering.md](MEMORY.watering.md) — Watering · 1 note · "
         "~10 tok · load JIT when the topic comes up\n"),
    ]
    produced_pairs = [
        ("MEMORY.md",
         "- [MEMORY.watering.md](MEMORY.watering.md) — Watering · 2 notes · "
         "~20 tok · load JIT when the topic comes up\n"),
    ]
    mg.check_extend_no_loss(original_pairs, produced_pairs)   # must not raise


# =========================================================================== #
# issue #175 (BP8) — `budget`'s proposal on an ALREADY-SHARDED store.
# Before this fix, an over-budget already-sharded core got `propose_shards`'s
# proposal (the plain, fresh-split one) verbatim: it does not know the shard
# files already exist, so it MINTS a second, colliding pointer line for each
# touched shard carrying only the overflow count (not the shard's true total)
# — the exact "8 pointer lines, 4 duplicates" defect the issue reports — and
# its "Next step" told the operator to run `shard --apply`, which REFUSES
# outright on an already-sharded core (unconditionally; `--force` does not
# override — see test_reshard_of_already_sharded_core_refuses_and_loses_
# nothing). Named seam: compute_budget/render_budget_report's own interface
# (a pure, read-only pair) plus the `main()` CLI seam for the end-to-end
# proof. No internal collaborator is mocked — `propose_extend` and
# `_core_is_already_sharded` run for real against real fixture files.
# =========================================================================== #

def test_compute_budget_on_already_sharded_store_defers_to_the_extend_proposal():
    """`compute_budget` must recognize an already-sharded, over-budget core
    and stop handing out the broken `propose_shards` proposal (`proposal`
    stays None); it must instead attach the SAME proposal `shard --extend`
    itself would execute (`reshard_proposal`, built via `propose_extend`) —
    reused, not re-derived, so budget's report can never drift from what
    `shard --extend` actually does."""
    res = mg.compute_budget(EXTEND_FIXTURE, max_tokens=100000, max_lines=7,
                            taxonomy=GARDEN_TAXONOMY, today=TODAY)
    assert res["over_budget"] is True
    assert res["over_lines"] is True
    assert res["already_sharded"] is True
    assert res["proposal"] is None                 # the broken path is OFF

    rp = res["reshard_proposal"]
    assert rp is not None
    assert {s["name"] for s in rp["shards"]} == {"watering"}   # NOT "compost"
    watering = next(s for s in rp["shards"] if s["file"] == "MEMORY.watering.md")
    assert watering["moved_count"] == 1
    assert any("sprinkler-timer.md" in ln for ln in watering["lines"])
    # Exactly ONE MEMORY.watering.md pointer in the proposed core — never the
    # duplicate-pointer shape (a fresh mint alongside the untouched original).
    assert rp["new_core_text"].count("(MEMORY.watering.md)") == 1


def test_compute_budget_under_already_sharded_store_is_clean():
    """Sanity: the SAME already-sharded fixture, under a generous ceiling,
    reports clean — proves the already_sharded branch only engages when
    actually over budget (same posture as the plain not-yet-sharded case)."""
    res = mg.compute_budget(EXTEND_FIXTURE, max_tokens=100000, max_lines=100000,
                            taxonomy=GARDEN_TAXONOMY, today=TODAY)
    assert res["over_budget"] is False
    assert res["already_sharded"] is True
    assert res["proposal"] is None
    assert res["reshard_proposal"] is None


def test_render_budget_report_on_already_sharded_store_never_shows_a_duplicate_pointer():
    """The rendered report must not reproduce issue #175's literal symptom —
    two `MEMORY.watering.md` pointer lines in the same preview, one of them
    carrying the wrong (overflow-only) note count — and must point the
    operator at `shard --extend`, not the plain `shard --apply` that
    refuses on this core."""
    res = mg.compute_budget(EXTEND_FIXTURE, max_tokens=100000, max_lines=7,
                            taxonomy=GARDEN_TAXONOMY, today=TODAY)
    text = mg.render_budget_report(res)
    assert text.count("(MEMORY.watering.md)") == 1
    assert "shard --extend" in text
    # The stale "Next step: `shard` (dry-run) ... `shard --apply`" guidance
    # (which dead-ends in an ALREADY SHARDED refusal on this core) is gone.
    assert "shard --apply --expect-sha256" not in text


def test_render_budget_report_already_sharded_nothing_to_move_is_honest():
    """Over budget, already sharded, but nothing classifies to an EXISTING
    shard under this taxonomy — genuinely nothing sanctioned to move. The
    report must say so plainly, not silently claim "within budget" (the
    generic `if not prop` fallback's wording, wrong here since the store
    IS over budget) and not print a broken split proposal either."""
    taxonomy_no_match = [("nothing", "Nothing", ["zzz-does-not-match-zzz"])]
    res = mg.compute_budget(EXTEND_FIXTURE, max_tokens=100000, max_lines=7,
                            taxonomy=taxonomy_no_match, today=TODAY)
    assert res["over_budget"] is True
    assert res["already_sharded"] is True
    assert res["reshard_proposal"] is not None
    assert res["reshard_proposal"]["shards"] == []
    text = mg.render_budget_report(res)
    assert "within budget" not in text
    assert "nothing sanctioned to move" in text or "nothing to move" in text


def test_shard_extend_apply_takes_an_already_sharded_over_budget_store_to_budget_zero(tmp_path):
    """End-to-end proof (issue #175's own ask): an already-sharded store over
    budget ONLY on the LINE axis — the axis the issue reports as having "no
    sanctioned path back under the ceiling" — via the full CLI: `budget`
    fires exit 3, `shard --extend --apply` is the sanctioned mutating path
    (plain `shard --apply`, no `--extend`, still refuses on this core,
    unchanged — see test_reshard_of_already_sharded_core_refuses_and_loses_
    nothing), and a second `budget` run afterward reaches exit 0."""
    work, sha = _seed_extend(tmp_path)
    tax = tmp_path / "tax.json"
    tax.write_text(_dumps_garden_taxonomy())

    rc_over = mg.main(["budget", "--dir", str(work), "--max-tokens", "100000",
                       "--max-lines", "7", "--taxonomy", str(tax),
                       "--today", "2026-07-08"])
    assert rc_over == mg.OVER_BUDGET_EXIT

    # Plain `shard --apply` (no --extend) still refuses — unchanged.
    rc_plain_refuses = mg.main(["shard", "--dir", str(work), "--apply",
                                "--expect-sha256", sha, "--taxonomy", str(tax),
                                "--today", "2026-07-08"])
    assert rc_plain_refuses == 2

    rc_apply = mg.main(["shard", "--dir", str(work), "--extend", "--apply",
                        "--expect-sha256", sha, "--taxonomy", str(tax),
                        "--today", "2026-07-08"])
    assert rc_apply == 0

    rc_after = mg.main(["budget", "--dir", str(work), "--max-tokens", "100000",
                        "--max-lines", "7", "--taxonomy", str(tax),
                        "--today", "2026-07-08"])
    assert rc_after == 0


def test_main_budget_json_already_sharded_carries_reshard_proposal(tmp_path, capsys):
    import json
    work = tmp_path / "memory_extend"
    shutil.copytree(EXTEND_FIXTURE, work)
    tax = tmp_path / "tax.json"
    tax.write_text(_dumps_garden_taxonomy())
    mg.main(["budget", "--dir", str(work), "--max-tokens", "100000",
             "--max-lines", "7", "--taxonomy", str(tax), "--json",
             "--today", "2026-07-08"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["already_sharded"] is True
    assert payload["proposal"] is None
    assert payload["reshard_proposal"]["shards"]


# =========================================================================== #
# BQ9 (BP8's D3 follow-up) — `render_shard_dryrun` (plain `shard`, no
# `--extend`) on an ALREADY-SHARDED store. Before this fix it called
# `propose_shards` unconditionally, the same broken-on-already-sharded shape
# issue #175/BP8 already closed for `compute_budget`: no notion the
# shard files already exist, so it would preview a fresh split that
# `--apply` (no `--extend`) refuses outright and that doesn't match what
# `--extend --apply` actually writes. Named seam: render_shard_dryrun's own
# already-sharded branch, mirroring compute_budget's exactly.
# =========================================================================== #

def test_render_shard_dryrun_on_already_sharded_store_previews_extend_path():
    """The plain `shard` dry-run on an already-sharded core must be
    BYTE-IDENTICAL to `shard --extend`'s own dry-run for the same store —
    proving it delegates to the SAME propose_extend-backed proposal rather
    than handing back propose_shards' broken fresh-split preview."""
    plain = mg.render_shard_dryrun(EXTEND_FIXTURE, GARDEN_TAXONOMY)
    extend = mg.render_extend_dryrun(EXTEND_FIXTURE, GARDEN_TAXONOMY)
    assert plain == extend
    assert "shard EXTEND dry run" in plain
    assert "topic-shard DRY RUN" not in plain      # never the broken shape
    # Never issue #175's literal symptom: a second, colliding pointer to a
    # shard that already exists on disk.
    assert plain.count("(MEMORY.watering.md)") == 1


def test_render_shard_dryrun_on_already_sharded_store_matches_actual_apply(tmp_path):
    """Stronger than byte-parity with the other renderer alone: the
    previewed 'WOULD WRITE' core + shard content must be EXACTLY what
    `shard --extend --apply` really commits to disk for the same store —
    i.e. the plain dry-run's preview now matches what an actual apply
    would do, not just what a differently-named dry-run prints."""
    work, sha = _seed_extend(tmp_path)
    preview = mg.render_shard_dryrun(str(work), GARDEN_TAXONOMY)

    mg.execute_extend(str(work), GARDEN_TAXONOMY, TODAY, expect_sha256=sha)

    assert (work / "MEMORY.md").read_text().rstrip("\n") in preview
    watering = work / "MEMORY.watering.md"
    assert watering.exists()
    assert watering.read_text().rstrip("\n") in preview


def test_render_shard_dryrun_on_not_yet_sharded_store_is_unchanged():
    """Sanity: the pre-existing fresh-split preview for a store that is NOT
    already sharded is untouched by this fix — the new branch only engages
    when `_core_is_already_sharded` is True, same posture as
    compute_budget's own already_sharded branch."""
    text = mg.render_shard_dryrun(FIXTURE, GARDEN_TAXONOMY)
    assert "topic-shard DRY RUN" in text
    assert "shard EXTEND dry run" not in text


def test_render_shard_dryrun_already_sharded_sha_matches_snapshot_not_a_race(
        tmp_path, monkeypatch):
    """codex round-2 (BQ9 follow-up): the already-sharded branch used to build
    its previewed proposal from one read of MEMORY.md but display a SEPARATE,
    later read's sha256 (`file_sha256(memory_md)`, evaluated after
    `propose_extend` had already run) — a writer landing between the two
    reads could make the shown hash describe different bytes than the
    proposal actually shown, a TOCTOU-style inconsistency. Mutates the file
    from inside `propose_extend` itself — the exact call that used to sit
    between the two reads — and confirms the returned preview and its
    displayed sha both still describe the ORIGINAL, pre-mutation snapshot:
    proof `text` and `sha` are now derived from one atomic read taken BEFORE
    `propose_extend` is even called, mirroring `render_extend_dryrun`'s own
    single-read discipline."""
    work, _sha = _seed_extend(tmp_path)
    memory_md = work / "MEMORY.md"
    original_sha = mg.file_sha256(str(memory_md))
    mutated = memory_md.read_text() + "\n<!-- concurrent mutation mid-render -->\n"

    real_propose_extend = mg.propose_extend

    def racing_propose_extend(*args, **kwargs):
        result = real_propose_extend(*args, **kwargs)
        memory_md.write_text(mutated)   # concurrent writer, mid-render
        return result

    monkeypatch.setattr(mg, "propose_extend", racing_propose_extend)
    out = mg.render_shard_dryrun(str(work), GARDEN_TAXONOMY)
    monkeypatch.undo()

    # The mutation actually landed (proves the race window is real, not a
    # no-op)...
    assert memory_md.read_text() == mutated
    # ...but the preview must still describe the ORIGINAL snapshot: its
    # displayed sha is the pre-mutation hash, never the post-mutation one.
    assert ("Current `MEMORY.md` sha256: `%s`" % original_sha) in out
    assert mg.file_sha256(str(memory_md)) != original_sha, \
        "test didn't actually mutate the file — assertion above proves nothing"
    assert "shard EXTEND dry run" in out


# --------------------------------------------------------------------------- #
# Shard-aware index writes, nested-bracket titles, and the destination ceiling
# (2026-09-06 gardening sitting — all three reproduced on the live store first)
# --------------------------------------------------------------------------- #

NESTED_TITLE_LINE = (
    "- [`/model` rewrites the settings pin to `[1m]`](model-pin.md) — the fat "
    "original summary that needs trimming\n")


def _strip_pointer_stats(text):
    """`text` with every JIT pointer's `· N notes · ~T tok ·` segment blanked, so
    a test can assert "nothing changed EXCEPT the refreshed pointer stats"."""
    return mg.POINTER_STATS_RE.sub("<stats>", text)


def _seed_sharded_store(tmp_path, name="sharded"):
    """A minimal ALREADY-SHARDED store: a core with one JIT pointer + one
    core-resident note, and a shard holding one shard-resident note."""
    work = tmp_path / name
    work.mkdir()
    pointer = mg._shard_pointer("watering", "Watering", 1, 40)
    _write(work / "MEMORY.md",
           "# Memory Index\n\n"
           + pointer + "\n"
           + "- [Core note](core-note.md) — lives in the core index\n")
    _write(work / "MEMORY.watering.md",
           "# Watering\n\n"
           "- [Hose leak](hose-leak.md) — lives in the SHARD index, fat summary here\n")
    for note in ("core-note.md", "hose-leak.md"):
        _write(work / note, "---\nname: %s\n---\n\nbody\n" % note[:-3])
    return work


def test_index_line_re_matches_a_nested_bracket_title():
    """A title carrying its own `[...]` run used to fail the match entirely —
    silently, and failing OPEN (rebuild_index then APPENDS a duplicate)."""
    m = mg.INDEX_LINE_RE.match(NESTED_TITLE_LINE)
    assert m is not None
    assert m.group(1) == "model-pin.md"


def test_index_line_re_keys_off_the_first_link_not_the_last():
    """A summary may itself carry a markdown link (the live NETWORK/OUTAGE
    line does). The nested-bracket fix must not become a greedy
    match-to-the-final-`](`, which would re-key the line to the wrong note."""
    ln = ("- [**NETWORK/OUTAGE**](network-outage.md) — read the syslog first. "
          "Studio-only dark → [studio-wifi-wedge](studio-wifi-wedge.md)\n")
    assert mg.INDEX_LINE_RE.match(ln).group(1) == "network-outage.md"


def test_rebuild_index_replaces_rather_than_duplicates_a_nested_bracket_line():
    core = "# Memory Index\n\n" + NESTED_TITLE_LINE
    rebuilt = mg.rebuild_index(core, {"model-pin.md": "- [pin](model-pin.md) — short"}, set())
    assert rebuilt.count("model-pin.md") == 1
    assert "the fat original summary" not in rebuilt
    mg._assert_no_duplicate_index_targets(rebuilt, "MEMORY.md")


def test_assert_no_duplicate_index_targets_refuses_a_doubled_listing():
    doubled = ("- [a](note.md) — one\n"
               "- [b](note.md) — two\n")
    with pytest.raises(ValueError) as ei:
        mg._assert_no_duplicate_index_targets(doubled, "MEMORY.md")
    assert "note.md" in str(ei.value)
    # A clean index passes.
    mg._assert_no_duplicate_index_targets("- [a](one.md) — x\n- [b](two.md) — y\n", "MEMORY.md")


def test_index_homes_resolves_core_and_shard_residents(tmp_path):
    work = _seed_sharded_store(tmp_path)
    homes = mg.index_homes(str(work))
    assert homes["core-note.md"] == "MEMORY.md"
    assert homes["hose-leak.md"] == "MEMORY.watering.md"
    # The pointer bullet is not a note and must never claim a home.
    assert "MEMORY.watering.md" not in homes


def test_scan_reports_shard_resident_summary_length_and_home(tmp_path):
    """index_summary_len was 0 for EVERY shard-resident note, so balloon_index
    could never fire outside the core — 92 of 173 notes on the live store."""
    work = _seed_sharded_store(tmp_path)
    res = mg.scan(str(work), date(2026, 9, 6))
    by = {e["file"]: e for e in res["files"]}
    assert by["hose-leak.md"]["index_summary_len"] > 0
    assert by["hose-leak.md"]["index_file"] == "MEMORY.watering.md"
    assert by["core-note.md"]["index_file"] == "MEMORY.md"


@pytest.mark.parametrize(
    ("note", "placements", "expected_homes", "clean"),
    [
        ("core-shared.md", ("MEMORY.md", "MEMORY.alpha.md"),
         ["MEMORY.md", "MEMORY.alpha.md"], False),
        ("shard-shared.md", ("MEMORY.alpha.md", "MEMORY.beta.md"),
         ["MEMORY.alpha.md", "MEMORY.beta.md"], False),
        ("single-home.md", ("MEMORY.beta.md",),
         ["MEMORY.beta.md"], True),
    ],
    ids=("core-and-shard", "two-shards", "single-home"),
)
def test_scan_reports_cross_index_integrity_deterministically(
        tmp_path, note, placements, expected_homes, clean):
    """Cross-index duplicates are invisible to each index's local assertion."""
    work = tmp_path / "memory"
    work.mkdir()
    pointers = (
        mg._shard_pointer("alpha", "Alpha", 1, 20),
        mg._shard_pointer("beta", "Beta", 1, 20),
    )
    indexes = {
        "MEMORY.md": "# Memory Index\n\n%s\n%s\n" % pointers,
        "MEMORY.alpha.md": "# Alpha\n\n",
        "MEMORY.beta.md": "# Beta\n\n",
    }
    for home in placements:
        indexes[home] += "- [Note](%s) — indexed in %s\n" % (note, home)
    for home, text in indexes.items():
        _write(work / home, text)
    _write(work / note, "---\nname: note\n---\n\nbody\n")

    first = mg.scan(str(work), date(2026, 9, 25))["integrity"]
    second = mg.scan(str(work), date(2026, 9, 25))["integrity"]
    expected_duplicates = ([] if clean else [{
        "file": note,
        "index_files": expected_homes,
    }])
    assert first == {
        "clean": clean,
        "cross_index_duplicates": expected_duplicates,
    }
    assert second == first


def test_apply_updates_a_shard_resident_line_in_its_own_shard(tmp_path):
    """The load-bearing regression. Before the fix this appended a SECOND
    bullet to MEMORY.md, left the shard's stale line in place, and reported
    "1 line(s) updated" — reproduced live on autonomy-contract.md."""
    work = _seed_sharded_store(tmp_path)
    core_before = (work / "MEMORY.md").read_text()
    plan = {"files": [{"file": "hose-leak.md", "verdict": "KEEP",
                       "index_line": "- [Hose leak](hose-leak.md) — trimmed"}]}
    res = mg.apply_plan(plan, str(work), date(2026, 9, 6))

    shard = (work / "MEMORY.watering.md").read_text()
    assert "— trimmed" in shard
    assert "fat summary here" not in shard
    core_after = (work / "MEMORY.md").read_text()
    # The load-bearing claim: no appended duplicate. The core keeps exactly the
    # bullets it had, and the note stays listed once, in its shard.
    assert core_after.count("(hose-leak.md)") == 0
    assert core_after.count("- [") == core_before.count("- [")
    # The ONE permitted difference: the shard's JIT pointer stats, refreshed to
    # match the shard it just rewrote (trimming shrank it, so the advertised
    # cost must come down with it — see refresh_shard_pointer).
    assert _strip_pointer_stats(core_after) == _strip_pointer_stats(core_before)
    assert core_after != core_before
    assert any("MEMORY.watering.md" in a for a in res["actions"])
    assert any("JIT pointer" in a for a in res["actions"])


def test_apply_still_appends_a_brand_new_entry_to_the_core(tmp_path):
    """The documented never-silently-dropped behaviour must survive the fix:
    a note listed in NO index still lands in the core."""
    work = _seed_sharded_store(tmp_path)
    _write(work / "orphan.md", "---\nname: orphan\n---\n\nbody\n")
    plan = {"files": [{"file": "orphan.md", "verdict": "KEEP",
                       "index_line": "- [Orphan](orphan.md) — new entry"}]}
    mg.apply_plan(plan, str(work), date(2026, 9, 6))
    core = (work / "MEMORY.md").read_text()
    assert core.count("orphan.md") == 1
    assert "MEMORY.watering.md" not in (work / "MEMORY.watering.md").read_text().replace(
        "MEMORY.watering.md", "")  # shard untouched by the core-bound append


def test_measure_text_reports_bytes_not_just_chars():
    m = mg.measure_text("— · em dash and middot\n")
    assert m["bytes"] > m["chars"]


def test_extend_ceiling_flags_over_and_reports_note_headroom():
    ok = mg._extend_ceiling({"bytes": 5000, "lines": 20, "chars": 5000, "est_tokens": 1250})
    assert ok["status"] == "ok" and ok["notes_headroom"] > 0
    over_bytes = mg._extend_ceiling(
        {"bytes": mg.FILE_BUDGET_MAX_BYTES + 1, "lines": 20, "chars": 1, "est_tokens": 1})
    assert over_bytes["status"] == "over"
    over_lines = mg._extend_ceiling(
        {"bytes": 100, "lines": mg.FILE_BUDGET_MAX_LINES + 1, "chars": 1, "est_tokens": 1})
    assert over_lines["status"] == "over"
    warn = mg._extend_ceiling(
        {"bytes": 100, "lines": mg.FILE_BUDGET_WARN_LINES, "chars": 1, "est_tokens": 1})
    assert warn["status"] == "warn"


def test_extend_refuses_when_a_destination_shard_would_truncate(tmp_path):
    """Curing MEMORY.md by pushing a shard past the harness ceiling is not a
    cure — it relocates the silent truncation into a JIT-loaded file."""
    work, sha = _seed_extend(tmp_path)
    # Fatten the destination shard so the single moved line tips it over.
    shard = work / "MEMORY.watering.md"
    shard.write_text(shard.read_text() + "x" * mg.FILE_BUDGET_MAX_BYTES + "\n",
                     encoding="utf-8")
    before = _dir_snapshot(str(work))
    with pytest.raises(ValueError) as ei:
        mg.execute_extend(str(work), GARDEN_TAXONOMY, TODAY, expect_sha256=sha)
    assert "REFUSING the extend" in str(ei.value)
    assert "MEMORY.watering.md" in str(ei.value)
    # Nothing written on a refusal.
    assert _dir_snapshot(str(work)) == before


def test_extend_dryrun_states_the_refusal_it_would_hit(tmp_path):
    """Report/apply parity: a dry run must not preview green a move --apply
    will reject."""
    work, _ = _seed_extend(tmp_path)
    shard = work / "MEMORY.watering.md"
    shard.write_text(shard.read_text() + "x" * mg.FILE_BUDGET_MAX_BYTES + "\n",
                     encoding="utf-8")
    prop = mg.propose_extend(str(work), GARDEN_TAXONOMY)
    watering = next(s for s in prop["shards"] if s["file"] == "MEMORY.watering.md")
    assert watering["ceiling"]["status"] == "over"
    assert "WILL REFUSE" in mg._ceiling_footnote(prop["shards"])


# --------------------------------------------------------------------------
# JIT pointer staleness after a shard rewrite (2026-09-06).
# `propose_extend` refreshes a pointer only for a shard it MOVES into, so an
# `apply_plan` that rewrote a shard's index lines left the core advertising the
# pre-trim cost. Reproduced live: the core claimed 6912 tok against a real 5578.
# --------------------------------------------------------------------------

def test_refresh_shard_pointer_updates_stats_only(tmp_path):
    """The stats segment is rewritten; every other byte of the line survives,
    including hand-added prose after the generated suffix."""
    pointer = mg._shard_pointer("fleet", "Fleet stuff", 41, 3487)
    hand_augmented = pointer + " · **standing note a human added**"
    core = "# Index\n\n%s\n- [Note](note.md) — a core note\n" % hand_augmented
    shard = "# Fleet\n\n- [A](a.md) — one\n- [B](b.md) — two\n"

    out = mg.refresh_shard_pointer(core, "MEMORY.fleet.md", shard)

    assert "· 2 notes · ~%d tok ·" % mg.estimate_tokens(shard) in out
    assert "3487" not in out
    assert "**standing note a human added**" in out
    assert "- [Note](note.md) — a core note" in out
    # Only the stats differ.
    assert _strip_pointer_stats(out) == _strip_pointer_stats(core)


def test_refresh_shard_pointer_is_a_noop_for_unknown_or_unparsable(tmp_path):
    """No pointer for that shard, or a pointer edited past recognition, is left
    alone rather than guessed at."""
    core = "# Index\n\n%s\n" % mg._shard_pointer("fleet", "Fleet", 1, 10)
    assert mg.refresh_shard_pointer(core, "MEMORY.infra.md", "x") == core
    mangled = "# Index\n\n- [MEMORY.fleet.md](MEMORY.fleet.md) — no stats here\n"
    assert mg.refresh_shard_pointer(mangled, "MEMORY.fleet.md", "x") == mangled
    assert mg.refresh_shard_pointer("", "MEMORY.fleet.md", "x") == ""


def test_apply_refreshes_the_pointer_of_every_shard_it_rewrites(tmp_path):
    """End-to-end: trimming a shard-resident line shrinks the shard, and the
    core's advertised cost for that shard comes down to match."""
    work = _seed_sharded_store(tmp_path)
    before = (work / "MEMORY.md").read_text()
    adv_before = int(re.search(r"~(\d+) tok", before).group(1))

    plan = {"files": [{"file": "hose-leak.md", "verdict": "KEEP",
                       "index_line": "- [Hose leak](hose-leak.md) — trimmed"}]}
    res = mg.apply_plan(plan, str(work), date(2026, 9, 6))

    after = (work / "MEMORY.md").read_text()
    adv_after = int(re.search(r"~(\d+) tok", after).group(1))
    real = mg.estimate_tokens((work / "MEMORY.watering.md").read_text())
    assert adv_after == real, "pointer must match the shard actually written"
    assert adv_after < adv_before, "a trim must lower the advertised cost"
    assert any("JIT pointer" in a for a in res["actions"])


def test_apply_leaves_pointers_alone_when_no_shard_changed(tmp_path):
    """A core-only change must not touch any pointer line."""
    work = _seed_sharded_store(tmp_path)
    before = (work / "MEMORY.md").read_text()
    core_note = [ln for ln in before.split("\n")
                 if ln.startswith("- [") and "MEMORY." not in ln][0]
    target = re.search(r"\(([^)]+\.md)\)", core_note).group(1)
    plan = {"files": [{"file": target, "verdict": "KEEP",
                       "index_line": "- [T](%s) — trimmed core line" % target}]}
    mg.apply_plan(plan, str(work), date(2026, 9, 6))

    after = (work / "MEMORY.md").read_text()
    assert "trimmed core line" in after
    assert _strip_pointer_stats(after) != after      # a pointer still exists
    assert (re.search(r"~(\d+) tok", after).group(1)
            == re.search(r"~(\d+) tok", before).group(1))


# --------------------------------------------------------------------------
# Report same-day clobber (2026-09-06): a second run overwrote the first with
# no backup, destroying the only record of 79 pre-trim index lines.
# --------------------------------------------------------------------------

def test_claim_dated_path_never_overwrites(tmp_path):
    d = str(tmp_path)
    first = mg._claim_dated_path(d, "2026-09-06", ".md")
    assert first.endswith("2026-09-06.md")
    open(first, "w").write("first")
    second = mg._claim_dated_path(d, "2026-09-06", ".md")
    assert second.endswith("2026-09-06-2.md")
    open(second, "w").write("second")
    assert mg._claim_dated_path(d, "2026-09-06", ".md").endswith("2026-09-06-3.md")
    # The originals survive untouched — the whole point.
    assert open(first).read() == "first"
    assert open(second).read() == "second"


# ------------------------------------------------------------- default --dir
def test_default_dir_is_home_project_memory(monkeypatch, tmp_path):
    """With no override the default `--dir` is the memory dir of the project
    rooted at the user's home: `~/.claude/projects/<home with / -> ->/memory`."""
    monkeypatch.delenv("MEMORY_GARDENER_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = os.path.join(
        str(tmp_path), ".claude", "projects",
        str(tmp_path).replace(os.sep, "-"), "memory")
    assert mg._default_memory_dir() == expected


def test_default_dir_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MEMORY_GARDENER_DIR", str(tmp_path / "elsewhere"))
    assert mg._default_memory_dir() == str(tmp_path / "elsewhere")


def test_default_dir_reaches_the_cli(monkeypatch, tmp_path, capsys):
    """`scan` without `--dir` reads the default dir (here: the env override)."""
    work = tmp_path / "memory"
    shutil.copytree(FIXTURE, work)
    monkeypatch.setenv("MEMORY_GARDENER_DIR", str(work))
    rc = mg.main(["scan", "--today", "2026-07-08"])
    assert rc == 0
    assert "hedge-trimmer-schedule" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# classify (report-only migration classification)
# --------------------------------------------------------------------------- #

CLASSIFY_STORE = os.path.join(HERE, "fixtures", "memory_classify")
CLASSIFY_RULES = os.path.join(HERE, "fixtures", "classify_rules")
CLASSIFY_LEDGER = os.path.join(HERE, "fixtures", "classify_ledger")


def _tree_hashes(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            out[os.path.relpath(p, root)] = mg.file_sha256(p)
    return out


def _classify(rules=(CLASSIFY_RULES,), ledger=(CLASSIFY_LEDGER,)):
    result = mg.classify_store(CLASSIFY_STORE, list(rules), list(ledger))
    return {e["note"]: e for e in result["entries"]}, result


def test_classify_assigns_each_class_from_a_fixture_note():
    by_note, _ = _classify()
    assert by_note["user-prefers-tea.md"]["class"] == "personal"
    assert by_note["terse-answers.md"]["class"] == "personal"
    assert by_note["review-before-merge.md"]["class"] == "harness-fact"
    assert by_note["shed-roof-choice.md"]["class"] == "decision"
    assert by_note["hose-reel-gotcha.md"]["class"] == "project-gotcha"
    assert by_note["old-fence-plan.md"]["class"] == "stale"
    assert by_note["missing-shed-config.md"]["class"] == "project-gotcha"


def test_classify_skips_index_and_shard_files():
    by_note, _ = _classify()
    assert "MEMORY.md" not in by_note
    assert "MEMORY.tools.md" not in by_note
    assert len(by_note) == 8


def test_classify_harness_fact_carries_rules_file_line():
    by_note, _ = _classify()
    assert by_note["review-before-merge.md"]["evidence"] == \
        "duplicates: classify_rules/house-rules.md:3"
    assert by_note["review-before-merge.md"]["low_confidence"] is None


def test_classify_per_class_evidence():
    by_note, _ = _classify()
    assert by_note["shed-roof-choice.md"]["evidence"] == \
        "ledger: classify_ledger/items.md:3"
    assert by_note["hose-reel-gotcha.md"]["evidence"] == "repo: garden-app"
    assert by_note["old-fence-plan.md"]["evidence"] == "reason: marked RESOLVED"
    gone = by_note["missing-shed-config.md"]
    assert gone["evidence"] == "repo: fixtures"
    assert "/etc/garden-notes/shed.conf" in gone["low_confidence"]


def test_classify_without_rules_root_never_yields_harness_fact():
    by_note, result = _classify(rules=(), ledger=())
    e = by_note["seat-worktree-habit.md"]
    assert e["class"] == "personal"
    assert e["evidence"] == "type=feedback; duplicates: not searched"
    assert "not searched" in e["low_confidence"]
    assert result["counts"]["harness-fact"] == 0
    assert by_note["shed-roof-choice.md"]["evidence"] == "ledger: none yet"


def test_classify_every_harness_fact_line_carries_file_line():
    _, result = _classify()
    facts = [e for e in result["entries"] if e["class"] == "harness-fact"]
    assert facts
    assert all(re.search(r"^duplicates: \S+\.md:\d+$", e["evidence"])
               for e in facts)


def test_classify_with_rules_root_but_no_duplicate_is_not_harness_fact():
    by_note, _ = _classify()
    e = by_note["seat-worktree-habit.md"]
    assert e["class"] == "personal"
    assert e["low_confidence"]


def test_classify_report_count_line_matches_entries():
    _, result = _classify()
    text = mg.render_classify_report(result)
    assert text.splitlines()[1] == (
        "counts: personal=3 harness-fact=1 decision=1 project-gotcha=2 "
        "stale=1 total=8")
    body = [ln for ln in text.splitlines() if " | " in ln]
    assert len(body) == 8
    assert body == sorted(body, key=lambda ln: ln.split(" | ")[1])


def test_classify_cli_rerun_is_byte_identical(tmp_path):
    outs = []
    for i in range(2):
        out = tmp_path / ("r%d.md" % i)
        rc = mg.main(["classify", "--dir", CLASSIFY_STORE,
                      "--rules-root", CLASSIFY_RULES, "--out", str(out)])
        assert rc == 0
        outs.append(out.read_bytes())
    assert outs[0] == outs[1]


def test_classify_refuses_out_inside_the_store(tmp_path, capsys):
    store = tmp_path / "memory"
    shutil.copytree(CLASSIFY_STORE, store)
    before = _tree_hashes(store)
    for target in (store / "report.md", store / "sub" / "report.md"):
        rc = mg.main(["classify", "--dir", str(store), "--out", str(target)])
        assert rc == 2
        assert not target.exists()
    assert "inside the memory dir" in capsys.readouterr().err
    assert _tree_hashes(store) == before


def test_classify_changes_no_file_in_the_store(tmp_path):
    store = tmp_path / "memory"
    shutil.copytree(CLASSIFY_STORE, store)
    before = _tree_hashes(store)
    rc = mg.main(["classify", "--dir", str(store),
                  "--rules-root", CLASSIFY_RULES,
                  "--out", str(tmp_path / "report.md")])
    assert rc == 0
    assert _tree_hashes(store) == before


# Built from parts so the installed-files reference check does not read these
# invented example paths as references into a user's home or temp dir.
HOME_TOKEN = "~"


def _store_of(tmp_path, notes):
    store = tmp_path / "proj" / "memory"
    store.mkdir(parents=True)
    for name, text in notes.items():
        (store / name).write_text(text)
    return store


def _note(desc, body, ntype="reference", extra=""):
    return ('---\nname: n\ndescription: "%s"\n%smetadata:\n  type: %s\n---\n\n%s\n'
            % (desc, extra, ntype, body))


def _classify_one(tmp_path, text, rules=()):
    store = _store_of(tmp_path, {"n.md": text})
    return mg.classify_store(str(store), list(rules), [])["entries"][0]


@pytest.mark.parametrize("extra,desc,body", [
    ("status: resolved\n", "Plain", "Plain."),
    ("status: superseded\n", "Plain", "Plain."),
    ("status: dead\n", "Plain", "Plain."),
    ("superseded_by: other.md\n", "Plain", "Plain."),
    ("", "Resolved upstream: the cache bug", "Plain."),
    ("", "Replaced by the v2 plan", "Plain."),
    ("", "Plain", "# OBSOLETE: old plan\n\nPlain."),
    ("", "Plain", "Status: replaced by notes-v2"),
    ("", "Plain", "One.\n\nTwo.\n\nThree.\n\nStatus: obsolete"),
    ("", "Plain", "**RESOLVED 2026-01-01:** it opens the body."),
], ids=["status-resolved", "status-superseded", "status-dead", "superseded-by",
        "desc-leading", "desc-replaced-by", "title-obsolete",
        "status-line-replaced-by", "status-line-deep", "opening-line"])
def test_classify_stale_needs_an_explicit_marker(tmp_path, extra, desc, body):
    e = _classify_one(tmp_path, _note(desc, body, extra=extra))
    assert e["class"] == "stale"


@pytest.mark.parametrize("desc,body", [
    ("How to handle resolved merge conflicts", "Plain."),
    ("Plain", "The bug was resolved by restarting the service."),
    ("Old plan, now superseded by the v2 plan", "Plain."),
    ("Cache rule RESOLVED in v3", "Plain."),
    ("Plain", "# Title\n\n## Defects: 3 of 4 now RESOLVED"),
    ("Plain", "One.\n\nTwo.\n\nThree.\n\n**RESOLVED 2026-01-01:** a saga line deep in a note."),
    ("Plain", "Kept in `/etc/garden-notes/shed.conf`, which is gone."),
], ids=["desc-mid-sentence", "body-mid-sentence", "desc-superseded-by-midway",
        "desc-caps-midway", "title-resolved-midway", "body-line-deep",
        "missing-path-alone"])
def test_classify_unmarked_note_is_not_stale(tmp_path, desc, body):
    assert _classify_one(tmp_path, _note(desc, body))["class"] != "stale"


def test_classify_class_ignores_whether_a_quoted_path_exists(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    store = _store_of(tmp_path, {
        "cfg.md": _note("Shed thermostat settings", "Kept in `%s/garden/shed.conf`." % HOME_TOKEN)})
    before = mg.classify_store(str(store), [], [])["entries"][0]
    (home / "garden").mkdir()
    (home / "garden" / "shed.conf").write_text("x")
    after = mg.classify_store(str(store), [], [])["entries"][0]
    assert before["class"] == after["class"] == "project-gotcha"
    assert "no longer exists" in before["low_confidence"]
    assert "no longer exists" not in after["low_confidence"]


@pytest.mark.parametrize("token", [
    "/private/tmp/<x>", HOME_TOKEN + "/work/*/notes.md",
    HOME_TOKEN + "/code/{a,b}/x.md", HOME_TOKEN + "/work/.../file",
    "https://example.org/a/b", "/api/v1/items", "/" + "tmp/scratch/out.txt",
    HOME_TOKEN + "/repos/wt/EK1/file.md",
    HOME_TOKEN + "/proj/.claude/worktrees/abc/file.md",
])
def test_classify_ignores_placeholder_route_and_scratch_paths(tmp_path, token):
    e = _classify_one(tmp_path, _note("Plain", "See `%s` for it." % token))
    assert "no longer exists" not in (e["low_confidence"] or "")


def _rule_lines(tmp_path, *lines):
    d = tmp_path / "rules"
    d.mkdir(exist_ok=True)
    (d / "r.md").write_text("\n".join(lines) + "\n")
    return mg._collect_md_lines([str(d)])


DUP_DESC = "alpha bravo charlie delta echoes foxtrot"


def test_find_duplicate_accepts_a_close_restatement(tmp_path):
    md = _rule_lines(tmp_path, "unrelated", "alpha bravo charlie delta echoes extra")
    assert mg._find_duplicate(DUP_DESC, md) == "rules/r.md:2"


def test_find_duplicate_needs_five_shared_words(tmp_path):
    md = _rule_lines(tmp_path, "alpha bravo charlie delta")
    assert mg._find_duplicate(DUP_DESC, md) is None


def test_find_duplicate_rejects_a_long_line_that_merely_contains_the_words(tmp_path):
    extra = " ".join("pad%d" % i for i in range(7))
    md = _rule_lines(tmp_path, "alpha bravo charlie delta echoes " + extra)
    assert mg._find_duplicate(DUP_DESC, md) is None
    extra = " ".join("pad%d" % i for i in range(6))
    md = _rule_lines(tmp_path, "alpha bravo charlie delta echoes " + extra)
    assert mg._find_duplicate(DUP_DESC, md) == "rules/r.md:1"


def test_find_duplicate_ignores_generic_harness_words(tmp_path):
    generic = "harness session standard worktree director builder"
    md = _rule_lines(tmp_path, generic + " alpha bravo charlie delta")
    assert mg._find_duplicate(generic + " alpha bravo charlie delta", md) is None


@pytest.mark.parametrize("text,expected", [
    ("Seen under work/... in the notes.", "slug"),
    ("Under `" + HOME_TOKEN + "/work/...` and then code/real-repo.", "real-repo"),
    ("Lives in code/garden-app.", "garden-app"),
])
def test_classify_repo_capture_skips_punctuation_only_names(text, expected):
    fm, _ = mg.split_frontmatter(_note("Plain", text))
    repo, _ = mg._note_repo(fm, text, "slug")
    assert repo == expected


def test_classify_out_refuses_a_symlink_through_a_linked_dir_inside_the_store(tmp_path, capsys):
    store = tmp_path / "memory"
    shutil.copytree(CLASSIFY_STORE, store)
    outside = tmp_path / "outside"
    outside.mkdir()
    (store / "linkdir").symlink_to(outside)
    rc = mg.main(["classify", "--dir", str(store),
                  "--out", str(store / "linkdir" / "r.md")])
    assert rc == 2
    assert not (outside / "r.md").exists()
    assert "inside the memory dir" in capsys.readouterr().err


def test_classify_out_refuses_a_path_that_resolves_into_the_store(tmp_path, capsys):
    store = tmp_path / "memory"
    shutil.copytree(CLASSIFY_STORE, store)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "alias").symlink_to(store)
    rc = mg.main(["classify", "--dir", str(store),
                  "--out", str(outside / "alias" / "r.md")])
    assert rc == 2
    assert not (store / "r.md").exists()
    assert "inside the memory dir" in capsys.readouterr().err


def test_classify_out_refuses_a_symlink_target(tmp_path, capsys):
    store = tmp_path / "memory"
    shutil.copytree(CLASSIFY_STORE, store)
    target = tmp_path / "keep.md"
    target.write_text("keep")
    for link in (tmp_path / "link.md", store / "inlink.md"):
        link.symlink_to(target)
        rc = mg.main(["classify", "--dir", str(store), "--out", str(link)])
        assert rc == 2
        assert target.read_text() == "keep"
    assert "symlink" in capsys.readouterr().err


def test_classify_out_hard_link_to_a_store_note_leaves_the_store_unchanged(tmp_path):
    # Outside the store, --out may be a hard link to a note. The report is
    # renamed onto the path, so the linked inode is never truncated. The run
    # succeeds (exit 0) and the link becomes an ordinary report file.
    store = tmp_path / "memory"
    shutil.copytree(CLASSIFY_STORE, store)
    note = sorted(p for p in store.iterdir()
                  if p.suffix == ".md" and p.name != "MEMORY.md")[0]
    link = tmp_path / "outside-link.md"
    os.link(note, link)
    before = _tree_hashes(store)
    rc = mg.main(["classify", "--dir", str(store), "--out", str(link)])
    assert rc == 0
    assert _tree_hashes(store) == before
    assert link.read_text().startswith("# Memory classification")
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "memory", "outside-link.md"]


def test_classify_report_writer_does_not_follow_a_symlink(tmp_path):
    target = tmp_path / "keep.md"
    target.write_text("keep")
    link = tmp_path / "link.md"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        mg.write_classify_report(str(link), "report")
    assert target.read_text() == "keep"


def test_classify_report_new_file_is_private_under_restrictive_umask(tmp_path):
    out = tmp_path / "report.md"
    previous = os.umask(0o077)
    try:
        mg.write_classify_report(str(out), "report\n")
        mode = os.stat(out).st_mode & 0o777
    finally:
        os.umask(previous)
    assert oct(mode) == oct(0o600)
    assert out.read_text() == "report\n"


def test_classify_report_keeps_existing_private_mode(tmp_path):
    out = tmp_path / "report.md"
    out.write_text("old\n")
    os.chmod(out, 0o600)
    previous = os.umask(0o077)
    try:
        mg.write_classify_report(str(out), "report\n")
        mode = os.stat(out).st_mode & 0o777
    finally:
        os.umask(previous)
    assert oct(mode) == oct(0o600)
    assert out.read_text() == "report\n"


def test_classify_report_keeps_existing_group_readable_mode(tmp_path):
    out = tmp_path / "report.md"
    out.write_text("old\n")
    os.chmod(out, 0o644)
    previous = os.umask(0o077)
    try:
        mg.write_classify_report(str(out), "report\n")
        mode = os.stat(out).st_mode & 0o777
    finally:
        os.umask(previous)
    assert oct(mode) == oct(0o644)
    assert out.read_text() == "report\n"


def test_classify_report_refuses_a_symlink_target_and_writes_nothing(tmp_path):
    target = tmp_path / "keep.md"
    target.write_text("keep")
    link = tmp_path / "link.md"
    link.symlink_to(target)

    with pytest.raises(ValueError):
        mg.write_classify_report(str(link), "report\n")
    assert target.read_text() == "keep"
    assert stat.S_ISLNK(os.lstat(str(link)).st_mode)
    assert not list(tmp_path.glob(".classify-*"))


def test_classify_report_refuses_symlink_swapped_in_before_mode_stat(
        tmp_path, monkeypatch):
    """The sole target stat catches a symlink planted at its write boundary."""
    out = tmp_path / "report.md"
    keep = tmp_path / "keep.md"
    keep.write_text("keep")
    real_lstat = os.lstat
    target_lstats = []

    def lstat_and_plant(path):
        if os.path.abspath(path) == os.path.abspath(str(out)):
            target_lstats.append(path)
            if list(tmp_path.glob(".classify-*.tmp")):
                out.symlink_to(keep)
        return real_lstat(path)

    monkeypatch.setattr(mg.os, "lstat", lstat_and_plant)
    with pytest.raises(ValueError):
        mg.write_classify_report(str(out), "report\n")
    assert target_lstats == [str(out)]
    assert keep.read_text() == "keep"
    assert stat.S_ISLNK(real_lstat(str(out)).st_mode)
    assert not list(tmp_path.glob(".classify-*"))


def test_classify_report_refuses_target_stat_permission_error(
        tmp_path, monkeypatch):
    out = tmp_path / "report.md"
    real_lstat = os.lstat
    target_lstats = []

    def denied_lstat(path):
        if os.path.abspath(path) == os.path.abspath(str(out)):
            target_lstats.append(path)
            raise PermissionError("stat denied")
        return real_lstat(path)

    monkeypatch.setattr(mg.os, "lstat", denied_lstat)
    with pytest.raises(ValueError, match="cannot write --out"):
        mg.write_classify_report(str(out), "report\n")
    assert target_lstats == [str(out)]
    assert not out.exists()
    assert not list(tmp_path.glob(".classify-*"))


def test_classify_report_strips_setuid_from_existing_mode(tmp_path, monkeypatch):
    """An existing 04644 target must be applied as 0644 — never setuid.
    macOS chmod(2) drops setuid on a non-executable file, so the 04644 is
    injected at the lstat boundary and the chmod argument is the observable."""
    out = tmp_path / "report.md"
    out.write_text("old\n")
    os.chmod(out, 0o644)
    real_lstat = os.lstat
    seen = []

    def lstat_with_setuid(path):
        st = real_lstat(path)
        if os.path.abspath(path) == os.path.abspath(str(out)):
            fields = list(st)
            fields[stat.ST_MODE] = (st.st_mode & ~0o7777) | 0o4644
            return os.stat_result(fields)
        return st

    def chmod_spy(path, mode, *args, **kwargs):
        seen.append(mode)
        return real_chmod(path, mode, *args, **kwargs)

    real_chmod = os.chmod
    monkeypatch.setattr(os, "lstat", lstat_with_setuid)
    monkeypatch.setattr(os, "chmod", chmod_spy)
    mg.write_classify_report(str(out), "report\n")
    assert seen == [0o644]
    assert oct(stat.S_IMODE(real_lstat(str(out)).st_mode)) == oct(0o644)
    assert out.read_text() == "report\n"


def test_classify_refuses_a_missing_or_non_directory_root(tmp_path, capsys):
    missing = str(tmp_path / "nope")
    a_file = tmp_path / "file.md"
    a_file.write_text("x")
    out = tmp_path / "report.md"
    for argv, flag in (
            (["--dir", missing], "memory dir"),
            (["--dir", CLASSIFY_STORE, "--rules-root", missing], "--rules-root"),
            (["--dir", CLASSIFY_STORE, "--rules-root", str(a_file)], "--rules-root"),
            (["--dir", CLASSIFY_STORE, "--ledger-root", missing], "--ledger-root")):
        rc = mg.main(["classify"] + argv + ["--out", str(out)])
        err = capsys.readouterr().err
        assert rc == 2
        assert flag in err
        assert (missing in err) or (str(a_file) in err)
        assert not out.exists()


def test_classify_names_a_missing_default_dir_from_the_environment(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MEMORY_GARDENER_DIR", str(tmp_path / "gone"))
    assert mg.main(["classify"]) == 2
    err = capsys.readouterr().err
    assert "MEMORY_GARDENER_DIR" in err
    assert "gone" in err
