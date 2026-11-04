"""Orchestrator rules: the evidence rule, the patch sandbox, the verification plan."""
from pathlib import Path

import pytest

import gridtwin as G

ROOT = Path(__file__).resolve().parents[1]
STALE = [{"name": "stale", "value": "on"}]


def fail(sc, failure="no_trip"):
    return {"scenario": {**G.norm(sc)}, "result": "FAIL", "failure": failure}


# ---- evidence rule ---------------------------------------------------------
def test_finding_without_a_measured_fail_is_refused():
    b = {"left": 3}
    assert "error" in G.check_finding({"found_failure": True, "summary": "I reason it fails"}, b)
    assert G.check_finding({"found_failure": False, "summary": "nothing"}, b) == {"received": True}
    b["fails"] = [fail({"faults": STALE, "drift_mv": 400})]
    assert G.check_finding({"found_failure": True, "summary": "measured"}, b) == {"received": True}


def test_verdict_must_name_a_scenario_the_judge_saw_fail():
    b = {"left": 0, "fails": [fail({"faults": STALE, "drift_mv": 400})]}
    other = {"reproduced": True, "minimal_scenario": {"faults": STALE, "drift_mv": 500}}
    same = {"reproduced": True, "minimal_scenario": {"faults": [{"name": "stale", "value": "ON"}], "drift_mv": "400"}}
    assert "error" in G.check_verdict(other, b)
    assert G.check_verdict(same, b) == {"received": True}
    assert "error" in G.check_verdict(same, {"left": 2})          # no FAIL of its own at all
    assert G.check_verdict({"reproduced": False}, {"left": 0}) == {"received": True}


def test_scenario_key_normalises_types():
    a = {"faults": [{"name": "drop", "value": "5"}], "drift_mv": "400", "poll_ms": "50.0"}
    b = {"faults": [{"name": "drop", "value": 5}], "drift_mv": 400, "poll_ms": 50, "charge_a": 10, "timing": "random"}
    assert G.scenario_key(a) == G.scenario_key(b)


# ---- sandbox ---------------------------------------------------------------
def controllers():
    yield ROOT / "pi" / "controller.py"
    yield from sorted(ROOT.glob("runs/*/round*_controller.py"))


@pytest.mark.parametrize("path", list(controllers()), ids=lambda p: str(p.relative_to(ROOT)))
def test_every_real_controller_passes_the_sandbox(path):
    assert G.check_patch(path.read_text(encoding="utf-8")) is None


@pytest.mark.parametrize("evil", [
    "import subprocess",
    "import os",
    "from socket import socket",
    "eval('1')",
    "__import__('os')",
    "open('/etc/passwd')",
    "import time\ntime.sys.modules['os'].system('id')",
    "().__class__.__base__.__subclasses__()",
])
def test_sandbox_blocks(evil):
    base = (ROOT / "pi" / "controller.py").read_text(encoding="utf-8")
    assert G.check_patch(base + "\n" + evil + "\n") is not None


def test_cli_must_stay():
    base = (ROOT / "pi" / "controller.py").read_text(encoding="utf-8")
    assert "command-line interface" in G.check_patch(base.replace("--poll-ms", "--period"))


# ---- verification plan -----------------------------------------------------
class S:
    history = [{"round": 1, "fixed": True, "scenario": G.norm({"faults": STALE, "drift_mv": 400})},
               {"round": 2, "fixed": False, "scenario": G.norm({"faults": [], "drift_mv": 0})}]


def plan(mode, extra=()):
    G.INTERLOCK["mode"] = mode
    return [label for label, _, _ in G.verification_plan(S, {"minimal_scenario": {"faults": [], "drift_mv": 400}},
                                                          list(extra))]


def test_plan_replays_fixed_rounds_only_and_adds_held_out_checks():
    labels = plan("legacy")
    assert "regression: round 1 failure" in labels and "regression: round 2 failure" not in labels
    assert any(l.startswith("held-out:") for l in labels)
    assert not any("killed" in l for l in labels)                 # impossible under the legacy interlock
    assert any("killed" in l for l in plan("failsafe"))


def test_plan_drops_invalid_judge_checks():
    extra = [{"faults": [], "drift_mv": 300, "why": "ambiguous"},
             {"faults": [{"name": "stale", "value": "on"}], "drift_mv": 0, "why": "heavy fault, no OV"},
             {"faults": [{"name": "drop", "value": 5}], "drift_mv": 0, "why": "light noise"}]
    labels = plan("legacy", extra)
    assert not any("ambiguous" in l or "heavy fault" in l for l in labels)
    assert any("light noise" in l and "judge" in l for l in labels)


def test_random_checks_are_repeated():
    assert G.is_random(G.norm({"faults": [{"name": "drop", "value": 5}], "drift_mv": 0}))
    assert not G.is_random(G.norm({"faults": [{"name": "burst", "value": 2}], "drift_mv": 400}))
