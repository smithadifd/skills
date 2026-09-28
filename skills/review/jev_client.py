#!/usr/bin/env python3
"""Stdlib client for TypeSafe's POST /v1/systemone, used as a typed-verdict
fallback when every regex adapter in review_phase.py raises ParseError.

Never reads an API key from the environment. The key is resolved through
launchd-kit/op-resolve.py's resolve_ref() against SECRET_REF. Failures raise
JevClosed with a secret-free reason; they never propagate another exception.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
SECRET_REF = "op://Automation/typesafe-jev/api-key"
CONFIDENCE_FLOOR = 0.70
OP_RESOLVE_PATH = Path(__file__).resolve().parents[2] / "launchd-kit" / "op-resolve.py"
DEFAULT_CONNECT_HOST = "http://192.168.50.88:8090"
KEYCHAIN_CONNECT_TOKEN_SERVICE = "op-connect-token"


APPROVED = "approved"
CHANGES_REQUESTED = "changes_requested"

# Measured arm-2 wording from the Jev review spike (27/27). Map the Choice
# enum onto this module's APPROVED / CHANGES_REQUESTED constants.
CRITERIA = {
    "APPROVE": (
        "The reviewer found no defect that must be fixed before merge. Any "
        "remaining notes are optional, cosmetic, or explicitly non-blocking."
    ),
    "REQUEST_CHANGES": (
        "The reviewer found at least one defect that must be fixed before "
        "merge - a correctness bug, a security or fail-open gap, a missing or "
        "fabricated test, or an unsupported claim material to the merge decision."
    ),
}
INSTRUCTIONS = (
    "This is an adversarial code review of one pull request. Decide the review's own overall "
    "verdict from its findings and assessment alone. Judge what the reviewer concluded, not "
    "whether you would agree with it. A review listing findings that it explicitly marks "
    "non-blocking, already-fixed, or informational is an APPROVE."
)
CHOICE_MAP = {
    "APPROVE": APPROVED,
    "REQUEST_CHANGES": CHANGES_REQUESTED,
}

# Patchable seams so tests never touch the network or 1Password.
urlopen = urllib.request.urlopen
sleep = time.sleep

_ANY_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*(.+?)\s*#*"
    r"|\**(FINDINGS|ISSUES|MINOR NOTES|VERDICT|ANALYSIS|SUMMARY|NOTES|RECOMMENDATION)\**\s*:.*"
    r"|\**(FINDINGS|ISSUES|MINOR NOTES|VERDICT|ANALYSIS|SUMMARY|NOTES|RECOMMENDATION)\**"
    r")\s*$",
    re.IGNORECASE,
)
_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})")


class JevClosed(Exception):
    """Fail-closed outcome. The message is safe to put in a phase `reason`."""


def _heading_title(match: re.Match[str]) -> str:
    return next(group for group in match.groups() if group is not None)


def _strip_quoted(text: str) -> str:
    """Drop fenced and every indented-4-space block; keep unquoted prose."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        fence = _FENCE.match(lines[i])
        if fence:
            mark = fence.group(2)[0]
            width = len(fence.group(2))
            i += 1
            while i < n:
                closer = _FENCE.match(lines[i])
                if closer:
                    closer_mark = closer.group(2)
                    if (
                        closer_mark[0] == mark
                        and len(closer_mark) >= width
                        and not lines[i][closer.end() :].strip()
                    ):
                        i += 1
                        break
                i += 1
            continue
        raw = lines[i]
        if raw.startswith("    ") or raw.startswith("\t"):
            i += 1
            continue
        out.append(raw)
        i += 1
    return "\n".join(out)


def trim_payload(text: str) -> str:
    """FINDINGS/VERDICT sections of `text`, with quoted file contents stripped.

    When those sections are absent, the whole return is stripped instead so a
    fallback call still has prose to judge.
    """
    lines = text.splitlines()
    chunks: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        heading = _ANY_HEADING.match(lines[i])
        if not heading:
            i += 1
            continue
        title = _heading_title(heading).strip().strip("*_ ").upper()
        keep = title.startswith("FINDINGS") or title.startswith("VERDICT")
        start = i
        i += 1
        while i < n and not _ANY_HEADING.match(lines[i]):
            i += 1
        if keep:
            chunks.append("\n".join(lines[start:i]))
    source = "\n\n".join(chunks) if chunks else text
    return _strip_quoted(source).strip()


def _load_op_resolve():
    path = OP_RESOLVE_PATH
    if not path.is_file():
        raise JevClosed("jev: could not load op-resolve")
    spec = importlib.util.spec_from_file_location("op_resolve", path)
    if spec is None or spec.loader is None:
        raise JevClosed("jev: could not load op-resolve")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        raise JevClosed("jev: could not load op-resolve") from None
    return module


def _default_connect_token() -> str:
    """Read the Connect token through op-resolve's Keychain implementation."""
    lookup = f"macOS Keychain service {KEYCHAIN_CONNECT_TOKEN_SERVICE}"
    try:
        module = _load_op_resolve()
        status, token = module._keychain_connect_token()
    except JevClosed:
        raise
    except Exception:
        raise JevClosed(
            "jev: OP_CONNECT_TOKEN unresolvable "
            f"(checked environ and {lookup}; credential resolver unavailable)"
        ) from None
    if status != "resolved" or not isinstance(token, str) or not token.strip():
        safe_status = (
            status
            if status in {"absent", "indeterminate", "present-unavailable", "resolved"}
            else "unknown"
        )
        raise JevClosed(
            "jev: OP_CONNECT_TOKEN unresolvable "
            f"(checked environ and {lookup}; status {safe_status})"
        )
    return token.strip()


