#!/usr/bin/env python3
"""GridTwin-HIL day 1: laptop monitor for the Nucleo (ST-Link virtual COM port).

Shows the emulated battery once per second, highlights trip events with their
measured latency, and forwards anything you type to the Nucleo as a command.

    python monitor.py --port COM5            # Windows (check Device Manager)
    python monitor.py --port /dev/ttyACM0    # Linux

Commands (type and press Enter):
    status | i <amps> | drift <mV> | drift pot | limit <mV> | deadline <ms>
    soc <pct> | grid <V> | help | quit
"""
from __future__ import annotations

import argparse
import json
import threading
import time

import serial  # pip install pyserial

RESULTS: list[dict] = []


def show(msg: dict, raw: bool) -> None:
    if raw:
        print(json.dumps(msg))
        return
    ev = msg.get("event")
    if ev == "trip":
        RESULTS.append(msg)
        print(f"\n*** TRIP  {msg['result']}  total {msg['latency_us'] / 1000:.1f} ms  "
              f"(until reported {msg['until_reported_us'] / 1000:.1f} ms, "
              f"Pi reaction {msg['reaction_us'] / 1000:.1f} ms, "
              f"deadline {msg['deadline_ms']} ms)\n")
    elif ev == "no_trip":
        print(f"\n*** FAIL  no trip within {msg['waited_ms']} ms of the over-voltage\n")
    elif ev == "overvoltage":
        print(f"--> over-voltage: {msg['cell_mv']} mV >= {msg['limit_mv']} mV, timer started")
    elif ev:
        print(f"[{ev}] {msg}")
    elif "ok" in msg:
        print(f"ok: {msg['ok']}")
    elif "error" in msg:
        print(f"error: {msg['error']}")
    elif "help" in msg:
        print(f"commands: {msg['help']}")
    elif "cells" in msg:
        c = msg["cells"]
        print(f"cells {c[0]} {c[1]} {c[2]} {c[3]} mV | I {msg['i_da'] / 10:.1f} A | "
              f"T {msg['temp_dc'] / 10:.1f} C | SOC {msg['soc_pm'] / 10:.1f}% | "
              f"drift {msg['drift_mv']} mV | trip {'ON ' if msg['trip'] else 'off'} | "
              f"polls {msg['polls']}"
              + (f" | link err: noise {msg['noise_err']} frame {msg['frame_err']} crc {msg['crc_err']}"
                 + (f" short {msg['short_err']}" if "short_err" in msg else "")
                 if "noise_err" in msg else ""))
    else:
        print(msg)


def reader(ser: serial.Serial, raw: bool, every: float,
           force: threading.Event, stop: threading.Event) -> None:
    last_tele = 0.0
    while not stop.is_set():
        try:
            line = ser.readline().decode(errors="replace").strip()
        except (serial.SerialException, OSError, TypeError):
            return                                # port closed (quit) or unplugged
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(f"(raw) {line}")
            continue
        if "cells" in msg and not raw:
            # Status lines only every few seconds (or right after 'status'),
            # so they don't scramble what you're typing. Events always print.
            now = time.monotonic()
            if not force.is_set() and now - last_tele < every:
                continue
            force.clear()
            last_tele = now
        show(msg, raw)


def main() -> None:
    ap = argparse.ArgumentParser(description="GridTwin day-1 Nucleo monitor")
    ap.add_argument("--port", required=True, help="e.g. COM5 or /dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--raw", action="store_true", help="print every JSON line as-is")
    ap.add_argument("--every", type=float, default=5.0,
                    help="seconds between status lines (default 5)")
    args = ap.parse_args()

    ser = serial.Serial(args.port, args.baud, timeout=0.5)
    stop = threading.Event()
    force = threading.Event()
    force.set()                                   # show one status line right away
    threading.Thread(target=reader, args=(ser, args.raw, args.every, force, stop),
                     daemon=True).start()
    print(f"connected. status every {args.every:g} s; type 'help' for commands, 'quit' to exit.")
    try:
        while True:
            cmd = input().strip()
            if cmd in ("quit", "exit"):
                break
            if cmd:
                if cmd == "status":
                    force.set()
                ser.write((cmd + "\n").encode())
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        stop.set()
        ser.close()
        if RESULTS:
            lat = [r["latency_us"] / 1000 for r in RESULTS]
            print(f"\n{len(lat)} trips: min {min(lat):.1f} ms, max {max(lat):.1f} ms, "
                  f"avg {sum(lat) / len(lat):.1f} ms, "
                  f"{sum(r['result'] == 'PASS' for r in RESULTS)} passed")


if __name__ == "__main__":
    main()
