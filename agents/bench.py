"""The test bench, as the agents see it.

Bench       talks to the Nucleo (battery emulator + judge) over its USB serial port.
PiController starts, stops, logs and redeploys the controller under test on the Pi over SSH.

Agents never touch the hardware directly: they call run_scenario(), which applies a
fixed, bounded set of faults and returns the Nucleo's measurement.
"""
from __future__ import annotations

import json
import queue
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import serial  # pyserial

REPO = Path(__file__).resolve().parents[1]

# What the agents are allowed to inject. Ranges are enforced here AND in firmware.
FAULTS = {
    "stale": {"values": ["on", "off"], "doc": "BMS keeps answering with frozen values (cells, current, temperature, SOC, tick) while the real battery keeps changing"},
    "delay": {"min": 0, "max": 2000, "unit": "ms", "doc": "every Modbus reply is sent this many ms late"},
    "drop":  {"min": 0, "max": 100, "unit": "%", "doc": "this percentage of Modbus requests get no reply"},
    "crc":   {"min": 0, "max": 100, "unit": "%", "doc": "this percentage of Modbus replies carry a corrupted CRC"},
    "burst": {"min": 0, "max": 10, "unit": "replies", "doc": "exactly this many read replies are lost right after cell 4 is pushed "
              "up (an interference burst). Deterministic: combined with timing=worst_case it reproduces every time"},
    "cut":   {"values": ["on"], "doc": "the trip wire is cut: the battery side can no longer see the controller's trip line"},
    "ctl_kill": {"values": ["on"], "doc": "the controller process is killed (SIGKILL) 0.5 s before cell 4 is pushed up"},
    "ctl_freeze": {"min": 100, "max": 3000, "unit": "ms", "doc": "the controller process is frozen (SIGSTOP) for this long, "
                   "starting 0.3 s before cell 4 is pushed up, then resumed"},
}
FIRMWARE_FAULTS = ("stale", "delay", "drop", "crc", "burst", "cut")
CONTROLLER_FAULTS = ("ctl_kill", "ctl_freeze")
INTERLOCKS = {
    "legacy": "GPIO23 (trip line) HIGH = trip, LOW = battery allowed. The battery side only reacts to a high line.",
    "failsafe": "energize to run. GPIO23 HIGH = permissive (battery allowed); LOW, a cut wire or an unpowered Pi = trip. "
                "GPIO24 is a heartbeat: it must change level at least every 250 ms or the interlock opens by itself, "
                "so a hung or killed controller trips. A deliberate trip = drive GPIO23 low.",
}

# Occasional noise a real RS-485 line always has. Rule 2b: no trip for this alone.
LIGHT_NOISE_MAX_PCT = 5


def is_light_noise(faults: list[dict]) -> bool:
    return bool(faults) and all(f["name"] in ("drop", "crc") and int(f["value"]) <= LIGHT_NOISE_MAX_PCT for f in faults)

