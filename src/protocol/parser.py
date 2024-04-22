"""protocol.parser module."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from enum import IntEnum, auto
from typing import Iterator


class ProtocolError(Exception):
    """Protocol error."""

    def __init__(self, invalid_byte: int, bad_value: bytes) -> None:
        self.bad_value = bad_value
        self.invalid_byte = invalid_byte
        super().__init__(f"unexpected byte: {bytes([invalid_byte])}")


class State(IntEnum):
    OP_START = 0
    OP_PLUS = auto()
    OP_PLUS_O = auto()
    OP_PLUS_OK = auto()
    OP_MINUS = auto()
    OP_MINUS_E = auto()
    OP_MINUS_ER = auto()
    OP_MINUS_ERR = auto()
    OP_MINUS_ERR_SPC = auto()
    MINUS_ERR_ARG = auto()
    OP_M = auto()
    OP_MS = auto()
    OP_MSG = auto()
    OP_MSG_SPC = auto()
    MSG_ARG = auto()
    MSG_PAYLOAD = auto()
    MSG_END = auto()
    OP_H = auto()
    OP_HM = auto()
    OP_HMS = auto()
    OP_HMSG = auto()
    OP_HMSG_SPC = auto()
    HMSG_ARG = auto()
    HMSG_END = auto()
    HMSG_PAYLOAD = auto()
    OP_P = auto()
    OP_PI = auto()
    OP_PIN = auto()
    OP_PING = auto()
    OP_PO = auto()
    OP_PON = auto()
    OP_PONG = auto()
    OP_I = auto()
    OP_IN = auto()
    OP_INF = auto()
    OP_INFO = auto()
    OP_INFO_SPC = auto()
    INFO_ARG = auto()
    OP_END = auto()


class Character(IntEnum):
    # +/-
    plus = ord("+")
    minus = ord("-")
    # ok
    o = ord("o")
    O = ord("O")
    k = ord("k")
    K = ord("K")
    # err
    e = ord("e")
    E = ord("E")
    r = ord("r")
    R = ord("R")
    # pub
    p = ord("p")
    P = ord("P")
    u = ord("u")
    U = ord("U")
    b = ord("b")
    B = ord("B")
    # sub
    s = ord("s")
    S = ord("S")
    # hpub
    h = ord("h")
    H = ord("H")
    # msg
    m = ord("m")
    M = ord("M")
    g = ord("g")
    G = ord("G")
    # ping
    i = ord("i")
    I = ord("I")
    n = ord("n")
    N = ord("N")
    # info
    f = ord("f")
    F = ord("F")
    # special characters
    space = ord(" ")
    newline = ord("\n")
    carriage_return = ord("\r")
    left_json_bracket = ord("{")


class Operation(IntEnum):
    OK = 0
    ERR = auto()
    MSG = auto()
    HMSG = auto()
    INFO = auto()
    PING = auto()
    PONG = auto()


@dataclass
class Event:
    """NATS Protocol event."""

    operation: Operation


@dataclass
class ErrorEvent(Event):
    """NATS Protocol error event."""

    message: str


@dataclass
class MsgEvent(Event):
    """NATS Protocol message event."""

    sid: int
    subject: str
    reply_to: str
    payload: bytes
    header: bytes


@dataclass
class Version:
    major: int
    minor: int
    patch: int
    dev: str

    def as_string(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}-{self.dev}"

    def __eq__(self, other: "Version") -> bool:
        if not isinstance(other, Version):
            return False
        return (
            self.major == other.major
            and self.minor == other.minor
            and self.patch == other.patch
            and self.dev == other.dev
        )

    def __gt__(self, other: "Version") -> bool:
        if not isinstance(other, Version):
            raise TypeError("unorderable types: Version() > {}".format(type(other)))
        if self.major > other.major:
            return True
        if self.major == other.major:
            if self.minor > other.minor:
                return True
            if self.minor == other.minor:
                if self.patch > other.patch:
                    return True
                if self.patch == other.patch:
                    return self.dev > other.dev
        return False


@dataclass
class InfoEvent(Event):
    proto: int
    server_id: str
    server_name: str
    version: Version
    go: str
    host: str
    port: int
    max_payload: int | None
    headers: bool | None
    client_id: int | None
    auth_required: bool | None
    tls_required: bool | None
    tls_verify: bool | None
    tls_available: bool | None
    connect_urls: list[str] | None
    ws_connect_urls: list[str] | None
    ldm: bool | None
    git_commit: str | None
    jetstream: bool | None
    ip: str | None
    client_ip: str | None
    nonce: str | None
    cluster: str | None
    domain: str | None
    xkey: str | None


CRLF = bytes([Character.carriage_return, Character.newline])
CRLF_SIZE = len(CRLF)


class Parser:
    """NATS Protocol parser."""

    def __init__(self, debug_max_history: int = 0) -> None:
        # Initialize the parser state.
        self._closed = False
        self._state_history = make_history(debug_max_history)
        self._data_received = b""
        self._events_received: list[Event] = []
        # Initialize pending args
        self._partial_ascii_text = ""
        # Initialize pending message
        self._expected_header_size = 0
        self._expected_total_size = 0
        self._partial_msg: MsgEvent | None = None
        # Initialize the parser iterator
        self.__loop__ = self.__parse__()

    def history(self) -> list[State]:
        """Return the history of states."""
        if __debug__:
            return list(self._state_history)
        raise RuntimeError("history is only available in debug mode")

    def state(self) -> State:
        """Return the current state of the parser."""
        return self._state_history[-1]

    def events(self) -> list[Event]:
        """Pop and return the events generated by the parser."""
        events = self._events_received
        self._events_received = []
        return events

    def parse(self, data: bytes) -> None:
        self._data_received += data
        next(self.__loop__)

    def __parse__(self) -> Iterator[None]:
        """Parse some bytes."""

        if __debug__:
            assert self._state_history, "history is empty"
            assert (
                self._state_history[-1] == State.OP_START
            ), "history is not in the start state"

            def set_state(state: State) -> None:
                self._state_history.append(state)
        else:

            def set_state(state: State) -> None:
                self._state_history[-1] = state

        while True:
            # Exit when the parser is closed.
            if self._closed:
                return
            # If there is no data to parse, yield None.
            if not self._data_received:
                yield None
                continue
            # Take the first byte
            next_byte = self._data_received[0]
            # Take the remaining data
            pending_data = self._data_received = self._data_received[1:]
            # Get the current state
            state = self._state_history[-1]

            if state == State.OP_START:
                # MSG
                if next_byte == Character.M or next_byte == Character.m:
                    set_state(State.OP_M)
                    continue
                # HMSG
                elif next_byte == Character.H or next_byte == Character.h:
                    set_state(State.OP_H)
                    continue
                # PING/PONG
                elif next_byte == Character.P or next_byte == Character.p:
                    set_state(State.OP_P)
                    continue
                # INFO
                elif next_byte == Character.I or next_byte == Character.i:
                    set_state(State.OP_I)
                    continue
                # +OK
                elif next_byte == Character.plus:
                    set_state(State.OP_PLUS)
                    continue
                # -ERR
                elif next_byte == Character.minus:
                    set_state(State.OP_MINUS)
                    continue
                # Anything else is an error
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_H:
                if next_byte == Character.m or next_byte == Character.M:
                    set_state(State.OP_HM)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_HM:
                if next_byte == Character.s or next_byte == Character.S:
                    set_state(State.OP_HMS)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_HMS:
                if next_byte == Character.g or next_byte == Character.G:
                    set_state(State.OP_HMSG)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_HMSG:
                if next_byte == Character.space:
                    set_state(State.OP_HMSG_SPC)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_HMSG_SPC:
                if next_byte == Character.carriage_return:
                    raise ProtocolError(next_byte, pending_data)
                elif next_byte == Character.newline:
                    raise ProtocolError(next_byte, pending_data)
                elif next_byte == Character.space:
                    raise ProtocolError(next_byte, pending_data)
                else:
                    try:
                        self._partial_ascii_text += chr(next_byte)
                    except Exception:
                        raise ProtocolError(next_byte, pending_data)
                    set_state(State.HMSG_ARG)
                    continue

            elif state == State.HMSG_ARG:
                if next_byte == Character.carriage_return:
                    args = self._partial_ascii_text.split(" ")
                    self._partial_ascii_text = ""
                    nbargs = len(args)
                    if nbargs == 5:
                        (
                            subject,
                            raw_sid,
                            reply_to,
                            raw_header_size,
                            raw_total_size,
                        ) = args
                    elif nbargs == 4:
                        reply_to = ""
                        subject, raw_sid, raw_header_size, raw_total_size = args
                    else:
                        raise ProtocolError(next_byte, pending_data)
                    try:
                        self._expected_header_size = int(raw_header_size)
                        self._expected_total_size = int(raw_total_size)
                        sid = int(raw_sid)
                    except Exception as e:
                        raise ProtocolError(next_byte, pending_data) from e
                    self._partial_msg = MsgEvent(
                        Operation.HMSG,
                        sid=sid,
                        subject=subject,
                        reply_to=reply_to,
                        payload=b"",
                        header=b"",
                    )
                    set_state(State.HMSG_END)
                    continue
                elif next_byte == Character.newline:
                    raise ProtocolError(next_byte, pending_data)
                else:
                    try:
                        self._partial_ascii_text += chr(next_byte)
                    except Exception:
                        raise ProtocolError(next_byte, pending_data)
                    continue

            elif state == State.HMSG_END:
                if next_byte == Character.newline:
                    set_state(State.HMSG_PAYLOAD)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.HMSG_PAYLOAD:
                assert self._partial_msg is not None, "pending_msg is None"
                pending_data = bytes([next_byte]) + pending_data
                if len(pending_data) >= self._expected_total_size + CRLF_SIZE:
                    msg = self._partial_msg
                    self._partial_msg = None

                    header = pending_data[: self._expected_header_size]
                    if header[-4:] != b"\r\n\r\n":
                        raise ProtocolError(next_byte, pending_data)
                    msg.header = header[:-4]

                    payload = pending_data[
                        self._expected_header_size : self._expected_total_size
                    ]
                    msg.payload = payload
                    self._data_received = pending_data[
                        self._expected_total_size + CRLF_SIZE :
                    ]
                    self._events_received.append(msg)
                    set_state(State.OP_END)
                    set_state(State.OP_START)
                    continue
                else:
                    self._data_received = pending_data
                    yield None
                    continue

            elif state == State.OP_M:
                if next_byte == Character.s or next_byte == Character.S:
                    set_state(State.OP_MS)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_MS:
                if next_byte == Character.g or next_byte == Character.G:
                    set_state(State.OP_MSG)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_MSG:
                if next_byte == Character.space:
                    set_state(State.OP_MSG_SPC)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_MSG_SPC:
                if next_byte == Character.carriage_return:
                    raise ProtocolError(next_byte, pending_data)
                elif next_byte == Character.newline:
                    raise ProtocolError(next_byte, pending_data)
                elif next_byte == Character.space:
                    raise ProtocolError(next_byte, pending_data)
                else:
                    try:
                        self._partial_ascii_text += chr(next_byte)
                    except Exception:
                        raise ProtocolError(next_byte, pending_data)
                    set_state(State.MSG_ARG)
                    continue

            elif state == State.MSG_ARG:
                if next_byte == Character.carriage_return:
                    set_state(State.MSG_END)
                    args = self._partial_ascii_text.split(" ")
                    nbargs = len(args)
                    self._partial_ascii_text = ""
                    if nbargs == 4:
                        subject, raw_sid, reply_to, raw_total_size = args
                    elif nbargs == 3:
                        reply_to = ""
                        subject, raw_sid, raw_total_size = args
                    else:
                        raise ProtocolError(next_byte, pending_data)
                    try:
                        sid = int(raw_sid)
                        self._expected_total_size = int(raw_total_size)
                    except Exception as e:
                        raise ProtocolError(next_byte, pending_data) from e
                    self._partial_msg = MsgEvent(
                        Operation.MSG,
                        sid=sid,
                        subject=subject,
                        reply_to=reply_to,
                        payload=b"",
                        header=b"",
                    )
                    continue
                elif next_byte == Character.newline:
                    raise ProtocolError(next_byte, pending_data)
                else:
                    try:
                        self._partial_ascii_text += chr(next_byte)
                    except Exception:
                        raise ProtocolError(next_byte, pending_data)
                    continue

            elif state == State.MSG_END:
                if next_byte == Character.newline:
                    set_state(State.MSG_PAYLOAD)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.MSG_PAYLOAD:
                assert self._partial_msg is not None, "pending_msg is None"
                pending_data = bytes([next_byte]) + pending_data
                if len(pending_data) >= self._expected_total_size + CRLF_SIZE:
                    msg = self._partial_msg
                    self._partial_msg = None
                    msg.payload = pending_data[: self._expected_total_size]
                    self._data_received = pending_data[
                        self._expected_total_size + CRLF_SIZE :
                    ]
                    self._events_received.append(msg)
                    set_state(State.OP_END)
                    set_state(State.OP_START)
                    continue
                else:
                    self._data_received = pending_data
                    yield None
                    continue

            elif state == State.OP_I:
                if next_byte == Character.n or next_byte == Character.N:
                    set_state(State.OP_IN)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_IN:
                if next_byte == Character.f or next_byte == Character.F:
                    set_state(State.OP_INF)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_INF:
                if next_byte == Character.o or next_byte == Character.O:
                    set_state(State.OP_INFO)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_INFO:
                if next_byte == Character.space:
                    set_state(State.OP_INFO_SPC)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_INFO_SPC:
                if next_byte == Character.left_json_bracket:
                    set_state(State.INFO_ARG)
                    pending = bytes([next_byte]) + pending_data
                    self._data_received = pending
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.INFO_ARG:
                pending = bytes([next_byte]) + pending_data
                if CRLF in pending:
                    end = pending.index(CRLF)
                    data = pending[:end]
                    self._data_received = pending[end + CRLF_SIZE :]
                    set_state(State.OP_END)
                    set_state(State.OP_START)
                    self._events_received.append(parse_info(data))
                    continue
                else:
                    self._data_received = pending
                    yield None
                    continue

            elif state == State.OP_PLUS:
                if next_byte == Character.o or next_byte == Character.O:
                    set_state(State.OP_PLUS_O)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_PLUS_O:
                if next_byte == Character.k or next_byte == Character.K:
                    set_state(State.OP_PLUS_OK)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_PLUS_OK:
                if next_byte == Character.carriage_return:
                    set_state(State.OP_END)
                    self._events_received.append(Event(Operation.OK))
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_MINUS:
                if next_byte == Character.e or next_byte == Character.E:
                    set_state(State.OP_MINUS_E)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_MINUS_E:
                if next_byte == Character.r or next_byte == Character.R:
                    set_state(State.OP_MINUS_ER)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_MINUS_ER:
                if next_byte == Character.r or next_byte == Character.R:
                    set_state(State.OP_MINUS_ERR)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_MINUS_ERR:
                if next_byte == Character.space:
                    set_state(State.OP_MINUS_ERR_SPC)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_MINUS_ERR_SPC:
                if next_byte == Character.carriage_return:
                    raise ProtocolError(next_byte, pending_data)
                elif next_byte == Character.newline:
                    raise ProtocolError(next_byte, pending_data)
                else:
                    try:
                        self._partial_ascii_text += chr(next_byte)
                    except Exception:
                        raise ProtocolError(next_byte, pending_data)
                    set_state(State.MINUS_ERR_ARG)
                    continue

            elif state == State.MINUS_ERR_ARG:
                if next_byte == Character.carriage_return:
                    msg = self._partial_ascii_text
                    set_state(State.OP_END)
                    self._events_received.append(ErrorEvent(Operation.ERR, msg))
                    self._partial_ascii_text = ""
                elif next_byte == Character.newline:
                    raise ProtocolError(next_byte, pending_data)
                else:
                    try:
                        self._partial_ascii_text += chr(next_byte)
                        continue
                    except Exception:
                        raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_P:
                if next_byte == Character.i or next_byte == Character.I:
                    set_state(State.OP_PI)
                    continue
                elif next_byte == Character.o or next_byte == Character.O:
                    set_state(State.OP_PO)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_PI:
                if next_byte == Character.n or next_byte == Character.N:
                    set_state(State.OP_PIN)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_PIN:
                if next_byte == Character.g or next_byte == Character.G:
                    set_state(State.OP_PING)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_PING:
                if next_byte == Character.carriage_return:
                    set_state(State.OP_END)
                    self._events_received.append(Event(Operation.PING))
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_PO:
                if next_byte == Character.n or next_byte == Character.N:
                    set_state(State.OP_PON)
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_PON:
                if next_byte == Character.g or next_byte == Character.G:
                    set_state(State.OP_PONG)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_PONG:
                if next_byte == Character.carriage_return:
                    set_state(State.OP_END)
                    self._events_received.append(Event(Operation.PONG))
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            elif state == State.OP_END:
                if next_byte == Character.newline:
                    set_state(State.OP_START)
                    continue
                else:
                    raise ProtocolError(next_byte, pending_data)

            else:
                raise ProtocolError(next_byte, pending_data)


def parse_info(data: bytes) -> InfoEvent:
    raw_info = json.loads(data.decode())
    return InfoEvent(
        operation=Operation.INFO,
        server_id=raw_info["server_id"],
        server_name=raw_info["server_name"],
        version=parse_version(raw_info["version"]),
        go=raw_info["go"],
        host=raw_info["host"],
        port=raw_info["port"],
        headers=raw_info["headers"],
        proto=raw_info["proto"],
        max_payload=raw_info.get("max_payload"),
        client_id=raw_info.get("client_id"),
        auth_required=raw_info.get("auth_required"),
        tls_required=raw_info.get("tls_required"),
        tls_verify=raw_info.get("tls_verify"),
        tls_available=raw_info.get("tls_available"),
        connect_urls=raw_info.get("connect_urls"),
        ws_connect_urls=raw_info.get("ws_connect_urls"),
        ldm=raw_info.get("ldm"),
        git_commit=raw_info.get("git_commit"),
        jetstream=raw_info.get("jetstream"),
        ip=raw_info.get("ip"),
        client_ip=raw_info.get("client_ip"),
        nonce=raw_info.get("nonce"),
        cluster=raw_info.get("cluster"),
        domain=raw_info.get("domain"),
        xkey=raw_info.get("xkey"),
    )


def parse_version(version: str) -> Version:
    semver = Version(0, 0, 0, "")
    v = version.split("-")
    if len(v) > 1:
        semver.dev = v[1]
    tokens = v[0].split(".")
    n = len(tokens)
    if n > 1:
        semver.major = int(tokens[0])
    if n > 2:
        semver.minor = int(tokens[1])
    if n > 3:
        semver.patch = int(tokens[2])
    return semver


def make_history(max_length: int) -> deque[State]:
    if max_length < -1:
        raise ValueError(
            "history must be -1, 0 or a positive integer. "
            "-1 means unlimited history. "
            "0 means no history. "
            "A positive integer means the maximum number of states to keep in history excluding the current state."
        )
    if __debug__:
        maxlen = max_length + 1 if max_length >= 0 else None
    else:
        if max_length:
            raise ValueError("history is only available in debug mode")
        maxlen = 1
    return deque([State.OP_START], maxlen=maxlen)
