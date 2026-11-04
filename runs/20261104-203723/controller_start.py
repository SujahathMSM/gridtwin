#!/usr/bin/env python3
"""GridTwin-HIL day 1: the energy controller under test (runs on the Raspberry Pi 5).

It polls the battery that the Nucleo emulates over Modbus RTU and pulls the
safety-trip line (GPIO23) high as soon as any cell reaches the over-voltage
limit. The Nucleo times how long that took.

Usage on the Pi:
    python3 controller.py                       # defaults: /dev/ttyAMA0, 9600 baud
    python3 controller.py --poll-ms 100         # poll faster -> lower trip latency
    python3 controller.py --no-gpio --port /dev/ttyUSB0   # run without GPIO (testing)
"""
from __future__ import annotations

import argparse
import sys
import time

import serial  # pip install pyserial

SLAVE_ADDR = 1
NUM_REGS = 12
REG_NAMES = [
    "cell1_mv", "cell2_mv", "cell3_mv", "cell4_mv", "pack_10mv", "current_da",
    "temp_dc", "soc_pm", "grid_v", "status", "tick", "charge_limit_da",
]

# Freshness fail-safe: tick (reg 10) increments every 100ms.
# If tick is frozen / no fresh reply for longer than this, we cannot trust
# cells -> trip. Debounced wall-time only (no single-miss trip) so light
# noise (drop/crc <=5% -> isolated misses, gap ~period+timeout ~100-190ms)
# does not trip, while persistent loss with fast retry reaches timeout in
# ~350-420ms and still meets 500ms OV deadline.
# 0.40s: > max triple-miss gap at capped period (120+3*70=330ms) so 1-3
# consecutive misses never trip; needs 4-5 consecutive (prob ~1e-4 at 10%
# combined loss) -> no nuisance in 10s; yet detection latency
# (Pc + k*70) stays <500ms for Pc<=120.
STALE_TIMEOUT_S = 0.40

# Safety deadline cap: worst-case OV right after a reply is invisible until
# the next read completes, so latency ~= effective period + read overhead
# (~33ms). With --poll-ms=500 this exceeds the 500ms deadline, so the
# internal safety period is capped. Capped to 120ms (not 400ms) so that
# debounced fail-safe (needing 4-5 fast retries x70ms) still trips <500ms:
# e.g. 120+5*70=470 worst, typically ~350-420ms. Faster polling does not
# cause false trips: staleness is wall-time based (STALE_TIMEOUT_S).
MAX_SAFETY_POLL_MS = 120

# Bound blocking/retry overhead for burst loss: pyserial timeout must be
# small so single-miss gap (period + timeout) stays <500ms even at capped
# period. 50ms still covers healthy 29-byte frame (~30ms @9600) with margin.
SERIAL_TIMEOUT_S = 0.05
# Retry delay on error: retry immediately without full-period sleep so
# persistent loss is re-probed in ~timeout+retry (~70ms), i.e. >=5 attempts
# per 500ms even at max poll.
ERROR_RETRY_S = 0.02


class ModbusError(Exception):
    pass


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def with_crc(body: bytes) -> bytes:
    c = crc16(body)
    return body + bytes([c & 0xFF, c >> 8])


def build_read(addr: int, start: int, count: int) -> bytes:
    return with_crc(bytes([addr, 0x03, start >> 8, start & 0xFF, count >> 8, count & 0xFF]))


def build_write(addr: int, reg: int, value: int) -> bytes:
    return with_crc(bytes([addr, 0x06, reg >> 8, reg & 0xFF, value >> 8, value & 0xFF]))


def check_crc(frame: bytes) -> None:
    if len(frame) < 4 or crc16(frame[:-2]) != frame[-2] | (frame[-1] << 8):
        raise ModbusError(f"bad CRC in {frame.hex(' ')}")


def parse_read_response(addr: int, count: int, frame: bytes) -> list[int]:
    if len(frame) == 5 and frame[1] == 0x83:
        check_crc(frame)
        raise ModbusError(f"exception code {frame[2]}")
    expected = 5 + 2 * count
    if len(frame) != expected:
        raise ModbusError(f"expected {expected} bytes, got {len(frame)}: {frame.hex(' ')}")
    check_crc(frame)
    if frame[0] != addr or frame[1] != 0x03 or frame[2] != 2 * count:
        raise ModbusError(f"unexpected header {frame[:3].hex(' ')}")
    return [(frame[3 + 2 * i] << 8) | frame[4 + 2 * i] for i in range(count)]


def read_registers(ser: serial.Serial, addr: int, start: int, count: int) -> list[int]:
    ser.reset_input_buffer()
    ser.write(build_read(addr, start, count))
    expected = 5 + 2 * count
    frame = ser.read(expected)            # pyserial stops at timeout if short
    if len(frame) >= 2 and frame[1] == 0x83 and len(frame) >= 5:
        frame = frame[:5]
    if not frame:
        raise ModbusError("no reply (timeout)")
    return parse_read_response(addr, count, frame)


