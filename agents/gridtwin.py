#!/usr/bin/env python3
"""GridTwin-HIL orchestrator: Muse agents attack, diagnose, fix and re-attack a real energy controller.

    python agents/gridtwin.py                 # campaign: rounds of attack -> diagnose -> fix+review -> verify
    python agents/gridtwin.py --rounds 1      # a single round
    python agents/gridtwin.py --reset         # put the original (vulnerable) controller back on the Pi
    python agents/gridtwin.py --scenario stale [--poll-ms 50]   # one hand-written scenario, no AI
    python agents/gridtwin.py --scenario healthy --poll-ms 500 --timing worst_case

Settings come from .env: MODEL_API_KEY, NUCLEO_PORT, PI_SSH (optionally JUDGE_PROVIDER=baseten).
Every run is saved under runs/<timestamp>/.

A round:
  attacker  finds a failure of either safety rule, knowing what earlier rounds found and fixed
  judge     reproduces it, isolates the minimal scenario, names the root cause
            (evidence rule: a finding or verdict claiming a failure is refused unless that agent's
             own hardware runs include a FAIL, and the judge's minimal scenario is one it saw FAIL)
  fixer     writes a patch                               <-+
  judge     reviews the patch before it reaches the Pi;    | rejected: reasons go back
            can add its own hardware checks              --+
  hardware  replays this round's failure, every earlier round's failure, a healthy over-voltage,
            a healthy idle (no false trip) and the judge's extra checks. Only all-PASS counts.
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
DEFAULT_POLL_MS = 200

# ----------------------------------------------------------------------------- console
C = {"attacker": "\033[91m", "judge": "\033[93m", "fixer": "\033[92m", "bench": "\033[96m",
     "round": "\033[95m", "dim": "\033[90m", "bold": "\033[1m", "end": "\033[0m"}
if os.name == "nt":
    os.system("")                       # enable ANSI colours in Windows terminals


def say(who: str, text: str) -> None:
    print(f"{C.get(who, '')}{C['bold']}[{who}]{C['end']} {text}", flush=True)


def short(obj, n: int = 160) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return s if len(s) <= n else s[: n - 3] + "..."


def verdict_text(r: dict) -> str:
    if "latency_ms" in r:
        return f"{r['result']} {r['latency_ms']} ms" + (" (already tripped)" if r.get("note") else "")
    if r.get("failure"):
        return f"{r['result']} ({r['failure']})"
    if r.get("observation"):
        return f"{r['result']} ({r['observation']})"
    return f"{r['result']}" + (f" ({r['check']})" if r.get("check") else "") + (f": {r['reason']}" if r.get("reason") else "")


def on_event(kind: str, d: dict) -> None:
    who = d["agent"]
    if kind == "tool_call":
        say(who, f"-> {d['tool']}({short(d['args'], 140)})")
    elif kind == "tool_result":
        r = d["result"]
        if isinstance(r, dict) and "result" in r:
            say("bench", f"{verdict_text(r)}  {short(r.get('scenario', {}), 120)}")
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
SCENARIO_PROPS = {
    "faults": {"type": "array", "items": FAULT_ITEMS, "description": "Faults active during the run. Empty list = a healthy BMS link."},
    "drift_mv": {"type": "integer", "description": "How far cell 4 is pushed up, 0-800 mV. >=400 causes an over-voltage (rule 1); <=250 does not (tests rule 2)."},
    "charge_a": {"type": "integer", "description": "Charging current in A, -100..100. Default 10."},
    "poll_ms": {"type": "integer", "description": "Controller polling interval for this run, 50-500 ms. Default 200."},
    "timing": {"type": "string", "enum": ["random", "worst_case"],
               "description": "When the over-voltage starts relative to the controller's polling. random (default): "
                              "anywhere in the poll cycle. worst_case: right after the BMS answers a read, so "
                              "timing races reproduce every time."},
}
SCENARIO_PARAMS = {"type": "object",
                   "properties": {**SCENARIO_PROPS,
                                  "hypothesis": {"type": "string", "description": "One sentence: what you expect and why."}},
                   "required": ["faults", "drift_mv", "hypothesis"]}


def obj(props: dict, req: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": req}


def norm(sc: dict | None) -> dict:
    sc = sc or {}
    return {"faults": sc.get("faults") or [], "drift_mv": int(sc.get("drift_mv", 400)),
            "charge_a": int(sc.get("charge_a", 10)), "poll_ms": int(sc.get("poll_ms", DEFAULT_POLL_MS)),
            "timing": str(sc.get("timing") or "random")}


def scenario_key(sc: dict) -> tuple:
    """What makes two scenarios the same test (for matching a claim to a measured run)."""
    n = norm(sc)
    faults = tuple(sorted((str(f.get("name")), str(f.get("value")).lower()) for f in n["faults"]))
    return (faults, n["drift_mv"], n["charge_a"], max(50, min(500, n["poll_ms"])), n["timing"])


def fail_summary(r: dict) -> dict:
    """Compact record of a measured FAIL, used as hardware evidence."""
    return {"scenario": r.get("scenario"), "result": r.get("result"), "failure": r.get("failure"),
            "latency_ms": r.get("latency_ms"), "until_reported_ms": r.get("until_reported_ms"),
            "detail": r.get("detail")}


class Session:
    def __init__(self, bench: Bench, pi: PiController, run_dir: Path):
        self.bench, self.pi, self.run_dir = bench, pi, run_dir
        self.scenarios: list[dict] = []
        self.original = BASELINE_CONTROLLER.read_text()
        self.source = self.original            # the controller currently deployed
        self.history: list[dict] = []          # one entry per completed round

    def save(self, name: str, data) -> None:
        (self.run_dir / name).write_text(data if isinstance(data, str) else json.dumps(data, indent=2, default=str),
                                         encoding="utf-8")

    def describe_bench(self) -> dict:
        return BENCH_INFO

    def read_controller_source(self) -> dict:
        return {"path": "pi/controller.py (as currently deployed)", "source": self.source}

    def run_scenario(self, faults, drift_mv=400, charge_a=10, poll_ms=DEFAULT_POLL_MS, hypothesis="",
                     budget=None, timing="random") -> dict:
        if budget is not None and budget["left"] <= 0:
            return {"error": "scenario budget used up: submit your result now"}
        poll_ms = int(max(50, min(500, int(poll_ms or DEFAULT_POLL_MS))))
        if not self.pi.running() or self.pi.poll_ms != poll_ms:
            self.pi.poll_ms = poll_ms
            self.pi.start()
        r = self.bench.run_scenario(faults, drift_mv=drift_mv, charge_a=charge_a, controller=self.pi,
                                    timing=timing or "random")
        r["hypothesis"] = hypothesis
        self.scenarios.append(r)
        self.save("scenarios.json", self.scenarios)
        if budget is not None:
            budget["left"] -= 1
            r["scenarios_left"] = budget["left"]
            if r.get("result") == "FAIL":
                budget.setdefault("fails", []).append(fail_summary(r))
        return r

    def scenario_tool(self, budget: dict) -> Tool:
        return Tool("run_scenario",
                    "Run one test on the real hardware: apply faults to the emulated BMS, optionally change the "
                    "controller's polling rate, push cell 4 up by drift_mv, and get the STM32's hardware-timed "
                    "verdict plus the controller's log. Takes ~10 s.",
                    SCENARIO_PARAMS,
                    lambda faults, drift_mv=400, charge_a=10, poll_ms=DEFAULT_POLL_MS, hypothesis="", timing="random":
                        self.run_scenario(faults, drift_mv, charge_a, poll_ms, hypothesis, budget, timing))

    def common_tools(self) -> list[Tool]:
        return [Tool("describe_bench", "Register map, safety rules, deployment range, fault library, how a scenario runs.",
                     obj({}, []), self.describe_bench),
                Tool("read_controller_source", "Full Python source of the controller as currently deployed.",
                     obj({}, []), self.read_controller_source)]

    def history_text(self) -> str:
        if not self.history:
            return "No earlier rounds: this is the original controller."
        fixed = [h for h in self.history if h["fixed"]]
        unproven = [h for h in self.history if not h["fixed"]]
        lines = []
        if fixed:
            lines.append("Earlier rounds (these failures were found, fixed and verified on hardware; do not report them again):")
            for h in fixed:
                lines.append(f"- Round {h['round']}: {h['root_cause']} Failing scenario: {json.dumps(h['scenario'])}. "
                             f"Fix applied: {h['fix']}")
        if unproven:
            lines.append("Suspected in earlier rounds but NOT reproduced on hardware (nothing was changed):")
            for h in unproven:
                lines.append(f"- Round {h['round']}: {h['root_cause']}")
        return "\n".join(lines)


# ----------------------------------------------------------------------------- prompts
ATTACKER = """You are the attacker in GridTwin-HIL, a hardware-in-the-loop test bench for battery-energy
controllers. A real Raspberry Pi runs the controller under test; an STM32 emulates the battery's BMS and
measures in hardware whether the controller meets its safety rules.

