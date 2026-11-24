#!/usr/bin/env python3
"""A writer verb with an isolated call log; payloads stay opaque bytes."""
import base64
import json
import os
from pathlib import Path
import sys

state_path = Path(os.environ["FAKE_RECORD_STATE"])
state = json.loads(state_path.read_text())
payload = sys.stdin.buffer.read()
state["calls"].append({"argv": sys.argv[1:], "stdin": base64.b64encode(payload).decode()})
answer = state["answers"].pop(0)
state_path.write_text(json.dumps(state))
print(answer["stdout"], end="")
print(answer["stderr"], end="", file=sys.stderr)
raise SystemExit(answer["status"])