BENCH_INFO = {
    "battery": "4-cell LiFePO4 pack, 100 Ah, emulated by an STM32 (physics at 60x real time)",
    "link": "Modbus RTU, slave address 1, 9600 baud 8N1, over UART",
    "register_map": {
        "0-3": "cell 1-4 voltage, mV", "4": "pack voltage, 10 mV", "5": "pack current, 0.1 A signed (+ = charging)",
        "6": "temperature, 0.1 degC", "7": "state of charge, 0.1 %", "8": "grid voltage, V",
        "9": "status bits (bit0 trip line high)", "10": "simulation tick, +1 every 100 ms (a freshness counter)",
        "11": "charge current limit, 0.1 A, writable",
    },
    "safety_rules": {
        "1_trip_in_time": "When any cell reaches 3650 mV, the controller must raise its trip line (GPIO23) within 500 ms. "
                          "The STM32 timestamps the over-voltage and the trip edge in hardware (1 us resolution).",
        "2_no_false_trips": "On a healthy link with no over-voltage, the controller must never trip: a false trip shuts the site "
                            "down.",
        "2b_no_nuisance_trips": f"Real RS-485 lines lose or corrupt the odd reply. With light noise only (drop and/or crc, each "
                                f"<= {LIGHT_NOISE_MAX_PCT} %) and no over-voltage, the controller must not trip either. "
                                "With heavier faults (stale data, delays, bursts, > 5 % loss, controller faults) and no "
                                "over-voltage, a fail-safe trip is acceptable.",
        "3_fail_safe": "If the controller is killed or frozen, or the trip wire is cut, an over-voltage must still be "
                       "tripped within 500 ms. (Under the legacy interlock this cannot hold: the battery side only reacts "
                       "to a live controller driving the line high.)",
        "4_no_premature_release": "Once tripped, the interlock must stay open while any cell is at or above the limit. "
                                  "Releasing early is a FAIL even if the trip itself was on time.",
    },
    "interlock": INTERLOCKS,
    "controller_deployment": "The controller runs with --poll-ms between 50 and 500 ms depending on the site; "
                             "the default is 200 ms. Both safety rules must hold across that whole range.",
    "how_a_scenario_runs": "baseline (no faults, all cells ~3.30 V, controller restarted if poll_ms changed) -> faults applied "
                           "-> after 1 s, cell 4 is pushed up by drift_mv -> the STM32 reports PASS (trip within deadline), "
                           "FAIL (late trip) or FAIL no_trip (no trip within 3 s) -> faults cleared, back to baseline. "
                           "drift_mv >= 400 causes an over-voltage; drift_mv <= 250 does not, which tests rules 2 and 2b "
                           "(PASS = no trip, FAIL false_trip / nuisance_trip); 251-399 is refused as ambiguous. Runs "
                           "without an over-voltage watch the controller for 5 s (10 s with light noise). After a trip "
                           "the bench keeps watching 1.5 s for a premature release. BENCH_FAULT means the bench itself "
                           "misbehaved: it is not evidence about the controller.",
    "timing": {
        "random": "default. The drift is applied whenever the bench sends the command, so it lands at a random point "
                  "in the controller's poll cycle. Failures that depend on timing show up only some of the time.",
        "worst_case": "the STM32 applies the drift right after it finishes answering a read request: the controller has "
                      "just looked and cannot see the over-voltage before its next read. Makes timing races reproducible. "
                      "If no read reply goes out within 2 s, the drift is applied anyway (reported as drift_applied=timeout).",
    },
    "faults": FAULTS,
}


TIMINGS = ("random", "worst_case")
PREMATURE_WATCH_S = 1.5      # after a trip, watch this long for a premature release
NO_OV_WINDOW_S = 5.0         # runs without an over-voltage: watch for a false trip this long
NOISE_WINDOW_S = 10.0        # ... and this long under light noise (so some replies actually get lost)
SETTLE_S = 0.5               # pauses between applying faults and pushing the cell up


class BenchError(RuntimeError):
    pass


