"""Runs the graph's own check script (graph/test_build_swarm.py) under pytest, so CI has one entry point.

The script prints PASS/FAIL per check and exits non-zero on any failure; its output is shown on failure.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_graph_checks():
    result = subprocess.run([sys.executable, "-m", "graph.test_build_swarm"], cwd=ROOT,
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
