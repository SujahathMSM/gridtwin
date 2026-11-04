"""The Modbus helpers in pi/controller.py (the controller under test)."""
import pytest

import controller as c


def reply(values, addr=1):
    body = bytes([addr, 0x03, 2 * len(values)]) + b"".join(v.to_bytes(2, "big") for v in values)
    return c.with_crc(body)


def test_crc_known_vector():
    # read 12 registers from slave 1: the request the controller sends every poll
    assert c.build_read(1, 0, 12) == bytes.fromhex("01 03 00 00 00 0C 45 CF")


def test_parse_good_reply():
    vals = list(range(3300, 3312))
    assert c.parse_read_response(1, 12, reply(vals)) == vals


def test_bad_crc_rejected():
    frame = bytearray(reply([3300] * 12))
    frame[-1] ^= 0x5A
    with pytest.raises(c.ModbusError):
        c.parse_read_response(1, 12, bytes(frame))


def test_short_and_wrong_header_rejected():
    with pytest.raises(c.ModbusError):
        c.parse_read_response(1, 12, reply([3300] * 11))
    with pytest.raises(c.ModbusError):
        c.parse_read_response(1, 12, reply([3300] * 12, addr=2))


def test_exception_reply():
    with pytest.raises(c.ModbusError, match="exception code 2"):
        c.parse_read_response(1, 12, c.with_crc(bytes([1, 0x83, 2])))


def test_to_signed():
    assert c.to_signed(0xFFFF) == -1 and c.to_signed(100) == 100