Your job: find a realistic condition under which the controller as currently deployed breaks a safety rule.
- Start from the bench description and the controller source. Reason about how the code could misbehave,
  then design scenarios that test those specific hypotheses.
- Every scenario runs on real hardware and costs ~10 s; make each one count.
- A failure only counts if it is caused by the condition you created: compare against a healthy control.
- A finding needs a FAIL measured on the hardware. Reasoning alone is not accepted: if you suspect a
  failure you could not make happen, keep testing or report that you found none. describe_bench explains
  how to make timing-dependent behaviour reproducible.
- If earlier rounds are listed, the controller has been patched for them. Look for something new,
  including problems the patches themselves may have introduced.
- When you have a reproducible failure, or your budget is spent, call submit_finding."""

JUDGE = """You are the independent judge in GridTwin-HIL. The attacker reports that a battery controller
breaks a safety rule. You did not design the attack; do not trust its explanation.

1. Reproduce the failure on the hardware.
2. Find the MINIMAL scenario that still fails: remove or reduce faults and parameters one at a time.
3. Read the controller source and locate the responsible code.
4. Call submit_verdict: root cause, minimal failing scenario, evidence (cite measured numbers and controller
   log lines) and precise requirements a fix must meet. Do not write code.
Only the hardware decides: reproduced=true requires that one of YOUR runs FAILED, and minimal_scenario must be
exactly a scenario you ran and saw FAIL. If you could not make it fail, submit reproduced=false."""

FIXER = """You are the fixer in GridTwin-HIL. You receive the judge's verdict on a safety failure of a
battery controller running on a Raspberry Pi, the history of earlier fixes, and the controller's full source.

