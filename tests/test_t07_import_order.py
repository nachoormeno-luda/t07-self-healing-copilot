"""Regression: the pack's tools module must be importable *before* the `app` package.

Vertex AI Agent Runtime unpickles the agent by importing `seed_packs.t07_self_healing_ops.tools`
first; a circular import through `app/__init__.py` (which eagerly builds the agent) used to leave
`send_a2a_text = None`, silently disabling the A2A advisory tool in the cloud.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_SNIPPET = """
import warnings; warnings.filterwarnings("ignore")
import seed_packs.t07_self_healing_ops.tools as t
assert callable(t.send_a2a_text), f"send_a2a_text is {t.send_a2a_text!r}"
from seed_packs.t07_self_healing_ops.guardrails import before_tool_callback
import app.agent
print("OK")
"""


def test_tools_import_first_keeps_a2a_client_and_guardrails_loadable():
    env = {**os.environ, "SEED_OFFLINE_MODE": "1", "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.run(
        [sys.executable, "-c", _SNIPPET],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert proc.stdout.strip().endswith("OK")
