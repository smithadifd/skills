"""Make the flat memory_gardener module importable when pytest runs standalone:

    python3 -m pytest skills/memory-gardener/tests/

The toolkit has no repo-wide Python packaging or test runner, so the skill dir
is put on sys.path here. Every test is network-free and offline: pure logic over
hand-built strings plus the invented FIXTURE memory dir under tests/fixtures/
(no real memory content is ever copied into the repo).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