Write a minimal, careful patch that makes the controller meet both safety rules under the failing condition,
keeps every earlier fix working, causes no false trips on a healthy link across the deployment range
(--poll-ms 50-500), and keeps the command-line interface unchanged.
If the controller cannot trust its view of the battery, it must fail safe (trip).
Return the COMPLETE patched file via submit_patch. The judge will review it, then the hardware will test it."""

REVIEW = """You are the judge in GridTwin-HIL, now reviewing a patch BEFORE it is deployed to the real
controller. Review it like a senior engineer responsible for site safety:
- Does it remove the root cause you identified, for the minimal failing scenario?
- Can it cause a false trip on a healthy link anywhere in the deployment range (--poll-ms 50-500)?
  Check timing assumptions against the bench description (e.g. how often values change).
- Can it miss or delay a real over-voltage trip? Does it keep every earlier fix working?
- Edge cases: start-up, counter wrap-around, recovery after faults clear.
Approve only if it is sound. If you reject, list concrete problems the fixer must address.
Either way you may add up to 3 extra hardware checks that would expose a flaw in this patch; they run
after deployment and must all PASS. Call submit_review."""


# ----------------------------------------------------------------------------- agents
def attack(s: Session, backend, budget: int, rnd: int) -> dict | None:
    say("attacker", f"start ({backend.label}), budget {budget} scenarios")
    b = {"left": budget}
    tools = s.common_tools() + [
        s.scenario_tool(b),
        Tool("submit_finding", "Report the failure you found, or that you found none.",
             obj({"found_failure": {"type": "boolean"},
                  "rule_broken": {"type": "string", "enum": ["1_trip_in_time", "2_no_false_trips", "none"]},
                  "scenario": obj(SCENARIO_PROPS, ["faults", "drift_mv"]),
                  "summary": {"type": "string", "description": "What fails and why, citing measurements"}},
                 ["found_failure", "summary"]),
             lambda **kw: check_finding(kw, b), final=True)]
    task = f"Round {rnd}.\n{s.history_text()}\n\nFind a condition under which the controller breaks a safety rule."
    run = run_agent(backend, "attacker", ATTACKER, task, tools, max_turns=budget + 8, on_event=on_event)
    if run.final and run.final.get("found_failure"):
        run.final["hardware_evidence"] = b.get("fails", [])
    s.save(f"round{rnd}_attacker.json", {"final": run.final, "transcript": run.transcript, "tokens": run.tokens,
                                         "refused_submissions": b.get("refused", [])})
    if run.final:
        say("attacker", f"finding: {run.final.get('summary', '')}")
    return run.final


def investigate(s: Session, backend, finding: dict, budget: int, rnd: int) -> dict | None:
    say("judge", f"start ({backend.label}), budget {budget} scenarios")
    b = {"left": budget}
    tools = s.common_tools() + [
        s.scenario_tool(b),
        Tool("submit_verdict", "Your root-cause verdict.",
             obj({"reproduced": {"type": "boolean"},
                  "rule_broken": {"type": "string", "enum": ["1_trip_in_time", "2_no_false_trips"]},
                  "minimal_scenario": obj(SCENARIO_PROPS, ["faults", "drift_mv"]),
                  "root_cause": {"type": "string"},
                  "code_location": {"type": "string"},
                  "evidence": {"type": "array", "items": {"type": "string"}},
                  "fix_requirements": {"type": "array", "items": {"type": "string"}}},
                 ["reproduced", "minimal_scenario", "root_cause", "evidence", "fix_requirements"]),
             lambda **kw: check_verdict(kw, b), final=True)]
    task = f"{s.history_text()}\n\nThe attacker reports:\n{json.dumps(finding, indent=2)}"
    run = run_agent(backend, "judge", JUDGE, task, tools, max_turns=budget + 8, on_event=on_event)
    v = run.final
    if v and v.get("reproduced"):
        # belt and braces: the tool already refused unproven verdicts, check again here
        key = scenario_key(v.get("minimal_scenario") or {})
        proof = [f for f in b.get("fails", []) if scenario_key(f["scenario"] or {}) == key]
        if not proof:
            v = {**v, "reproduced": False, "overruled": "no FAIL measured by the judge for its minimal scenario"}
        else:
            v["hardware_evidence"] = proof
    s.save(f"round{rnd}_judge.json", {"final": v, "transcript": run.transcript, "tokens": run.tokens,
                                      "refused_submissions": b.get("refused", [])})
    if v:
        say("judge", f"root cause: {v.get('root_cause', '')}" if v.get("reproduced")
            else "NOT reproduced on hardware" + (f" ({v['overruled']})" if v.get("overruled") else ""))
    return v


def _refuse(b: dict, why: str, payload: dict) -> dict:
    b.setdefault("refused", []).append({"why": why, "submission": payload})
    say("bench", f"submission refused: {why}")
    return {"error": why}


def check_finding(kw: dict, b: dict) -> dict:
    """Evidence rule for the attacker: a claimed failure needs a measured FAIL."""
    if kw.get("found_failure") and not b.get("fails"):
        return _refuse(b, "None of your scenarios FAILED on the hardware, so this finding is not accepted. "
                          + ("Run more scenarios to make it fail, or " if b["left"] > 0 else "")
                          + "submit found_failure=false with what you suspected.", kw)
    return {"received": True}


def check_verdict(kw: dict, b: dict) -> dict:
    """Evidence rule for the judge: reproduced=true needs a FAIL of exactly the minimal scenario."""
    if not kw.get("reproduced"):
        return {"received": True}
    fails = b.get("fails", [])
    if not fails:
        return _refuse(b, "reproduced=true, but none of YOUR scenarios FAILED on the hardware. "
                          + ("Run it again, or " if b["left"] > 0 else "")
                          + "submit reproduced=false.", kw)
    key = scenario_key(kw.get("minimal_scenario") or {})
    if not any(scenario_key(f["scenario"] or {}) == key for f in fails):
        return _refuse(b, "minimal_scenario must be exactly one of the scenarios you ran that FAILED. "
                          f"Your FAILs: {json.dumps([f['scenario'] for f in fails])}", kw)
    return {"received": True}


def write_patch(s: Session, backend, verdict: dict, feedback: str, rnd: int, attempt: int) -> dict | None:
    say("fixer", f"start ({backend.label}), attempt {attempt}")
    tools = [Tool("submit_patch", "Submit the complete patched controller source.",
                  obj({"explanation": {"type": "string"},
                       "patched_source": {"type": "string", "description": "The COMPLETE new pi/controller.py"}},
                      ["explanation", "patched_source"]),
                  lambda **kw: {"received": True}, final=True)]
    task = (f"{s.history_text()}\n\nJudge's verdict for this round:\n{json.dumps(verdict, indent=2)}"
            f"\n\nCurrent controller source:\n```python\n{s.source}\n```")
    if feedback:
        task += f"\n\nYour previous patch was rejected:\n{feedback}"
    run = run_agent(backend, "fixer", FIXER, task, tools, max_turns=4, on_event=on_event)
    s.save(f"round{rnd}_fixer_{attempt}.json", {"final": run.final, "tokens": run.tokens})
    return run.final


def review_patch(s: Session, backend, verdict: dict, patch: dict, diff: str, rnd: int, attempt: int) -> dict:
    say("judge", "reviewing the patch before it touches the Pi")
    tools = [Tool("submit_review", "Approve or reject the patch, and optionally add hardware checks.",
                  obj({"approve": {"type": "boolean"},
                       "problems": {"type": "array", "items": {"type": "string"}},
                       "extra_checks": {"type": "array", "maxItems": 3,
                                        "items": obj({**SCENARIO_PROPS,
                                                      "why": {"type": "string", "description": "What flaw this would expose"}},
                                                     ["faults", "drift_mv", "why"])}},
                      ["approve", "problems"]),
                  lambda **kw: {"received": True}, final=True)]
    task = (f"{json.dumps(BENCH_INFO)}\n\n{s.history_text()}\n\nYour verdict:\n{json.dumps(verdict, indent=2)}"
            f"\n\nFixer's explanation: {patch['explanation']}\n\nDiff:\n```diff\n{diff}\n```"
            f"\n\nFull patched source:\n```python\n{patch['patched_source']}\n```")
    run = run_agent(backend, "judge", REVIEW, task, tools, max_turns=4, on_event=on_event)
    s.save(f"round{rnd}_review_{attempt}.json", {"final": run.final, "tokens": run.tokens})
    review = run.final or {"approve": False, "problems": ["the judge did not submit a review"]}
    if review.get("approve"):
        say("judge", "patch APPROVED" + (f", with {len(review.get('extra_checks') or [])} extra checks"
                                         if review.get("extra_checks") else ""))
    else:
        say("judge", "patch REJECTED: " + "; ".join(review.get("problems") or []))
    return review


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


def verification_plan(s: Session, verdict: dict, extra: list[dict]) -> list[tuple[str, dict, str]]:
    """(label, scenario, required result). The fixer never chooses these."""
    plan = [("this round's failing scenario", norm(verdict.get("minimal_scenario")), "PASS")]
    for h in s.history:
        if h["fixed"]:
            plan.append((f"regression: round {h['round']} failure", norm(h["scenario"]), "PASS"))
    plan += [("healthy over-voltage (worst-case timing)", norm({"faults": [], "drift_mv": 400, "timing": "worst_case"}), "PASS"),
             ("healthy idle, no false trip", norm({"faults": [], "drift_mv": 0}), "PASS")]
    for x in (extra or [])[:3]:
        sc = norm(x)
        if sc["faults"] and sc["drift_mv"] < 300:
            continue          # faults without over-voltage: a fail-safe trip is allowed, nothing to require
        plan.append((f"judge's check: {x.get('why', '')[:70]}", sc, "PASS"))
    return plan


def verify(s: Session, plan: list[tuple[str, dict, str]]) -> tuple[bool, list[dict]]:
    results, ok = [], True
    for label, sc, want in plan:
        say("bench", f"verify: {label}  {short(sc, 100)}")
        r = s.run_scenario(sc["faults"], sc["drift_mv"], sc["charge_a"], sc["poll_ms"], f"verification: {label}",
                           timing=sc["timing"])
        passed = r["result"] == want
        results.append({**r, "check": label, "required": want})
        say("bench", f"   -> {verdict_text(r)}" + ("" if passed else f"   {C['bold']}<- required {want}{C['end']}"))
        ok &= passed
    return ok, results


def fix_round(s: Session, fix_b, judge_b, verdict: dict, rnd: int, attempts: int) -> dict | None:
    feedback = ""
    for attempt in range(1, attempts + 1):
        patch = write_patch(s, fix_b, verdict, feedback, rnd, attempt)
        if not patch:
            feedback = "No patch was submitted."
            continue
        problem = check_patch(patch["patched_source"])
        if problem:
            feedback = f"Static check failed: {problem}"
            say("fixer", f"patch rejected by static check: {problem}")
            continue
        diff = "".join(difflib.unified_diff(s.source.splitlines(True), patch["patched_source"].splitlines(True),
                                            "pi/controller.py", "pi/controller.py (patched)"))
        s.save(f"round{rnd}_patch_{attempt}.diff", diff)
        say("fixer", f"patch: {patch['explanation']}")
        print(C["dim"] + diff + C["end"])

        review = review_patch(s, judge_b, verdict, patch, diff, rnd, attempt)
        if not review.get("approve"):
            feedback = "The judge rejected your patch in review:\n- " + "\n- ".join(review.get("problems") or [])
            continue

        say("bench", "deploying the approved patch to the Pi")
        s.pi.deploy(patch["patched_source"])
        s.pi.start()
        ok, results = verify(s, verification_plan(s, verdict, review.get("extra_checks") or []))
        s.save(f"round{rnd}_verification_{attempt}.json", results)
        if ok:
            s.source = patch["patched_source"]
            s.save(f"round{rnd}_controller.py", s.source)
            return {"patch": patch, "review": review, "verification": results, "attempts": attempt}
        failed = [{"check": r["check"], "result": r["result"], "failure": r.get("failure"),
                   "detail": r.get("detail"), "scenario": r.get("scenario"),
                   "controller_log": r.get("controller_log", [])[-8:]} for r in results if r["result"] != r["required"]]
        feedback = "Hardware verification failed after deployment:\n" + json.dumps(failed, indent=2, default=str)[:5000]
        say("fixer", "verification failed, the results go back to the fixer")
        s.pi.deploy(s.source)                      # roll back to the last good controller
        s.pi.start()
    return None


# ----------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="GridTwin-HIL agent campaign")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--reset", action="store_true", help="deploy the original controller and exit")
    ap.add_argument("--scenario", choices=["healthy", "idle", "stale", "delay", "drop", "crc"],
                    help="run one hand-written scenario without AI")
    ap.add_argument("--poll-ms", type=int, default=DEFAULT_POLL_MS, help="with --scenario")
    ap.add_argument("--timing", choices=["random", "worst_case"], default="random", help="with --scenario")
    ap.add_argument("--attack-budget", type=int, default=6)
    ap.add_argument("--judge-budget", type=int, default=4)
    ap.add_argument("--fix-attempts", type=int, default=3)
    args = ap.parse_args()

    port, host = os.environ.get("NUCLEO_PORT", "COM5"), os.environ.get("PI_SSH", "")
    if not host:
        sys.exit("Set PI_SSH in .env (e.g. gridpi)")
    run_dir = REPO / "runs" / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    pi = PiController(host, poll_ms=DEFAULT_POLL_MS)
    pi.check()
    bench = Bench(port)
    bench.fresh_telemetry()
    s = Session(bench, pi, run_dir)
    say("bench", f"Nucleo on {port}, Pi at {host}, run saved to {run_dir.relative_to(REPO)}")

    try:
        pi.deploy(s.original)                      # always start from the original controller
        pi.start()
        if args.reset:
            say("bench", "original controller deployed")
            return 0
        if args.scenario:
            faults = {"healthy": [], "idle": [], "stale": [{"name": "stale", "value": "on"}],
                      "delay": [{"name": "delay", "value": 300}], "drop": [{"name": "drop", "value": 60}],
                      "crc": [{"name": "crc", "value": 60}]}[args.scenario]
            drift = 0 if args.scenario == "idle" else 400
            print(json.dumps(s.run_scenario(faults, drift, 10, args.poll_ms, f"manual: {args.scenario}",
                                            timing=args.timing), indent=2))
            return 0

        attacker_b, judge_b = backend_from_env(), backend_from_env("JUDGE")
        t_start = time.monotonic()
        for rnd in range(1, args.rounds + 1):
            print()
            say("round", f"{C['bold']}ROUND {rnd}{C['end']}" + (f"  ({len(s.history)} fix(es) deployed)" if s.history else ""))
            finding = attack(s, attacker_b, args.attack_budget, rnd)
            if not finding or not finding.get("found_failure"):
                say("round", "the attacker found no new failure within its budget: campaign complete")
                break
            verdict = investigate(s, judge_b, finding, args.judge_budget, rnd)
            if not verdict or not verdict.get("reproduced"):
                say("round", "the judge could not reproduce the finding on hardware: not fixing an unproven failure")
                s.history.append({"round": rnd, "root_cause": "(not reproduced) " + finding.get("summary", ""),
                                  "scenario": norm(finding.get("scenario")), "fix": "none", "fixed": False})
                continue
            fixed = fix_round(s, attacker_b, judge_b, verdict, rnd, args.fix_attempts)
            s.history.append({"round": rnd, "rule": verdict.get("rule_broken"), "root_cause": verdict["root_cause"],
                              "scenario": norm(verdict.get("minimal_scenario")),
                              "fix": fixed["patch"]["explanation"] if fixed else "none",
                              "fixed": bool(fixed), "fix_attempts": fixed["attempts"] if fixed else None})
            s.save("campaign.json", s.history)
            if not fixed:
                say("round", f"no verified fix after {args.fix_attempts} attempts: stopping")
                break
            say("round", f"{C['bold']}ROUND {rnd} FIXED{C['end']} and verified on hardware "
                         f"(attempt {fixed['attempts']})")

        print()
        say("round", f"{C['bold']}CAMPAIGN SUMMARY{C['end']}  ({(time.monotonic() - t_start) / 60:.1f} min, "
                     f"{len(s.scenarios)} hardware runs)")
        for h in s.history:
            mark = "FIXED" if h["fixed"] else "OPEN "
            say("round", f"  round {h['round']} [{mark}] {short(h['root_cause'], 150)}")
        s.save("campaign.json", s.history)
        s.save("controller_final.py", s.source)
        return 0 if s.history and all(h["fixed"] for h in s.history) else 1
    except BenchError as e:
        say("bench", f"error: {e}")
        return 2
    finally:
        pi.stop()
        bench.close()


if __name__ == "__main__":
    sys.exit(main())