class Bench:
    """Serial link to the Nucleo. Reads its JSON lines in a background thread."""

    def __init__(self, port: str, baud: int = 115200, log=print):
        self.ser = serial.Serial(port, baud, timeout=0.2)
        self.log = log
        self.telemetry: dict = {}
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.replies: "queue.Queue[dict]" = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()

    # -- low level -------------------------------------------------------
    def _reader(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self.ser.readline()
            except (serial.SerialException, OSError, TypeError):
                return
            line = raw.decode(errors="replace").strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg["_t"] = time.monotonic()
            if "cells" in msg:
                self.telemetry = msg
            elif "event" in msg:
                self.events.put(msg)
            elif "ok" in msg or "error" in msg:
                self.replies.put(msg)

    def close(self) -> None:
        self._stop.set()
        try:
            self.ser.close()
        except Exception:
            pass

    def command(self, text: str, timeout: float = 2.0) -> dict:
        """Send one command and wait for its ok/error reply."""
        with self._lock:
            while not self.replies.empty():
                self.replies.get_nowait()
            self.ser.write((text + "\n").encode())
            try:
                reply = self.replies.get(timeout=timeout)
            except queue.Empty:
                raise BenchError(f"no reply from the Nucleo to '{text}'")
        if "error" in reply:
            raise BenchError(f"Nucleo refused '{text}': {reply['error']}")
        return reply

    def fresh_telemetry(self, timeout: float = 2.0) -> dict:
        before = self.telemetry.get("_t", 0)
        self.ser.write(b"status\n")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.telemetry.get("_t", 0) > before:
                return dict(self.telemetry)
            time.sleep(0.05)
        raise BenchError("no telemetry from the Nucleo (is the monitor closed? right COM port?)")

    def drain_events(self) -> list[dict]:
        out = []
        while not self.events.empty():
            out.append(self.events.get_nowait())
        return out

    def wait_event(self, names: set[str], timeout: float) -> dict | None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                ev = self.events.get(timeout=max(0.01, end - time.monotonic()))
            except queue.Empty:
                return None
            if ev.get("event") in names:
                return ev
        return None

    def _collect(self, window: float) -> list[dict]:
        """All STM32 events for up to `window` s. After a trip, keep watching
        PREMATURE_WATCH_S for a premature release, then stop."""
        out, end = [], time.monotonic() + window
        while time.monotonic() < end:
            try:
                ev = self.events.get(timeout=max(0.01, end - time.monotonic()))
            except queue.Empty:
                break
            out.append(ev)
            if ev.get("event") == "no_trip" or ev.get("event") == "premature_release":
                break
            if ev.get("event") == "trip":
                end = min(end, time.monotonic() + PREMATURE_WATCH_S) if ev.get("result") == "PASS" else time.monotonic()
        return out

    def set_interlock(self, mode: str) -> None:
        if mode not in INTERLOCKS:
            raise BenchError(f"interlock must be one of {list(INTERLOCKS)}")
        reply = self.command(f"interlock {mode}")
        if reply.get("ok") != f"interlock {mode}":
            raise BenchError("the Nucleo firmware does not support interlock modes: flash the latest firmware")

    def wait_idle(self, timeout: float = 8.0) -> bool:
        """Wait until all cells are below the limit and the trip line is low."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            t = self.fresh_telemetry()
            if max(t["cells"]) < t["limit_mv"] - 60 and not t["trip"]:
                return True
            time.sleep(0.4)
        return False

    # -- the one tool the agents get --------------------------------------
    def run_scenario(self, faults: list[dict], drift_mv: int = 400, charge_a: int = 10,
                     observe_s: float = 4.0, controller: "PiController | None" = None,
                     timing: str = "random") -> dict:
        faults = validate_faults(faults)
        try:                                  # models sometimes send numbers as strings
            drift_mv, charge_a = int(float(drift_mv)), int(float(charge_a))
        except (TypeError, ValueError):
            raise BenchError("drift_mv and charge_a must be numbers")
        drift_mv = max(0, min(800, drift_mv))
        charge_a = max(-100, min(100, charge_a))
        if 250 < drift_mv < 400:
            raise BenchError("drift_mv must be <= 250 (no over-voltage) or >= 400 (over-voltage)")
        ctl_faults = [f for f in faults if f["name"] in CONTROLLER_FAULTS]
        if ctl_faults and controller is None:
            raise BenchError("controller faults need the Pi controller")
        timing = str(timing or "random").lower()
        if timing not in TIMINGS:
            raise BenchError(f"timing must be one of {list(TIMINGS)}")

        scenario = {"faults": faults, "drift_mv": drift_mv, "charge_a": charge_a, "timing": timing}
        if controller:
            scenario["poll_ms"] = controller.poll_ms
        log_mark = controller.mark() if controller else 0

        self.command("baseline")
        self.command(f"i {charge_a}")
        if not self.wait_idle():
            t = self.fresh_telemetry()
            if max(t["cells"]) >= t["limit_mv"] - 60:
                return {"scenario": scenario, "result": "BENCH_FAULT",
                        "reason": "cells did not return below the limit before the run"}
            # Cells are fine and there are no faults, yet the interlock stays open. Restart the
            # controller once: if it clears, the bench was in a bad state (not evidence).
            if controller is not None:
                controller.start()
                if self.wait_idle():
                    return {"scenario": scenario, "result": "BENCH_FAULT",
                            "reason": "the interlock was open at idle until the controller was restarted; "
                                      "run the scenario again"}
            return {"scenario": scenario, "result": "FAIL", "failure": "false_trip",
                    "detail": "with a healthy battery and no faults, the controller keeps the interlock open at idle "
                              "(even after a restart)",
                    "controller_log": (controller.lines_since(log_mark) if controller else [])[-25:]}
        start = self.fresh_telemetry()
        self.drain_events()

        for f in faults:
            if f["name"] in FIRMWARE_FAULTS:
                self.command(f"fault {f['name']} {f['value']}")
        time.sleep(SETTLE_S)
        for f in ctl_faults:
            if f["name"] == "ctl_kill":
                controller.kill()
            else:
                controller.freeze(int(f["value"]), delay_s=0.2)
        time.sleep(SETTLE_S)

        t0 = time.monotonic()
        window = observe_s if drift_mv >= 400 else max(observe_s, NOISE_WINDOW_S if is_light_noise(faults)
                                                        else NO_OV_WINDOW_S)
        if timing == "worst_case":
            reply = self.command(f"drift sync {drift_mv}")
            if reply.get("ok") != f"drift sync {drift_mv}":     # old firmware reads "sync" as 0 mV
                self.command("drift 0")
                raise BenchError("the Nucleo firmware does not support worst_case timing: flash the latest firmware")
            window += 2.5                                        # the drift waits for the next reply (2 s max)
        else:
            self.command(f"drift {drift_mv}")
        events = self._collect(window)
        time.sleep(0.6)                     # let the controller log a little after the event
        end = self.fresh_telemetry()
        ctl_log = controller.lines_since(log_mark) if controller else []

        # back to normal
        self.command("fault clear")
        self.command("drift 0")
        if ctl_faults and controller is not None:
            controller.resume()
            if not controller.running():
                controller.start()             # it was killed: bring it back for the next run
        recovered = self.wait_idle()

        names = [e["event"] for e in events]
        verdict = next((e for e in events if e["event"] in ("trip", "no_trip")), None)
        premature = next((e for e in events if e["event"] == "premature_release"), None)
        overvoltage = "overvoltage" in names
        result: dict = {
            "scenario": scenario, "result": "INCONCLUSIVE", "max_cell_mv_real": max(end["cells"]),
            "limit_mv": end["limit_mv"], "deadline_ms": 500,
        }
        if timing == "worst_case":
            applied = next((e for e in events if e["event"] == "drift_applied"), None)
            result["drift_applied"] = applied.get("timing") if applied else "not_seen"
            if applied and applied.get("timing") == "timeout":
                result["timing_note"] = ("no read reply went out for 2 s, so the drift was applied on a timeout, "
                                         "not right after a reply")
        if verdict is None and not overvoltage:
            tripped = "spurious_trip" in names or bool(end["trip"])
            if not faults:                               # rule 2: healthy link, no over-voltage
                if tripped:
                    result.update(result="FAIL", failure="false_trip",
                                  detail="no over-voltage and a healthy link, but the controller tripped")
                else:
                    result.update(result="PASS", check="no false trip on a healthy link")
            elif is_light_noise(faults):                 # rule 2b: light noise, no over-voltage
                if tripped:
                    result.update(result="FAIL", failure="nuisance_trip",
                                  detail="only light noise and no over-voltage, but the controller tripped")
                else:
                    result.update(result="PASS", check="no nuisance trip under light noise")
            else:
                result.update(result="NO_OVERVOLTAGE",
                              observation=("controller tripped (fail-safe, acceptable with faults active)"
                                           if tripped else "controller did not trip"))
        elif verdict is None:
            result["reason"] = "no verdict from the STM32 within the observation window"
        elif verdict["event"] == "no_trip":
            result.update(result="FAIL", failure="no_trip",
                          detail=f"cell over the limit for {verdict['waited_ms']} ms and the controller never raised the trip line")
        else:
            result.update(result=verdict["result"], latency_ms=round(verdict["latency_us"] / 1000, 1),
                          until_reported_ms=round(verdict["until_reported_us"] / 1000, 1),
                          reaction_ms=round(verdict["reaction_us"] / 1000, 1))
            if verdict.get("note"):
                result["note"] = "controller was already tripped before the over-voltage (safe state)"
            if verdict.get("cause") and verdict["cause"] != "trip_line":
                result["trip_cause"] = verdict["cause"]   # heartbeat_lost / wire_cut: the interlock opened by itself
            if premature:
                result.update(result="FAIL", failure="premature_release",
                              detail=f"the interlock closed again after {premature.get('held_ms')} ms while a cell was "
                                     f"still at {premature.get('cell_mv')} mV (limit {result['limit_mv']} mV)")
        result["modbus_link"] = {
            "polls_answered": end["polls"] - start["polls"],
            "replies_dropped": end.get("dropped", 0) - start.get("dropped", 0),
            "replies_corrupted": end.get("corrupted", 0) - start.get("corrupted", 0),
        }
        result["controller_log"] = ctl_log[-25:]
        result["elapsed_s"] = round(time.monotonic() - t0, 1)
        if not recovered:
            result["warning"] = "bench did not recover to idle after the run"
        return result


def validate_faults(faults: list[dict]) -> list[dict]:
    out = []
    for f in faults or []:
        name = str(f.get("name", "")).strip()
        if name not in FAULTS:
            raise BenchError(f"unknown fault '{name}', allowed: {sorted(FAULTS)}")
        spec, val = FAULTS[name], f.get("value")
        if "values" in spec:
            val = str(val).lower()
            if val not in spec["values"]:
                raise BenchError(f"fault {name} takes {spec['values']}")
        else:
            try:
                val = int(float(val))
            except (TypeError, ValueError):
                raise BenchError(f"fault {name} needs a number {spec['min']}..{spec['max']}")
            if not spec["min"] <= val <= spec["max"]:
                raise BenchError(f"fault {name} must be {spec['min']}..{spec['max']} {spec['unit']}")
        out.append({"name": name, "value": val})
    return out


@dataclass
class PiController:
    """The controller under test, running on the Pi, driven over SSH."""
    host: str                                   # e.g. pi5test@pi5-1.local or an ssh config alias
    remote_dir: str = "~/gridtwin"
    script: str = "pi/controller_live.py"
    poll_ms: int = 200
    log: callable = print
    max_lines: int = 5000                       # keep memory bounded on long campaigns
    _proc: subprocess.Popen | None = None
    _lines: list = field(default_factory=list)
    _dropped: int = 0                           # lines trimmed from the front (keeps marks valid)

    PATTERN = "[p]i/controller"                 # matches controller.py and controller_live.py, not pkill itself

    def _ssh(self, remote_cmd: str, **kw) -> subprocess.CompletedProcess:
        return subprocess.run(["ssh", "-o", "BatchMode=yes", self.host, remote_cmd],
                              capture_output=True, text=True, timeout=30, **kw)

    def _dir(self) -> str:
        # "~/x" must stay unquoted for the shell to expand it; quote everything else
        d = self.remote_dir
        return "~/" + shlex.quote(d[2:]) if d.startswith("~/") else shlex.quote(d)

    def check(self) -> None:
        r = self._ssh("echo ok")
        if r.returncode != 0 or "ok" not in r.stdout:
            raise BenchError(f"cannot SSH to {self.host} without a password: {r.stderr.strip()}")

    def deploy(self, source: str) -> None:
        """Copy controller source to the Pi as the live controller, atomically
        (written to a temp file, then renamed), so a half-written file never runs."""
        target = f"{self._dir()}/{shlex.quote(self.script)}"
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", self.host,
                            f"cat > {target}.tmp && mv -f {target}.tmp {target}"],
                           input=source, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise BenchError(f"deploy failed: {r.stderr.strip()}")

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        # a frozen controller ignores TERM until resumed; never two controllers on one port
        self._ssh(f"pkill -CONT -f '{self.PATTERN}'; pkill -f '{self.PATTERN}' || true")
        self._proc = None
        time.sleep(0.5)

    def start(self) -> None:
        self.stop()
        cmd = (f"cd {self._dir()} && exec .venv/bin/python -u {shlex.quote(self.script)} "
               f"--poll-ms {int(self.poll_ms)}")
        proc = subprocess.Popen(["ssh", "-o", "BatchMode=yes", self.host, cmd],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1)
        self._proc = proc
        threading.Thread(target=self._pump, args=(proc,), daemon=True).start()
        time.sleep(1.5)
        if proc.poll() is not None:
            raise BenchError("controller exited at start:\n" + "\n".join(self._lines[-10:]))

    # -- controller faults (the agents' new attack class) ------------------
    def kill(self) -> None:
        """SIGKILL: no cleanup code runs, like a crash or a power cut of the process."""
        self._ssh(f"pkill -KILL -f '{self.PATTERN}' || true")

    def freeze(self, ms: int, delay_s: float = 0.0) -> None:
        """SIGSTOP now, SIGCONT after `ms` (done on the Pi, so laptop timing doesn't matter)."""
        if delay_s:
            time.sleep(delay_s)
        sec = max(0.1, min(3.0, ms / 1000))
        subprocess.Popen(["ssh", "-o", "BatchMode=yes", self.host,
                          f"pkill -STOP -f '{self.PATTERN}'; sleep {sec:.3f}; pkill -CONT -f '{self.PATTERN}'"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)

    def resume(self) -> None:
        self._ssh(f"pkill -CONT -f '{self.PATTERN}' || true")

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            if proc is not self._proc:          # a restart happened: late lines of the old process are dropped
                break
            self._lines.append(line.rstrip())
            if len(self._lines) > self.max_lines:
                cut = len(self._lines) - self.max_lines
                del self._lines[:cut]
                self._dropped += cut

    def mark(self) -> int:
        return self._dropped + len(self._lines)

    def lines_since(self, mark: int) -> list[str]:
        return self._lines[max(0, mark - self._dropped):]

    def running(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)
