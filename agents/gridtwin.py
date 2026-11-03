#!/usr/bin/env python3
"""GridTwin-HIL orchestrator: three Muse agents attack, diagnose and fix a real energy controller.

    python agents/gridtwin.py              # full loop: attack -> diagnose -> fix -> verify
    python agents/gridtwin.py --reset      # put the original (vulnerable) controller back on the Pi
    python agents/gridtwin.py --scenario stale   # run one hand-written scenario, no AI (bench check)

Settings come from .env: MODEL_API_KEY, NUCLEO_PORT, PI_SSH (and optionally JUDGE_PROVIDER=baseten).
Every run is saved under runs/<timestamp>/ (scenarios, agent tool calls, verdict, patch, verification).
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import py_compile
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench import BENCH_INFO, REPO, Bench, BenchError, PiController  # noqa: E402
from llm import Tool, backend_from_env, run_agent  # noqa: E402

load_dotenv(REPO / ".env")
BASELINE_CONTROLLER = REPO / "pi" / "controller.py"

# ----------------------------------------------------------------------------- console
C = {"attacker": "\033[91m", "judge": "\033[93m", "fixer": "\033[92m", "bench": "\033[96m",
     "dim": "\033[90m", "bold": "\033[1m", "end": "\033[0m"}
if os.name == "nt":
    os.system("")                       # enable ANSI colours in Windows terminals


def say(who: str, text: str) -> None:
    print(f"{C.get(who, '')}{C['bold']}[{who}]{C['end']} {text}", flush=True)


def short(obj, n: int = 160) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return s if len(s) <= n else s[: n - 3] + "..."


def on_event(kind: str, d: dict) -> None:
    who = d["agent"]
    if kind == "tool_call":
        say(who, f"-> {d['tool']}({short(d['args'], 140)})")
    elif kind == "tool_result":
        r = d["result"]
        if isinstance(r, dict) and "result" in r:
            lat = f" {r['latency_ms']} ms" if "latency_ms" in r else (f" ({r['failure']})" if "failure" in r else "")
            say("bench", f"{r['result']}{lat}  {short(r.get('scenario', {}), 120)}")
        elif isinstance(r, dict) and "error" in r:
            say("bench", f"error: {r['error']}")
    elif kind == "thought":
        print(f"{C['dim']}   {short(d['text'].strip().replace(chr(10), ' '), 220)}{C['end']}", flush=True)


# ----------------------------------------------------------------------------- tool schemas
FAULT_ITEMS = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "enum": ["stale", "delay", "drop", "crc"]},
        "value": {"type": "string", "description": "stale: 'on'; delay: ms 0-2000; drop/crc: percent 0-100"},
    },
    "required": ["name", "value"],
}
SCENARIO_PARAMS = {
    "type": "object",
    "properties": {
        "faults": {"type": "array", "items": FAULT_ITEMS,
                   "description": "Faults active during the run. Empty list = a healthy BMS."},
        "drift_mv": {"type": "integer", "description": "How far cell 4 is pushed up (0-800 mV). ~400 crosses the 3650 mV limit."},
        "charge_a": {"type": "integer", "description": "Charging current in A (-100..100). Default 10."},
        "hypothesis": {"type": "string", "description": "One sentence: what you expect to happen and why."},
    },
    "required": ["faults", "drift_mv", "hypothesis"],
}


class Session:
    def __init__(self, bench: Bench, pi: PiController, run_dir: Path):
        self.bench, self.pi, self.run_dir = bench, pi, run_dir
        self.scenarios: list[dict] = []
        self.source = BASELINE_CONTROLLER.read_text()

    def save(self, name: str, data) -> None:
        p = self.run_dir / name
        p.write_text(data if isinstance(data, str) else json.dumps(data, indent=2, default=str), encoding="utf-8")

    # tools ------------------------------------------------------------------
    def describe_bench(self) -> dict:
        return BENCH_INFO

    def read_controller_source(self) -> dict:
        return {"path": "pi/controller.py", "source": self.source}

    def run_scenario(self, faults, drift_mv=400, charge_a=10, hypothesis="", budget=None) -> dict:
        if budget is not None and budget["left"] <= 0:
            return {"error": "scenario budget used up: submit your result now"}
        if not self.pi.running():
            self.pi.start()
        r = self.bench.run_scenario(faults, drift_mv=drift_mv, charge_a=charge_a, controller=self.pi)
        r["hypothesis"] = hypothesis
        self.scenarios.append(r)
        self.save("scenarios.json", self.scenarios)
        if budget is not None:
            budget["left"] -= 1
            r["scenarios_left"] = budget["left"]
        return r

    def scenario_tool(self, budget: dict) -> Tool:
        return Tool("run_scenario",
                    "Run one test on the real hardware: apply faults to the emulated BMS, push cell 4 over the "
                    "limit, and get the STM32's hardware-timed verdict plus the controller's log. Takes ~10 s.",
                    SCENARIO_PARAMS,
                    lambda faults, drift_mv=400, charge_a=10, hypothesis="":
                        self.run_scenario(faults, drift_mv, charge_a, hypothesis, budget))


# ----------------------------------------------------------------------------- agents
ATTACKER = """You are the attacker in GridTwin-HIL, a hardware-in-the-loop test bench for battery-energy
controllers. A real Raspberry Pi runs the controller under test; an STM32 emulates the battery's BMS and
measures, in hardware, whether the controller trips in time when a cell over-voltages.

