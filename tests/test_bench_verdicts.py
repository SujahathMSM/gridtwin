"""The referee: how bench.run_scenario turns the STM32's events into PASS / FAIL.
A fake Nucleo stands in for the serial port, so no hardware is needed."""
import json
import queue

import pytest

import bench as B


class FakeNucleo:
    """Answers commands like the firmware and plays a scripted list of events
    after the first `drift` command of a run."""

    def __init__(self, script=None, idle_trip=0):
        self.out = queue.Queue()
        self.script = script or (lambda drift, faults: [])
        self.drift, self.faults, self.trip, self.idle_trip = 0, {}, idle_trip, idle_trip
        self.polls, self.scripted = 0, False
        self.commands = []

    # pyserial surface used by Bench
    def readline(self):
        try:
            return self.out.get(timeout=0.02)
        except queue.Empty:
            return b""

    def write(self, data):
        for line in data.decode().splitlines():
            if line.strip():
                self.handle(line.strip())

    def close(self):
        pass

    def emit(self, obj):
        self.out.put((json.dumps(obj) + "\r\n").encode())

    def handle(self, line):
        self.commands.append(line)
        p = line.split()
        if p[0] == "status":
            self.polls += 10
            cell4 = 3309 + (self.drift if self.drift <= 250 else 400)
            self.emit({"cells": [3300, 3303, 3306, cell4], "limit_mv": 3650, "trip": self.trip,
                       "polls": self.polls, "dropped": 0, "corrupted": 0})
            return
        if p[0] == "drift":
            n = int(p[-1])
            self.emit({"ok": line})
            self.drift = n
            if not self.scripted:
                self.scripted = True
                for ev in self.script(n, dict(self.faults)):
                    if ev.get("event") in ("spurious_trip",):
                        self.trip = 1
                    self.emit(ev)
            if n == 0:
                self.trip = self.idle_trip
            return
        if p[0] == "fault":
            if p[1] == "clear":
                self.faults = {}
            else:
                self.faults[p[1]] = p[2]
        if p[0] == "baseline":
            self.faults, self.drift = {}, 0
        self.emit({"ok": line})


@pytest.fixture
def make_bench(monkeypatch):
    monkeypatch.setattr(B, "SETTLE_S", 0.0)
    monkeypatch.setattr(B, "PREMATURE_WATCH_S", 0.3)
    monkeypatch.setattr(B, "NO_OV_WINDOW_S", 0.3)
    monkeypatch.setattr(B, "NOISE_WINDOW_S", 0.3)
    benches = []

    def _make(fake):
        monkeypatch.setattr(B.serial, "Serial", lambda *a, **k: fake)
        b = B.Bench("FAKE")
        benches.append(b)
        return b

    yield _make
    for b in benches:
        b.close()


OV = {"event": "overvoltage", "cell_mv": 3694, "limit_mv": 3650}


def trip(lat_us, result="PASS", **kw):
    return {"event": "trip", "latency_us": lat_us, "until_reported_us": lat_us - 34000,
            "reaction_us": 34000, "deadline_ms": 500, "result": result, **kw}


def test_trip_in_time_passes(make_bench):
    r = make_bench(FakeNucleo(lambda d, f: [OV, trip(203400)])).run_scenario([], 400, observe_s=1)
    assert r["result"] == "PASS" and r["latency_ms"] == 203.4


def test_late_trip_fails(make_bench):
    r = make_bench(FakeNucleo(lambda d, f: [OV, trip(503400, "FAIL")])).run_scenario([], 400, observe_s=1)
    assert r["result"] == "FAIL" and r["latency_ms"] == 503.4


def test_no_trip_fails(make_bench):
    r = make_bench(FakeNucleo(lambda d, f: [OV, {"event": "no_trip", "result": "FAIL", "waited_ms": 3000}])
                   ).run_scenario([{"name": "stale", "value": "on"}], 400, observe_s=1)
    assert r["result"] == "FAIL" and r["failure"] == "no_trip"