def _default_resolve_ref(ref: str, host: str, token: str) -> str:
    try:
        module = _load_op_resolve()
    except JevClosed:
        raise
    except Exception:
        raise JevClosed("jev: could not load op-resolve") from None
    try:
        return module.resolve_ref(ref, host, token)
    except Exception as exc:
        if type(exc).__name__ == "ResolveError":
            raise JevClosed(f"jev: {exc}") from None
        raise JevClosed("jev: could not resolve secret") from None


def _reject_json_constant(value: str):
    raise JevClosed(f"jev: non-finite JSON constant {value}")


class JevClient:
    """POST /v1/systemone once (with 429/529 retries) for fallback or shadow."""

    def __init__(
        self,
        *,
        resolve_ref=None,
        urlopen_fn=None,
        environ=None,
        sleep_fn=None,
        default_connect_host=DEFAULT_CONNECT_HOST,
    ):
        env = os.environ if environ is None else environ
        host = (env.get("OP_CONNECT_HOST") or default_connect_host or "").strip()
        if not host:
            raise JevClosed(
                "jev: OP_CONNECT_HOST unresolvable "
                "(checked environ and configured default)"
            )
        token = (env.get("OP_CONNECT_TOKEN") or "").strip()
        if not token:
            token = _default_connect_token()
        resolver = _default_resolve_ref if resolve_ref is None else resolve_ref
        try:
            key = resolver(SECRET_REF, host, token)
        except JevClosed:
            raise
        except Exception as exc:
            if type(exc).__name__ == "ResolveError":
                raise JevClosed(f"jev: {exc}") from None
            raise JevClosed("jev: could not resolve secret") from None
        if not isinstance(key, str) or not key:
            raise JevClosed("jev: could not resolve secret")
        self._key = key
        self._urlopen = urlopen if urlopen_fn is None else urlopen_fn
        self._sleep = sleep if sleep_fn is None else sleep_fn
        self._model = env.get("JEV_MODEL") or "jev-latest"

    def _scrub(self, text: str) -> str:
        if self._key and self._key in text:
            return text.replace(self._key, "<redacted>")
        return text

    def decide(self, text: str) -> tuple[str, float]:
        """Return (verdict, confidence) or raise JevClosed. Never any other exception."""
        try:
            return self._decide(text)
        except JevClosed:
            raise
        except Exception:
            raise JevClosed("jev: request failed") from None

    def _decide(self, text: str) -> tuple[str, float]:
        body = {
            "state": trim_payload(text),
            "model": self._model,
            "questions": {
                "verdict": {
                    "type": "choice",
                    "instructions": INSTRUCTIONS,
                    "criteria": CRITERIA,
                }
            },
        }
        data = self._post(body, attempt=0)
        answers = data.get("answers") if isinstance(data, dict) else None
        verdict = answers.get("verdict") if isinstance(answers, dict) else None
        if not isinstance(verdict, dict):
            raise JevClosed("jev: malformed response")
        choice = verdict.get("choice")
        confidence = verdict.get("confidence")
        if choice not in CHOICE_MAP:
            raise JevClosed(
                f"jev: choice {choice!r} is not APPROVE or REQUEST_CHANGES"
            )
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            raise JevClosed("jev: malformed response")
        confidence = float(confidence)
        if not math.isfinite(confidence):
            raise JevClosed(f"jev: confidence {confidence!r} is not finite")
        if not 0 <= confidence <= 1:
            raise JevClosed(f"jev: confidence {confidence!r} outside [0, 1]")
        if confidence < CONFIDENCE_FLOOR:
            raise JevClosed(
                f"jev: confidence {confidence:.2f} below floor {CONFIDENCE_FLOOR:.2f}"
            )
        return CHOICE_MAP[choice], confidence

    def _post(self, body: dict, attempt: int) -> dict:
        req = urllib.request.Request(
            ENDPOINT,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._urlopen(req, timeout=60, context=ssl.create_default_context()) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                detail = ""
            detail = self._scrub(detail)
            if exc.code in (429, 529) and attempt < 4:
                self._sleep(2 ** attempt)
                return self._post(body, attempt + 1)
            extra = f": {detail}" if detail.strip() else ""
            raise JevClosed(f"jev: HTTP {exc.code}{extra}") from None
        except TimeoutError:
            raise JevClosed("jev: timeout") from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            blob = self._scrub(str(reason) if reason is not None else str(exc)).lower()
            if isinstance(reason, TimeoutError) or "timed out" in blob:
                raise JevClosed("jev: timeout") from None
            raise JevClosed("jev: request failed") from None
        try:
            data = json.loads(raw, parse_constant=_reject_json_constant)
        except (TypeError, ValueError):
            raise JevClosed("jev: malformed response") from None
        if not isinstance(data, dict):
            raise JevClosed("jev: malformed response")
        return data
