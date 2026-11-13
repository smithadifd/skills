"""Store selection follows the rule every skill shares (``skills/_lib/ledger_cli.py``).

``TICKET_LEDGER_CLI`` comes first; then the topic skill's ``ledger_cli`` key, the explicit
setting every other skill reads; then a compatible ``ledger`` on
``PATH`` with ``LEDGER_DB``. ``none`` in either setting keeps the markdown store, and the reason
on the open card starts with the rule that chose.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import ticket
from ledger_support import FakeLedger, machine
from ticket_support import KEY, TITLE, Source, refusal

SKILL_DIR = Path(__file__).resolve().parent.parent


def topic_config(value):
    """Write the topic skill's override file under this test's HOME."""

    path = Path(os.environ["HOME"]) / ".claude" / "skill-config" / "topic.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ledger_cli": value}), encoding="utf-8")


@pytest.mark.parametrize("value", ["none", "NONE", " None "])
def test_none_in_the_topic_config_keeps_the_markdown_store_though_a_ledger_is_on_path(repo, tmp_path, monkeypatch, value):
    fake = FakeLedger(tmp_path / "bin")
    machine(monkeypatch, tmp_path, fake)
    topic_config(value)
    chosen = ticket.choose_store(repo, KEY, TITLE)
    assert isinstance(chosen, ticket.MarkdownStore)
    assert chosen.selection == "markdown (off by setting: ledger_cli in the topic config is none)"
    assert fake.calls() == []


def test_the_topic_config_naming_a_cli_selects_it_and_probes_it(repo, tmp_path, monkeypatch):
    machine(monkeypatch, tmp_path)  # no ledger on PATH
    named = FakeLedger(tmp_path / "named")
    topic_config(str(named.path))
    chosen = ticket.choose_store(repo, KEY, TITLE)
    assert isinstance(chosen, ticket.LedgerStore)
    assert chosen.cli == str(named.path)
    assert chosen.selection == "ledger (explicit setting: ledger_cli in the topic config names it)"
    assert [call.argv for call in named.calls()] == [["append-many", "--help"]]
    session = ticket.TicketPass.open(KEY, Source(), chosen)
    assert session.reconciliation[0] == f"store: {chosen.selection}"


def test_an_incompatible_cli_named_in_the_topic_config_is_refused_not_skipped(repo, tmp_path, monkeypatch):
    good = FakeLedger(tmp_path / "bin")
    env = machine(monkeypatch, tmp_path, good)
    bad = FakeLedger(tmp_path / "bad")
    bad.help("no --expect-revision")
    topic_config(str(bad.path))
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ticket.choose_store(repo, KEY, TITLE)
    assert refusal(excinfo) == "ledger_incompatible"
    result = subprocess.run(
        [sys.executable, str(SKILL_DIR / "ticket.py"), "release", KEY, "0123456789abcdef", "--repo", str(repo)],
        capture_output=True, text=True, env=env, check=False,
    )
    assert result.returncode == 2
    assert len(result.stderr.strip().splitlines()) == 1 and "lacks append-many" in result.stderr
    assert good.calls() == [] and not (repo / ".ticket").exists()


def test_the_ticket_variable_comes_before_the_topic_config(repo, tmp_path, monkeypatch):
    fake = FakeLedger(tmp_path / "bin")
    machine(monkeypatch, tmp_path)
    topic_config("none")
    monkeypatch.setenv("TICKET_LEDGER_CLI", str(fake.path))
    chosen = ticket.choose_store(repo, KEY, TITLE)
    assert isinstance(chosen, ticket.LedgerStore)
    assert chosen.selection == "ledger (explicit setting: TICKET_LEDGER_CLI names it)"
    topic_config(str(fake.path))
    monkeypatch.setenv("TICKET_LEDGER_CLI", "none")
    chosen = ticket.choose_store(repo, KEY, TITLE)
    assert isinstance(chosen, ticket.MarkdownStore)
    assert chosen.selection == "markdown (off by setting: TICKET_LEDGER_CLI=none)"
    monkeypatch.setenv("TICKET_LEDGER_CLI", "   ")  # blank is unset: the topic config decides
    assert ticket.choose_store(repo, KEY, TITLE).selection == (
        "ledger (explicit setting: ledger_cli in the topic config names it)"
    )


def test_a_ticket_installed_without_the_shared_rule_says_how_to_fix_it(tmp_path):
    lone = tmp_path / "skills" / "ticket"
    lone.mkdir(parents=True)
    (lone / "ticket.py").write_bytes((SKILL_DIR / "ticket.py").read_bytes())
    result = subprocess.run(
        [sys.executable, str(lone / "ticket.py"), "find-cards", str(tmp_path)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "is missing" in result.stderr and "reinstall the skills" in result.stderr


def test_the_choice_is_the_shared_rules_decision(repo, tmp_path, monkeypatch):
    fake = FakeLedger(tmp_path / "bin")
    machine(monkeypatch, tmp_path, fake)
    decision = ticket._LEDGER_RULE.decide(selector_env=ticket.LEDGER_SELECTOR, quiet=True)
    chosen = ticket.select_store()
    assert (chosen.cli, chosen.reason) == (decision.cli, decision.reason)
    assert chosen.reason.startswith(f"{ticket._LEDGER_RULE.RULE_PATH}: ")
