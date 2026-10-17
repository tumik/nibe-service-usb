import asyncio
import json

import pytest

from nibe_service_usb.api.framing import (
    FramingError,
    LineTooLong,
    classify,
    encode_message,
    is_request,
    read_message,
)


def reader_with(data: bytes, limit: int = 2**20) -> asyncio.StreamReader:
    reader = asyncio.StreamReader(limit=limit)
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def test_read_message_basic_and_eof():
    reader = reader_with(b'{"id":1,"cmd":"ping"}\n{"a":2}\n')
    assert await read_message(reader) == {"id": 1, "cmd": "ping"}
    assert await read_message(reader) == {"a": 2}
    assert await read_message(reader) is None


async def test_partial_last_line_is_eof():
    assert await read_message(reader_with(b'{"a":1}')) is None


@pytest.mark.parametrize(
    "raw", [b"not json\n", b"\n", b"[1,2]\n", b"3\n", b'"x"\n', b"\xff\xfe\n", b'{"a":\n']
)
async def test_invalid_lines_raise(raw):
    with pytest.raises(FramingError):
        await read_message(reader_with(raw))


async def test_too_long_line_with_large_reader_limit():
    line = b'{"x":"' + b"a" * 70000 + b'"}\n'
    with pytest.raises(LineTooLong):
        await read_message(reader_with(line), max_line=65536)


async def test_too_long_line_with_small_reader_limit():
    line = b'{"x":"' + b"a" * 5000 + b'"}\n'
    with pytest.raises(LineTooLong):
        await read_message(reader_with(line, limit=1024))


async def test_line_at_max_length_is_accepted():
    body = b'{"x":"' + b"a" * 50 + b'"}'
    assert await read_message(reader_with(body + b"\n"), max_line=len(body)) == {"x": "a" * 50}


def test_encode_message_compact_newline_terminated():
    data = encode_message({"id": 1, "ok": True, "data": {"t": "°C"}})
    assert data.endswith(b"\n") and data.count(b"\n") == 1
    assert b" " not in data
    assert json.loads(data) == {"id": 1, "ok": True, "data": {"t": "°C"}}


def test_encode_rejects_nan():
    with pytest.raises(ValueError):
        encode_message({"v": float("nan")})


@pytest.mark.parametrize(
    "msg, expected",
    [
        ({"id": 1, "cmd": "ping"}, True),
        ({"id": 1, "cmd": "ping", "params": {}}, True),
        ({"id": 1, "cmd": "ping", "params": {"a": 1}, "extra": 1}, True),
        ({"id": "1", "cmd": "ping"}, False),
        ({"id": True, "cmd": "ping"}, False),
        ({"id": 1}, False),
        ({"cmd": "ping"}, False),
        ({"id": 1, "cmd": 5}, False),
        ({"id": 1, "cmd": "ping", "params": [1]}, False),
        ({"event": "hello"}, False),
    ],
)
def test_classify(msg, expected):
    assert is_request(msg) is expected
    assert classify(msg) == ("request" if expected else "invalid")