Your job: find a realistic condition under which the controller FAILS its safety rule.
- Read the bench description and the controller source first, and reason about its weak points.
- Every scenario runs on real hardware and costs ~10 s, so each one must test a specific hypothesis.
- Think about how real BMS links fail in the field: stuck/frozen data, slow replies, lost replies, corruption.
- A FAIL is only meaningful if the same scenario without faults PASSES; check the healthy baseline once.
- When you have a reproducible failure (or the budget is spent), call submit_finding."""

JUDGE = """You are the independent judge in GridTwin-HIL. The attacker reports a failure of a battery
controller's over-voltage trip. You did not design the attack; do not trust its explanation.

Your job: establish the root cause from evidence.
1. Reproduce the failure once.
2. Find the MINIMAL scenario that still fails (remove or reduce faults one at a time).
3. Read the controller source and locate the code responsible.
4. Call submit_verdict with the root cause, the minimal failing scenario, the evidence (cite measured
   numbers and controller log lines) and precise requirements a fix must meet.
Do not propose code. Be concrete and short."""

FIXER = """You are the fixer in GridTwin-HIL. You receive the judge's verdict on a safety failure of a
battery controller running on a Raspberry Pi, and the controller's full source.

Write a minimal, careful patch that makes the controller meet the safety rule under the failing condition,
without causing false trips on a healthy link and without changing its command-line interface.
Principle: if the controller cannot trust its view of the battery, it must fail safe (trip).
Return the COMPLETE patched file via submit_patch. You will not test it yourself: the orchestrator
replays the judge's failing scenario plus a healthy regression on the real hardware."""