def to_signed(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


class TripLine:
    """GPIO23 safety-trip output. Falls back to a dummy when --no-gpio is set."""

    def __init__(self, pin: int, enabled: bool):
        self._dev = None
        if enabled:
            from gpiozero import OutputDevice  # imported here so --no-gpio works anywhere
            self._dev = OutputDevice(pin, active_high=True, initial_value=False)
        self.active = False

    def set(self, on: bool) -> None:
        self.active = on
        if self._dev is not None:
            self._dev.on() if on else self._dev.off()


def main() -> int:
    ap = argparse.ArgumentParser(description="GridTwin day-1 controller under test")
    ap.add_argument("--port", default="/dev/ttyAMA0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--poll-ms", type=int, default=200, help="poll period in ms")
    ap.add_argument("--limit-mv", type=int, default=3650, help="cell over-voltage limit")
    ap.add_argument("--rearm-mv", type=int, default=3550,
                    help="release the trip when every cell is below this")
    ap.add_argument("--trip-pin", type=int, default=23, help="BCM GPIO number for the trip line")
    ap.add_argument("--no-gpio", action="store_true", help="don't touch GPIO (for testing)")
    args = ap.parse_args()

    # Effective safety poll period: honor --poll-ms when fast, but cap at
    # MAX_SAFETY_POLL_MS so the 500ms hardware deadline holds over the full
    # deployment range (50-500ms) even with worst-case after_reply phasing,
    # ~33ms Modbus overhead, and debounced (4-5 miss) fail-safe.
    # Faster polling does not cause false trips:
    # staleness is wall-time based (STALE_TIMEOUT_S).
    safety_poll_ms = min(args.poll_ms, MAX_SAFETY_POLL_MS)
    safety_poll_s = safety_poll_ms / 1000.0

    trip = TripLine(args.trip_pin, enabled=not args.no_gpio)
    ser = serial.Serial(args.port, args.baud, bytesize=8, parity="N", stopbits=1,
                        timeout=SERIAL_TIMEOUT_S)
    print(f"controller: polling {args.port} @ {args.baud} every {args.poll_ms} ms, "
          f"trip at {args.limit_mv} mV, re-arm below {args.rearm_mv} mV", flush=True)
    if safety_poll_ms != args.poll_ms:
        print(f"controller: safety poll capped to {safety_poll_ms} ms "
              f"(requested {args.poll_ms} ms) to meet 500ms deadline", flush=True)

    errors = 0
    last_print = 0.0
    last_tick = None
    last_fresh = time.monotonic()
    try:
        while True:
            t0 = time.monotonic()
            try:
                regs = read_registers(ser, SLAVE_ADDR, 0, NUM_REGS)
                errors = 0
            except (ModbusError, serial.SerialException, OSError) as e:
                errors += 1
                print(f"modbus error #{errors}: {e}", flush=True)
                now_err = time.monotonic()
                # Debounced fail-safe: trip only on persistent loss, i.e.
                # wall-time since last fresh tick exceeds STALE_TIMEOUT_S.
                # Isolated single misses from light noise (drop/crc <=5%)
                # give gap ~period+timeout+retry (~120-190ms) < timeout so
                # no nuisance trip. Persistent loss (70% drop, delay,
                # burst, stale) is re-probed every ~70ms (timeout+retry),
                # so gap exceeds 0.40s after 4-5 misses (~350-420ms) and
                # still meets 500ms OV deadline. No errors>=1 trip.
                if not trip.active:
                    if (now_err - last_fresh) > STALE_TIMEOUT_S:
                        trip.set(True)
                        print(f"TRIP: stale/no data for {now_err - last_fresh:.2f}s (errors={errors})", flush=True)
                # Retry immediately without full-period sleep to bound overhead:
                # each retry costs only SERIAL_TIMEOUT_S + ERROR_RETRY_S.
                time.sleep(ERROR_RETRY_S)
                continue

            tick = regs[10]
            now = time.monotonic()
            if last_tick is None:
                last_tick = tick
                last_fresh = now
                stale = False
            elif tick != last_tick:
                # Any change proves freshness (allow jumps >1, 16-bit wrap via !=).
                last_tick = tick
                last_fresh = now
                stale = False
            else:
                stale = (now - last_fresh) > STALE_TIMEOUT_S

            cells = regs[0:4]
            vmax = max(cells)
            if not trip.active:
                if vmax >= args.limit_mv:
                    trip.set(True)
                    print(f"TRIP: cell {cells.index(vmax) + 1} at {vmax} mV "
                          f"(limit {args.limit_mv})", flush=True)
                elif stale:
                    trip.set(True)
                    print(f"TRIP: stale tick frozen at {tick} for {now - last_fresh:.2f}s", flush=True)
            else:
                # Latched: only release on fresh data showing all cells < rearm.
                if not stale and vmax < args.rearm_mv:
                    trip.set(False)
                    print(f"released: max cell {vmax} mV", flush=True)

            now2 = time.monotonic()
            if now2 - last_print >= 1.0:
                last_print = now2
                print(f"cells={cells} mV  I={to_signed(regs[5]) / 10:.1f} A  "
                      f"T={to_signed(regs[6]) / 10:.1f} C  SOC={regs[7] / 10:.1f}%  "
                      f"tick={regs[10]}  trip={'ON' if trip.active else 'off'}", flush=True)

            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, safety_poll_s - elapsed))
    except KeyboardInterrupt:
        pass
    finally:
        trip.set(False)
        ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