def test_premature_release_overrides_a_good_trip(make_bench):
    events = [OV, trip(120000), {"event": "premature_release", "result": "FAIL", "cell_mv": 3694, "held_ms": 300}]
    r = make_bench(FakeNucleo(lambda d, f: events)).run_scenario([], 400, observe_s=1)
    assert r["result"] == "FAIL" and r["failure"] == "premature_release"


def test_watchdog_trip_reports_its_cause(make_bench):
    r = make_bench(FakeNucleo(lambda d, f: [OV, trip(180000, cause="heartbeat_lost")])).run_scenario([], 400, observe_s=1)
    assert r["result"] == "PASS" and r["trip_cause"] == "heartbeat_lost"


def test_healthy_idle_passes_and_false_trip_fails(make_bench):
    assert make_bench(FakeNucleo()).run_scenario([], 0)["result"] == "PASS"
    r = make_bench(FakeNucleo(lambda d, f: [{"event": "spurious_trip"}])).run_scenario([], 0)
    assert r["result"] == "FAIL" and r["failure"] == "false_trip"


def test_light_noise_trip_is_a_nuisance_trip_but_heavy_faults_may_trip(make_bench):
    spurious = lambda d, f: [{"event": "spurious_trip"}]
    r = make_bench(FakeNucleo(spurious)).run_scenario([{"name": "drop", "value": 5}], 0)
    assert r["result"] == "FAIL" and r["failure"] == "nuisance_trip"
    r = make_bench(FakeNucleo(spurious)).run_scenario([{"name": "drop", "value": 30}], 0)
    assert r["result"] == "NO_OVERVOLTAGE"


def test_ambiguous_drift_refused(make_bench):
    with pytest.raises(B.BenchError):
        make_bench(FakeNucleo()).run_scenario([], 300)


def test_stuck_interlock_at_idle_without_a_controller_is_a_false_trip(make_bench, monkeypatch):
    monkeypatch.setattr(B.Bench, "wait_idle", lambda self, timeout=8.0: False)
    r = make_bench(FakeNucleo(idle_trip=1)).run_scenario([], 400)
    assert r["result"] == "FAIL" and r["failure"] == "false_trip"


def test_stuck_interlock_cleared_by_restart_is_a_bench_fault(make_bench, monkeypatch):
    calls = {"n": 0}

    def wait_idle(self, timeout=8.0):
        calls["n"] += 1
        return calls["n"] > 1                   # clears after the restart

    class Ctl:
        poll_ms = 200
        def mark(self): return 0
        def lines_since(self, m): return []
        def start(self): pass

    monkeypatch.setattr(B.Bench, "wait_idle", wait_idle)
    r = make_bench(FakeNucleo(idle_trip=1)).run_scenario([], 400, controller=Ctl())
    assert r["result"] == "BENCH_FAULT"


def test_worst_case_timing_uses_drift_sync(make_bench):
    fake = FakeNucleo(lambda d, f: [{"event": "drift_applied", "drift_mv": 400, "timing": "after_reply"}, OV, trip(503400, "FAIL")])
    r = make_bench(fake).run_scenario([], 400, observe_s=1, timing="worst_case")
    assert "drift sync 400" in fake.commands and r["drift_applied"] == "after_reply" and r["result"] == "FAIL"


@pytest.mark.parametrize("faults,ok", [
    ([{"name": "drop", "value": "5"}], True),
    ([{"name": "delay", "value": 2001}], False),
    ([{"name": "burst", "value": 11}], False),
    ([{"name": "ctl_freeze", "value": 50}], False),
    ([{"name": "ctl_kill", "value": "on"}], True),
    ([{"name": "rm -rf", "value": 1}], False),
])
def test_validate_faults(faults, ok):
    if ok:
        assert B.validate_faults(faults)
    else:
        with pytest.raises(B.BenchError):
            B.validate_faults(faults)


def test_light_noise_definition():
    assert B.is_light_noise([{"name": "drop", "value": 5}, {"name": "crc", "value": 3}])
    assert not B.is_light_noise([{"name": "drop", "value": 6}])
    assert not B.is_light_noise([{"name": "burst", "value": 1}])
    assert not B.is_light_noise([])