def obj(props: dict, req: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": req}


def attack(s: Session, backend, budget: int) -> dict | None:
    say("attacker", f"start ({backend.label}), budget {budget} scenarios")
    b = {"left": budget}
    tools = [
        Tool("describe_bench", "Register map, fault library, safety rule and how a scenario runs.",
             obj({}, []), s.describe_bench),
        Tool("read_controller_source", "Full Python source of the controller under test.",
             obj({}, []), s.read_controller_source),
        s.scenario_tool(b),
        Tool("submit_finding", "Report the failure you found (or that you found none).",
             obj({"found_failure": {"type": "boolean"},
                  "scenario": {"type": "object", "description": "The failing scenario: faults, drift_mv, charge_a"},
                  "summary": {"type": "string", "description": "What fails and why you think so, citing measurements"}},
                 ["found_failure", "summary"]),
             lambda **kw: {"received": True}, final=True),
    ]
    run = run_agent(backend, "attacker", ATTACKER,
                    "Find a condition under which this controller fails its over-voltage trip.",
                    tools, max_turns=budget + 8, on_event=on_event)
    s.save("attacker.json", {"final": run.final, "transcript": run.transcript, "tokens": run.tokens})
    if run.final:
        say("attacker", f"finding: {run.final.get('summary', '')}")
    return run.final


def judge(s: Session, backend, finding: dict, budget: int) -> dict | None:
    say("judge", f"start ({backend.label}), budget {budget} scenarios")
    b = {"left": budget}
    tools = [
        Tool("describe_bench", "Register map, fault library, safety rule and how a scenario runs.",
             obj({}, []), s.describe_bench),
        Tool("read_controller_source", "Full Python source of the controller under test.",
             obj({}, []), s.read_controller_source),
        s.scenario_tool(b),
        Tool("submit_verdict", "Your root-cause verdict.",
             obj({"reproduced": {"type": "boolean"},
                  "minimal_scenario": {"type": "object", "description": "faults, drift_mv, charge_a"},
                  "root_cause": {"type": "string"},
                  "code_location": {"type": "string", "description": "function / lines responsible"},
                  "evidence": {"type": "array", "items": {"type": "string"}},
                  "fix_requirements": {"type": "array", "items": {"type": "string"}}},
                 ["reproduced", "minimal_scenario", "root_cause", "evidence", "fix_requirements"]),
             lambda **kw: {"received": True}, final=True),
    ]
    run = run_agent(backend, "judge", JUDGE,
                    "The attacker reports:\n" + json.dumps(finding, indent=2),
                    tools, max_turns=budget + 8, on_event=on_event)
    s.save("judge.json", {"final": run.final, "transcript": run.transcript, "tokens": run.tokens})
    if run.final:
        say("judge", f"root cause: {run.final.get('root_cause', '')}")
    return run.final


def fix(s: Session, backend, verdict: dict, feedback: str = "") -> dict | None:
    say("fixer", f"start ({backend.label})")
    tools = [Tool("submit_patch", "Submit the complete patched controller source.",
                  obj({"explanation": {"type": "string"},
                       "patched_source": {"type": "string", "description": "The COMPLETE new pi/controller.py"}},
                      ["explanation", "patched_source"]),
                  lambda **kw: {"received": True}, final=True)]
    task = ("Judge's verdict:\n" + json.dumps(verdict, indent=2) +
            "\n\nCurrent controller source (pi/controller.py):\n```python\n" + s.source + "\n```")
    if feedback:
        task += "\n\nYour previous patch was rejected:\n" + feedback
    run = run_agent(backend, "fixer", FIXER, task, tools, max_turns=4, on_event=on_event)
    s.save("fixer.json", {"final": run.final, "transcript": run.transcript, "tokens": run.tokens})
    return run.final


def check_patch(source: str) -> str | None:
    """Static checks before anything touches the Pi."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(source)
    try:
        py_compile.compile(f.name, doraise=True)
    except py_compile.PyCompileError as e:
        return f"does not compile: {e.msg}"
    finally:
        os.unlink(f.name)
    for needle in ("--poll-ms", "--port", "argparse"):
        if needle not in source:
            return f"command-line interface changed ({needle} missing)"
    return None


def verify(s: Session, minimal: dict) -> tuple[bool, list[dict]]:
    """The fixer never grades itself: replay the failure and a healthy regression on hardware."""
    checks = [("judge's failing scenario", minimal),
              ("healthy regression", {"faults": [], "drift_mv": 400, "charge_a": 10})]
    results, ok = [], True
    for label, sc in checks:
        say("bench", f"verify: {label}")
        r = s.run_scenario(sc.get("faults", []), sc.get("drift_mv", 400), sc.get("charge_a", 10),
                           hypothesis=f"verification: {label}")
        results.append({"check": label, **r})
        say("bench", f"   -> {r['result']}" + (f" {r['latency_ms']} ms" if "latency_ms" in r else ""))
        ok &= r["result"] == "PASS"
    # no false trips on a healthy idle link
    say("bench", "verify: no false trip on a healthy idle link (5 s)")
    s.bench.command("baseline")
    time.sleep(5)
    t = s.bench.fresh_telemetry()
    idle_ok = not t["trip"]
    results.append({"check": "no false trip at idle", "result": "PASS" if idle_ok else "FAIL"})
    say("bench", f"   -> {'PASS' if idle_ok else 'FAIL (trip line high with a healthy battery)'}")
    return ok and idle_ok, results


# ----------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="GridTwin-HIL agent loop")
    ap.add_argument("--reset", action="store_true", help="deploy the original controller and exit")
    ap.add_argument("--scenario", choices=["healthy", "stale", "delay", "drop", "crc"],
                    help="run one hand-written scenario without AI")
    ap.add_argument("--attack-budget", type=int, default=5)
    ap.add_argument("--judge-budget", type=int, default=4)
    args = ap.parse_args()

    port, host = os.environ.get("NUCLEO_PORT", "COM5"), os.environ.get("PI_SSH", "")
    if not host:
        sys.exit("Set PI_SSH in .env (e.g. pi5test@pi5-1.local)")
    run_dir = REPO / "runs" / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    pi = PiController(host)
    pi.check()
    bench = Bench(port)
    bench.fresh_telemetry()
    s = Session(bench, pi, run_dir)
    say("bench", f"Nucleo on {port}, Pi at {host}, run saved to {run_dir.relative_to(REPO)}")

    try:
        pi.deploy(s.source)                      # always start from the original controller
        pi.start()
        if args.reset:
            say("bench", "original controller deployed")
            return 0
        if args.scenario:
            faults = {"healthy": [], "stale": [{"name": "stale", "value": "on"}],
                      "delay": [{"name": "delay", "value": 300}], "drop": [{"name": "drop", "value": 60}],
                      "crc": [{"name": "crc", "value": 60}]}[args.scenario]
            r = s.run_scenario(faults, 400, 10, f"manual: {args.scenario}")
            print(json.dumps(r, indent=2))
            return 0

        attacker_b, judge_b = backend_from_env(), backend_from_env("JUDGE")
        finding = attack(s, attacker_b, args.attack_budget)
        if not finding or not finding.get("found_failure"):
            say("attacker", "no failure found")
            return 1
        verdict = judge(s, judge_b, finding, args.judge_budget)
        if not verdict or not verdict.get("reproduced"):
            say("judge", "could not reproduce the attacker's finding")
            return 1

        feedback = ""
        for attempt in (1, 2):
            patch = fix(s, attacker_b, verdict, feedback)
            if not patch:
                say("fixer", "no patch submitted")
                return 1
            problem = check_patch(patch["patched_source"])
            if problem:
                feedback = f"Static check failed: {problem}"
                say("fixer", f"patch rejected: {problem}")
                continue
            diff = "".join(difflib.unified_diff(s.source.splitlines(True), patch["patched_source"].splitlines(True),
                                                "pi/controller.py", "pi/controller.py (patched)"))
            s.save(f"patch_{attempt}.diff", diff)
            s.save(f"controller_patched_{attempt}.py", patch["patched_source"])
            say("fixer", f"patch: {patch['explanation']}")
            print(C["dim"] + diff + C["end"])

            say("bench", "deploying patch to the Pi and restarting the controller")
            pi.deploy(patch["patched_source"])
            pi.start()
            ok, results = verify(s, verdict.get("minimal_scenario") or finding.get("scenario") or {})
            s.save(f"verification_{attempt}.json", results)
            if ok:
                say("bench", f"{C['bold']}ALL CHECKS PASS{C['end']}: failure found, diagnosed and fixed on hardware")
                s.save("summary.json", {"finding": finding, "verdict": verdict, "patch": patch["explanation"],
                                        "verification": results})
                return 0
            feedback = "Hardware verification failed:\n" + json.dumps(results, indent=2, default=str)[:4000]
            say("fixer", "verification failed, retrying with the results")
        return 1
    except BenchError as e:
        say("bench", f"error: {e}")
        return 2
    finally:
        pi.stop()
        bench.close()


if __name__ == "__main__":
    sys.exit(main())
