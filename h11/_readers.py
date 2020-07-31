# Code to read HTTP data
#
# Strategy: each reader is a callable which takes a ReceiveBuffer object, and
# either:
# 1) consumes some of it and returns an Event
# 2) raises a LocalProtocolError (for consistency -- e.g. we call validate()
#    and it might raise a LocalProtocolError, so simpler just to always use
#    this)
# 3) returns None, meaning "I need more data"
#
# If they have a .read_eof attribute, then this will be called if an EOF is
# received -- but this is optional. Either way, the actual ConnectionClosed
# event will be generated afterwards.
#
# READERS is a dict describing how to pick a reader. It maps states to either:
# - a reader
# - or, for body readers, a dict of per-framing reader factories

import re

from ._abnf import chunk_header, header_field, request_line, status_line
from ._events import *
from ._state import *
from ._util import LocalProtocolError, RemoteProtocolError, validate

__all__ = ["READERS"]

header_field_re = re.compile(header_field.encode("ascii"))

# Remember that this has to run in O(n) time -- so e.g. the bytearray cast is
# critical.
obs_fold_re = re.compile(br"[ \t]+")


def _obsolete_line_fold(lines):
    it = iter(lines)
    last = None
    for line in it:
        match = obs_fold_re.match(line)
        if match:
            if last is None:
                raise LocalProtocolError("continuation line at start of headers")
            if not isinstance(last, bytearray):
                last = bytearray(last)
            last += b" "
            last += line[match.end() :]
        else:
            if last is not None:
                yield last
            last = line
    if last is not None:
        yield last


def _decode_header_lines(lines):
    for line in _obsolete_line_fold(lines):
        # _obsolete_line_fold yields either bytearray or bytes objects. On
        # Python 3, validate() takes either and returns matches as bytes. But
        # on Python 2, validate can return matches as bytearrays, so we have
        # to explicitly cast back.
        matches = validate(header_field_re, bytes(line), "illegal header line: {!r}", bytes(line))
        yield (matches["field_name"], matches["field_value"])


request_line_re = re.compile(request_line.encode("ascii"))


def maybe_read_from_IDLE_client(buf):
    lines = buf.maybe_extract_lines()
    if lines is None:
        return None
    if not lines:
        raise LocalProtocolError("no request line received")
    matches = validate(request_line_re, lines[0], "illegal request line: {!r}", lines[0])
    return Request(
        headers=list(_decode_header_lines(lines[1:])), _parsed=True, **matches
    )


status_line_re = re.compile(status_line.encode("ascii"))


def maybe_read_from_SEND_RESPONSE_server(buf):
    lines = buf.maybe_extract_lines()
    if lines is None:
        return None
    if not lines:
        raise LocalProtocolError("no response line received")
    matches = validate(status_line_re, lines[0], "illegal status line: {!r}", lines[0])
    # Tolerate missing reason phrases
    if matches["reason"] is None:
        matches["reason"] = b""
    status_code = matches["status_code"] = int(matches["status_code"])
    class_ = InformationalResponse if status_code < 200 else Response
    return class_(
        headers=list(_decode_header_lines(lines[1:])), _parsed=True, **matches
    )


class ContentLengthReader:
    def __init__(self, length):
        self._length = length
        self._remaining = length

    def __call__(self, buf):
        if self._remaining == 0:
            return EndOfMessage()
        data = buf.maybe_extract_at_most(self._remaining)
        if data is None:
            return None
        self._remaining -= len(data)
        return Data(data=data)

    def read_eof(self):
        raise RemoteProtocolError(
            "peer closed connection without sending complete message body "
            "(received {} bytes, expected {})".format(
                self._length - self._remaining, self._length
            )
        )


chunk_header_re = re.compile(chunk_header.encode("ascii"))


class ChunkedReader(object):
    def __init__(self):
        self._bytes_in_chunk = 0
        # After reading a chunk, we have to throw away the trailing \r\n; if
        # this is >0 then we discard that many bytes before resuming regular
        # de-chunkification.
        self._bytes_to_discard = 0
        self._reading_trailer = False

    def __call__(self, buf):
        if self._reading_trailer:
            lines = buf.maybe_extract_lines()
            if lines is None:
                return None
            return EndOfMessage(headers=list(_decode_header_lines(lines)))
        if self._bytes_to_discard > 0:
            data = buf.maybe_extract_at_most(self._bytes_to_discard)
            if data is None:
                return None
            self._bytes_to_discard -= len(data)
            if self._bytes_to_discard > 0:
                return None
            # else, fall through and read some more
        assert self._bytes_to_discard == 0
        if self._bytes_in_chunk == 0:
            # We need to refill our chunk count
            chunk_header = buf.maybe_extract_until_next(b"\r\n")
            if chunk_header is None:
                return None
            matches = validate(chunk_header_re, chunk_header, "illegal chunk header: {!r}", chunk_header)
            # XX FIXME: we discard chunk extensions. Does anyone care?
            # We convert to bytes because Python 2's `int()` function doesn't
            # work properly on bytearray objects.
            self._bytes_in_chunk = int(bytes(matches["chunk_size"]), base=16)
            if self._bytes_in_chunk == 0:
                self._reading_trailer = True
                return self(buf)
            chunk_start = True
        else:
            chunk_start = False
        assert self._bytes_in_chunk > 0
        data = buf.maybe_extract_at_most(self._bytes_in_chunk)
        if data is None:
            return None
        self._bytes_in_chunk -= len(data)
        if self._bytes_in_chunk == 0:
            self._bytes_to_discard = 2
            chunk_end = True
        else:
            chunk_end = False
        return Data(data=data, chunk_start=chunk_start, chunk_end=chunk_end)

    def read_eof(self):
        raise RemoteProtocolError(
            "peer closed connection without sending complete message body "
            "(incomplete chunked read)"
        )


class Http10Reader(object):
    def __call__(self, buf):
        data = buf.maybe_extract_at_most(999999999)
        if data is None:
            return None
        return Data(data=data)

    def read_eof(self):
        return EndOfMessage()


def expect_nothing(buf):
    if buf:
        raise LocalProtocolError("Got data when expecting EOF")
    return None


READERS = {
    (CLIENT, IDLE): maybe_read_from_IDLE_client,
    (SERVER, IDLE): maybe_read_from_SEND_RESPONSE_server,
    (SERVER, SEND_RESPONSE): maybe_read_from_SEND_RESPONSE_server,
    (CLIENT, DONE): expect_nothing,
    (CLIENT, MUST_CLOSE): expect_nothing,
    (CLIENT, CLOSED): expect_nothing,
    (SERVER, DONE): expect_nothing,
    (SERVER, MUST_CLOSE): expect_nothing,
    (SERVER, CLOSED): expect_nothing,
    SEND_BODY: {
        "chunked": ChunkedReader,
        "content-length": ContentLengthReader,
        "http/1.0": Http10Reader,
    },
}
