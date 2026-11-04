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
                                "With heavier faults (stale data, delays, bursts, > 5 % loss) and no over-voltage, a fail-safe "
                                "trip is acceptable.",
    },
    "controller_deployment": "The controller runs with --poll-ms between 50 and 500 ms depending on the site; "
                             "the default is 200 ms. Both safety rules must hold across that whole range.",
    "how_a_scenario_runs": "baseline (no faults, all cells ~3.30 V, controller restarted if poll_ms changed) -> faults applied "
                           "-> after 1 s, cell 4 is pushed up by drift_mv -> the STM32 reports PASS (trip within deadline), "
                           "FAIL (late trip) or FAIL no_trip (no trip within 3 s) -> faults cleared, back to baseline. "
                           "drift_mv >= 400 causes an over-voltage; drift_mv <= 250 does not, which tests rules 2 and 2b "
                           "(PASS = no trip, FAIL false_trip / nuisance_trip). Runs without an over-voltage watch the "
                           "controller for 5 s (10 s with light noise).",
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
        """All STM32 events for up to `window` s; stops early on a trip verdict."""
        out, end = [], time.monotonic() + window
        while time.monotonic() < end:
            try:
                ev = self.events.get(timeout=max(0.01, end - time.monotonic()))
            except queue.Empty:
                break
            out.append(ev)
            if ev.get("event") in ("trip", "no_trip"):
                break
        return out

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
                return {"scenario": scenario, "result": "ABORTED",
                        "reason": "cells did not return below the limit before the run"}
            # cells are fine and there are no faults, yet the trip line stays high
            return {"scenario": scenario, "result": "FAIL", "failure": "false_trip",
                    "detail": "with a healthy battery and no faults, the controller holds its trip line high at idle",
                    "controller_log": (controller.lines_since(log_mark) if controller else [])[-25:]}
        start = self.fresh_telemetry()
        self.drain_events()

        for f in faults:
            self.command(f"fault {f['name']} {f['value']}")
        time.sleep(1.0)

        t0 = time.monotonic()
        window = observe_s if drift_mv >= 300 else max(observe_s, 10.0 if is_light_noise(faults) else 5.0)
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
        recovered = self.wait_idle()

        names = [e["event"] for e in events]
        verdict = next((e for e in events if e["event"] in ("trip", "no_trip")), None)
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
    _proc: subprocess.Popen | None = None
    _lines: list = field(default_factory=list)

    def _ssh(self, remote_cmd: str, **kw) -> subprocess.CompletedProcess:
        return subprocess.run(["ssh", "-o", "BatchMode=yes", self.host, remote_cmd],
                              capture_output=True, text=True, timeout=30, **kw)

    def check(self) -> None:
        r = self._ssh("echo ok")
        if r.returncode != 0 or "ok" not in r.stdout:
            raise BenchError(f"cannot SSH to {self.host} without a password: {r.stderr.strip()}")

    def deploy(self, source: str) -> None:
        """Copy controller source to the Pi as the live controller."""
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", self.host,
                            f"cat > {self.remote_dir}/{self.script}"],
                           input=source, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise BenchError(f"deploy failed: {r.stderr.strip()}")

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self._ssh("pkill -f '[p]i/controller' || true")   # never two controllers on one port
        self._proc = None
        time.sleep(0.5)

    def start(self) -> None:
        self.stop()
        cmd = (f"cd {self.remote_dir} && exec .venv/bin/python -u {self.script} "
               f"--poll-ms {self.poll_ms}")
        self._proc = subprocess.Popen(["ssh", "-o", "BatchMode=yes", self.host, cmd],
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL, text=True, bufsize=1)
        threading.Thread(target=self._pump, daemon=True).start()
        time.sleep(1.5)
        if self._proc.poll() is not None:
            raise BenchError("controller exited at start:\n" + "\n".join(self._lines[-10:]))

    def _pump(self) -> None:
        proc = self._proc
        for line in proc.stdout:
            self._lines.append(line.rstrip())

    def mark(self) -> int:
        return len(self._lines)

    def lines_since(self, mark: int) -> list[str]:
        return self._lines[mark:]

    def running(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)
