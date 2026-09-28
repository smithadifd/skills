"""Unit tests for jev_client.py — trim, mapping, fail-closed HTTP/resolve.

No network, no 1Password: resolver and urlopen are stubbed. No machine-local
paths.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))
import jev_client  # noqa: E402

FAKE_SECRET = "sk-test-FAKESECRET_c4d5e6f7g8h9i0j1k2l3"


def _env():
    return {"OP_CONNECT_HOST": "http://connect.example", "OP_CONNECT_TOKEN": "connect-tok"}


def _resolve(_ref, _host, _token):
    return "test-api-key-not-real"


class _Resp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, body=b""):
    return urllib.error.HTTPError(
        jev_client.ENDPOINT,
        code,
        "error",
        None,
        io.BytesIO(body),
    )


def test_trim_drops_fenced_secret_keeps_prose_and_bullets():
    raw = (
        "## FINDINGS\n"
        "- auth cookie is compared in constant time\n"
        "\n"
        "```python\n"
        f'API_KEY = "{FAKE_SECRET}"\n'
        "```\n"
        "\n"
        "## VERDICT\n"
        "BLOCKING — the quoted file must not reach the fallback.\n"
    )
    trimmed = jev_client.trim_payload(raw)
    assert FAKE_SECRET not in trimmed
    assert "auth cookie is compared in constant time" in trimmed
    assert "VERDICT" in trimmed
    assert "FINDINGS" in trimmed
    assert "```" not in trimmed


def test_trim_drops_indented_four_space_block():
    raw = (
        "## FINDINGS\n"
        "- keep this bullet\n"
        "\n"
        f"    {FAKE_SECRET}\n"
        "    still-indented\n"
        "\n"
        "## VERDICT\n"
        "LGTM — prose stays\n"
    )
    trimmed = jev_client.trim_payload(raw)
    assert FAKE_SECRET not in trimmed
    assert "still-indented" not in trimmed
    assert "keep this bullet" in trimmed
    assert "prose stays" in trimmed


def test_trim_drops_tilde_fence():
    raw = (
        "## FINDINGS\n"
        "- a finding\n"
        "~~~\n"
        f"{FAKE_SECRET}\n"
        "~~~\n"
        "## VERDICT\n"
        "flagged — x\n"
    )
    trimmed = jev_client.trim_payload(raw)
    assert FAKE_SECRET not in trimmed
    assert "a finding" in trimmed


def test_trim_four_backtick_fence_ignores_shorter_inner_fence():
    raw = (
        "## FINDINGS\n"
        "- prose\n"
        "````\n"
        "secret = SOURCE_SECRET_987654321\n"
        "```\n"
        "SOURCE_SECRET_TRAILING_111\n"
        "````\n"
        "\n"
        "## VERDICT\n"
        "LGTM\n"
    )
    trimmed = jev_client.trim_payload(raw)
    assert "SOURCE_SECRET_987654321" not in trimmed
    assert "SOURCE_SECRET_TRAILING_111" not in trimmed
    assert "## VERDICT\nLGTM" in trimmed


def test_trim_four_tilde_fence_ignores_shorter_inner_fence():
    raw = (
        "## FINDINGS\n"
        "- prose\n"
        "~~~~\n"
        "secret = SOURCE_SECRET_987654321\n"
        "~~~\n"
        "SOURCE_SECRET_TRAILING_111\n"
        "~~~~\n"
        "\n"
        "## VERDICT\n"
        "LGTM\n"
    )
    trimmed = jev_client.trim_payload(raw)
    assert "SOURCE_SECRET_987654321" not in trimmed
    assert "SOURCE_SECRET_TRAILING_111" not in trimmed
    assert "## VERDICT\nLGTM" in trimmed


def test_trim_drops_indented_bullet_shaped_secret():
    raw = (
        "## FINDINGS\n"
        "- prose\n"
        "    - password: SOURCE_SECRET_987654321\n"
        "\n"
        "## VERDICT\n"
        "LGTM\n"
    )
    trimmed = jev_client.trim_payload(raw)
    assert "SOURCE_SECRET_987654321" not in trimmed
    assert "password" not in trimmed
    assert "## VERDICT\nLGTM" in trimmed


def test_trim_drops_tab_indented_secret():
    raw = (
        "## FINDINGS\n"
        "- prose\n"
        "\t- password: SOURCE_SECRET_987654321\n"
        "\n"
        "## VERDICT\n"
        "LGTM\n"
    )
    trimmed = jev_client.trim_payload(raw)
    assert "SOURCE_SECRET_987654321" not in trimmed
    assert "password" not in trimmed
    assert "## VERDICT\nLGTM" in trimmed


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [
        (float("nan"), "jev: confidence nan is not finite"),
        (float("inf"), "jev: confidence inf is not finite"),
        (float("-inf"), "jev: confidence -inf is not finite"),
    ],
)
def test_decoded_non_finite_confidence_raises(monkeypatch, confidence, expected):
    client = jev_client.JevClient(resolve_ref=_resolve, environ=_env())
    monkeypatch.setattr(
        client,
        "_post",
        lambda _body, attempt: {
            "answers": {"verdict": {"choice": "APPROVE", "confidence": confidence}}
        },
    )
    with pytest.raises(jev_client.JevClosed) as exc:
        client.decide("x")
    assert str(exc.value) == expected


def test_approve_at_high_confidence_maps_to_approved(monkeypatch):
    payload = {"answers": {"verdict": {"choice": "APPROVE", "confidence": 0.95}}}
    monkeypatch.setattr(jev_client, "urlopen", lambda *a, **k: _Resp(payload))
    monkeypatch.setattr(jev_client, "sleep", lambda _s: None)
    client = jev_client.JevClient(resolve_ref=_resolve, environ=_env())
    verdict, confidence = client.decide("## FINDINGS\n- none\n## VERDICT\nLGTM\n")
    assert verdict == jev_client.APPROVED
    assert confidence == 0.95


def test_request_changes_maps_to_changes_requested(monkeypatch):
    payload = {"answers": {"verdict": {"choice": "REQUEST_CHANGES", "confidence": 0.80}}}
    monkeypatch.setattr(jev_client, "urlopen", lambda *a, **k: _Resp(payload))
    client = jev_client.JevClient(resolve_ref=_resolve, environ=_env())
    verdict, confidence = client.decide("x")
    assert verdict == jev_client.CHANGES_REQUESTED
    assert confidence == 0.80


def test_confidence_below_floor_raises(monkeypatch):
    payload = {"answers": {"verdict": {"choice": "APPROVE", "confidence": 0.4}}}
    monkeypatch.setattr(jev_client, "urlopen", lambda *a, **k: _Resp(payload))
    client = jev_client.JevClient(resolve_ref=_resolve, environ=_env())
    with pytest.raises(jev_client.JevClosed, match="below floor"):
        client.decide("x")


def test_unknown_choice_raises(monkeypatch):
    payload = {"answers": {"verdict": {"choice": "MAYBE", "confidence": 0.99}}}
    monkeypatch.setattr(jev_client, "urlopen", lambda *a, **k: _Resp(payload))
    client = jev_client.JevClient(resolve_ref=_resolve, environ=_env())
    with pytest.raises(jev_client.JevClosed, match="MAYBE"):
        client.decide("x")


def test_http_500_names_status_scrubs_key(monkeypatch):
    def boom(*a, **k):
        raise _http_error(500, b"denied test-api-key-not-real")

    monkeypatch.setattr(jev_client, "urlopen", boom)
    client = jev_client.JevClient(resolve_ref=_resolve, environ=_env())
    with pytest.raises(jev_client.JevClosed) as exc:
        client.decide("x")
    assert "500" in str(exc.value)
    assert "test-api-key-not-real" not in str(exc.value)


def test_resolve_error_fail_closed():
    class ResolveError(Exception):
        pass

    def boom(ref, host, token):
        raise ResolveError("item not found: 'typesafe-jev'")

    with pytest.raises(jev_client.JevClosed, match="item not found"):
        jev_client.JevClient(resolve_ref=boom, environ=_env())


def test_explicit_connect_values_win_without_keychain_lookup(monkeypatch):
    observed = {}

    def resolver(ref, host, token):
        observed.update(ref=ref, host=host, token=token)
        return "test-api-key-not-real"

    monkeypatch.setattr(
        jev_client,
        "_default_connect_token",
        lambda: pytest.fail("explicit OP_CONNECT_TOKEN must bypass Keychain"),
    )

    jev_client.JevClient(resolve_ref=resolver, environ=_env())

    assert observed == {
        "ref": jev_client.SECRET_REF,
        "host": "http://connect.example",
        "token": "connect-tok",
    }


def test_connect_host_defaults_to_working_consumer_address():
    observed = {}

    def resolver(_ref, host, _token):
        observed["host"] = host
        return "test-api-key-not-real"

    jev_client.JevClient(
        resolve_ref=resolver,
        environ={"OP_CONNECT_TOKEN": "connect-tok"},
    )

    assert observed["host"] == "http://192.168.50.88:8090"


def test_connect_host_uses_configurable_default_when_environ_has_none():
    observed = {}

    def resolver(_ref, host, token):
        observed.update(host=host, token=token)
        return "test-api-key-not-real"

    jev_client.JevClient(
        resolve_ref=resolver,
        environ={"OP_CONNECT_TOKEN": "connect-tok"},
        default_connect_host="http://configured-connect.example",
    )

    assert observed == {
        "host": "http://configured-connect.example",
        "token": "connect-tok",
    }


def test_connect_token_uses_op_resolve_keychain_lookup_when_environ_has_none(
    monkeypatch,
):
    class FakeOpResolve:
        @staticmethod
        def _keychain_connect_token():
            return "resolved", "keychain-connect-token"

    observed = {}

    def resolver(_ref, host, token):
        observed.update(host=host, token=token)
        return "test-api-key-not-real"

    monkeypatch.setattr(jev_client, "_load_op_resolve", lambda: FakeOpResolve)

    jev_client.JevClient(
        resolve_ref=resolver,
        environ={"OP_CONNECT_HOST": "http://connect.example"},
    )

    assert observed == {
        "host": "http://connect.example",
        "token": "keychain-connect-token",
    }


def test_missing_connect_host_fail_closed():
    with pytest.raises(
        jev_client.JevClosed,
        match="OP_CONNECT_HOST unresolvable.*environ and configured default",
    ):
        jev_client.JevClient(
            resolve_ref=_resolve,
            environ={"OP_CONNECT_TOKEN": "t"},
            default_connect_host="",
        )


def test_missing_connect_token_fails_closed_with_keychain_location(monkeypatch):
    class FakeOpResolve:
        @staticmethod
        def _keychain_connect_token():
            return "absent", None

    monkeypatch.setattr(jev_client, "_load_op_resolve", lambda: FakeOpResolve)

    with pytest.raises(jev_client.JevClosed) as exc:
        jev_client.JevClient(
            resolve_ref=_resolve,
            environ={"OP_CONNECT_HOST": "http://connect.example"},
        )

    reason = str(exc.value)
    assert "OP_CONNECT_TOKEN unresolvable" in reason
    assert "macOS Keychain service op-connect-token" in reason
    assert "status absent" in reason


def test_connect_token_preserves_op_resolve_loader_failure(monkeypatch):
    def unavailable():
        raise jev_client.JevClosed("jev: could not load op-resolve")

    monkeypatch.setattr(jev_client, "_load_op_resolve", unavailable)

    with pytest.raises(
        jev_client.JevClosed, match=r"^jev: could not load op-resolve$"
    ):
        jev_client.JevClient(
            resolve_ref=_resolve,
            environ={"OP_CONNECT_HOST": "http://connect.example"},
        )


def test_retry_429_then_succeeds(monkeypatch):
    calls = {"n": 0}
    slept = []

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(429, b"slow down")
        return _Resp({"answers": {"verdict": {"choice": "APPROVE", "confidence": 0.91}}})

    monkeypatch.setattr(jev_client, "urlopen", flaky)
    monkeypatch.setattr(jev_client, "sleep", lambda s: slept.append(s))
    client = jev_client.JevClient(resolve_ref=_resolve, environ=_env())
    verdict, confidence = client.decide("x")
    assert verdict == "approved"
    assert confidence == 0.91
    assert calls["n"] == 3
    assert slept == [1, 2]
