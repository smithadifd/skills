#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
set -eu
rm skills/demo/tests/test_demo.py
rmdir skills/demo/tests
