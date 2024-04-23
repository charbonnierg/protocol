"""
NATS protocol parser.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterator

from .common import (
    CRLF,
    CRLF_SIZE,
    Character,
    ErrorEvent,
    Event,
    MsgEvent,
    Operation,
    ProtocolError,
    State,
    parse_info,
)

STOP_HEADER = bytearray(b"\r\n\r\n")


class Parser310:
    """NATS Protocol parser."""

    __slots__ = ["_closed", "_state", "_data_received", "_events_received", "__loop__"]

    def __init__(self) -> None:
        # Initialize the parser state.
        self._closed = False
        self._data_received = bytearray()
        self._events_received: list[Event] = []
        # Initialize the parser iterator
        self.__loop__ = self.__parse__()

    def events_received(self) -> list[Event]:
        """Pop and return the events generated by the parser."""
        events = self._events_received
        self._events_received = []
        return events

    def parse(self, data: bytes | bytearray) -> None:
        self._data_received.extend(data)
        self.__loop__.__next__()

    def __parse__(self) -> Iterator[None]:
        """Parse some bytes."""

        cursor = 0
        expected_header_size = 0
        expected_total_size = 0
        partial_msg: MsgEvent | None = None
        state = State.OP_START

        while not self._closed:
            # If there is no data to parse, yield None.
            if not self._data_received:
                yield None
                continue
            # Take the first byte
            next_byte = self._data_received[cursor]

            match state:
                case State.OP_START:
                    match next_byte:
                        case Character.M | Character.m:
                            # Fast path for MSG
                            if CRLF in self._data_received:
                                end = self._data_received.index(CRLF)
                                data = self._data_received[cursor + 4 : end]
                                args = data.decode().split(" ")
                                nbargs = len(args)
                                match nbargs:
                                    case 4:
                                        subject, raw_sid, reply_to, raw_total_size = (
                                            args
                                        )
                                    case 3:
                                        reply_to = ""
                                        subject, raw_sid, raw_total_size = args
                                    case _:
                                        raise ProtocolError(
                                            next_byte, self._data_received
                                        )
                                try:
                                    sid = int(raw_sid)
                                    expected_total_size = int(raw_total_size)
                                except Exception as e:
                                    raise ProtocolError(
                                        next_byte, self._data_received
                                    ) from e
                                partial_msg = MsgEvent(
                                    Operation.MSG,
                                    sid=sid,
                                    subject=subject,
                                    reply_to=reply_to,
                                    payload=bytearray(),
                                    header=bytearray(),
                                )
                                state = State.MSG_END
                                cursor = end + 1
                                continue
                            state = State.OP_M
                            cursor += 1
                            continue
                        case Character.H | Character.h:
                            # Fast path for HMSG
                            if CRLF in self._data_received:
                                end = self._data_received.index(CRLF)
                                args = (
                                    self._data_received[cursor + 5 : end]
                                    .decode()
                                    .split(" ")
                                )
                                match len(args):
                                    case 5:
                                        (
                                            subject,
                                            raw_sid,
                                            reply_to,
                                            raw_header_size,
                                            raw_total_size,
                                        ) = args
                                    case 4:
                                        reply_to = ""
                                        (
                                            subject,
                                            raw_sid,
                                            raw_header_size,
                                            raw_total_size,
                                        ) = args
                                    case _:
                                        raise ProtocolError(
                                            next_byte, self._data_received
                                        )
                                try:
                                    expected_header_size = int(raw_header_size)
                                    expected_total_size = int(raw_total_size)
                                    sid = int(raw_sid)
                                except Exception as e:
                                    raise ProtocolError(
                                        next_byte, self._data_received
                                    ) from e
                                partial_msg = MsgEvent(
                                    Operation.HMSG,
                                    sid=sid,
                                    subject=subject,
                                    reply_to=reply_to,
                                    payload=bytearray(),
                                    header=bytearray(),
                                )
                                state = State.HMSG_END
                                cursor = end + 1
                                continue
                            state = State.OP_H
                            cursor += 1
                            continue
                        case Character.P | Character.p:
                            state = State.OP_P
                            cursor += 1
                            continue
                        case Character.I | Character.i:
                            state = State.OP_I
                            cursor += 1
                            continue
                        case Character.plus:
                            state = State.OP_PLUS
                            cursor += 1
                            continue
                        case Character.minus:
                            state = State.OP_MINUS
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_H:
                    match next_byte:
                        case Character.M | Character.m:
                            state = State.OP_HM
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_HM:
                    match next_byte:
                        case Character.S | Character.s:
                            state = State.OP_HMS
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_HMS:
                    match next_byte:
                        case Character.G | Character.g:
                            state = State.OP_HMSG
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_HMSG:
                    match next_byte:
                        case Character.space:
                            state = State.OP_HMSG_SPC
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_HMSG_SPC:
                    match next_byte:
                        case (
                            Character.carriage_return
                            | Character.newline
                            | Character.space
                        ):
                            raise ProtocolError(next_byte, self._data_received)
                        case _:
                            state = State.HMSG_ARG
                            self._data_received = self._data_received[cursor:]
                            cursor = 1
                            continue
                case State.HMSG_ARG:
                    if CRLF in self._data_received:
                        end = self._data_received.index(CRLF)
                        args = self._data_received[:end].decode().split(" ")
                        match len(args):
                            case 5:
                                (
                                    subject,
                                    raw_sid,
                                    reply_to,
                                    raw_header_size,
                                    raw_total_size,
                                ) = args
                            case 4:
                                reply_to = ""
                                (
                                    subject,
                                    raw_sid,
                                    raw_header_size,
                                    raw_total_size,
                                ) = args
                            case _:
                                raise ProtocolError(next_byte, self._data_received)
                        try:
                            expected_header_size = int(raw_header_size)
                            expected_total_size = int(raw_total_size)
                            sid = int(raw_sid)
                        except Exception as e:
                            raise ProtocolError(next_byte, self._data_received) from e
                        partial_msg = MsgEvent(
                            Operation.HMSG,
                            sid=sid,
                            subject=subject,
                            reply_to=reply_to,
                            payload=bytearray(),
                            header=bytearray(),
                        )
                        state = State.HMSG_END
                        cursor = end + 1
                        continue
                    else:
                        yield None
                        continue
                case State.HMSG_END:
                    match next_byte:
                        case Character.newline:
                            state = State.HMSG_PAYLOAD
                            self._data_received = self._data_received[cursor + 1 :]
                            cursor = 0
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.HMSG_PAYLOAD:
                    assert partial_msg is not None, "pending_msg is None"
                    if len(self._data_received) >= expected_total_size + CRLF_SIZE:
                        msg = partial_msg
                        partial_msg = None
                        header = self._data_received[:expected_header_size]
                        if header[-4:] != STOP_HEADER:
                            raise ProtocolError(next_byte, header)
                        msg.header = header[:-4]
                        payload = self._data_received[
                            expected_header_size:expected_total_size
                        ]
                        msg.payload = payload
                        self._data_received = self._data_received[
                            expected_total_size + CRLF_SIZE + 1 :
                        ]
                        self._events_received.append(msg)
                        state = State.OP_END
                        state = State.OP_START
                        cursor = 0
                        continue
                    else:
                        yield None
                        continue
                case State.OP_M:
                    match next_byte:
                        case Character.S | Character.s:
                            state = State.OP_MS
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_MS:
                    match next_byte:
                        case Character.G | Character.g:
                            state = State.OP_MSG
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_MSG:
                    match next_byte:
                        case Character.space:
                            state = State.OP_MSG_SPC
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_MSG_SPC:
                    match next_byte:
                        case Character.carriage_return:
                            raise ProtocolError(next_byte, self._data_received)
                        case Character.newline:
                            raise ProtocolError(next_byte, self._data_received)
                        case Character.space:
                            raise ProtocolError(next_byte, self._data_received)
                        case _:
                            state = State.MSG_ARG
                            self._data_received = self._data_received[cursor:]
                            cursor = 1
                            continue
                case State.MSG_ARG:
                    if CRLF in self._data_received:
                        end = self._data_received.index(CRLF)
                        data = self._data_received[:end]
                        args = data.decode().split(" ")
                        nbargs = len(args)
                        match nbargs:
                            case 4:
                                subject, raw_sid, reply_to, raw_total_size = args
                            case 3:
                                reply_to = ""
                                subject, raw_sid, raw_total_size = args
                            case _:
                                raise ProtocolError(next_byte, self._data_received)
                        try:
                            sid = int(raw_sid)
                            expected_total_size = int(raw_total_size)
                        except Exception as e:
                            raise ProtocolError(next_byte, self._data_received) from e
                        partial_msg = MsgEvent(
                            Operation.MSG,
                            sid=sid,
                            subject=subject,
                            reply_to=reply_to,
                            payload=bytearray(),
                            header=bytearray(),
                        )
                        state = State.MSG_END
                        cursor = end + 1
                        continue
                    else:
                        yield None
                        continue
                case State.MSG_END:
                    match next_byte:
                        case Character.newline:
                            state = State.MSG_PAYLOAD
                            self._data_received = self._data_received[cursor + 1 :]
                            cursor = 0
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.MSG_PAYLOAD:
                    assert partial_msg is not None, "pending_msg is None"
                    if len(self._data_received) >= expected_total_size + CRLF_SIZE:
                        msg = partial_msg
                        partial_msg = None
                        msg.payload = self._data_received[:expected_total_size]
                        self._data_received = self._data_received[
                            expected_total_size + CRLF_SIZE + 1 :
                        ]
                        cursor = 0
                        self._events_received.append(msg)
                        state = State.OP_END
                        state = State.OP_START
                        continue
                    else:
                        yield None
                        continue
                case State.OP_P:
                    match next_byte:
                        case Character.I | Character.i:
                            state = State.OP_PI
                            cursor += 1
                            continue
                        case Character.O | Character.o:
                            state = State.OP_PO
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_PI:
                    match next_byte:
                        case Character.N | Character.n:
                            state = State.OP_PIN
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_PIN:
                    match next_byte:
                        case Character.G | Character.g:
                            state = State.OP_PING
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_PING:
                    match next_byte:
                        case Character.carriage_return:
                            state = State.OP_END
                            self._events_received.append(Event(Operation.PING))
                            self._data_received = self._data_received[cursor + 1 :]
                            cursor = 0
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_PO:
                    match next_byte:
                        case Character.N | Character.n:
                            state = State.OP_PON
                            cursor += 1
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_PON:
                    match next_byte:
                        case Character.G | Character.g:
                            state = State.OP_PONG
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_PONG:
                    match next_byte:
                        case Character.carriage_return:
                            state = State.OP_END
                            self._events_received.append(Event(Operation.PONG))
                            self._data_received = self._data_received[cursor + 1 :]
                            cursor = 0
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_I:
                    match next_byte:
                        case Character.N | Character.n:
                            state = State.OP_IN
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_IN:
                    match next_byte:
                        case Character.F | Character.f:
                            state = State.OP_INF
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_INF:
                    match next_byte:
                        case Character.O | Character.o:
                            state = State.OP_INFO
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_INFO:
                    match next_byte:
                        case Character.space:
                            state = State.OP_INFO_SPC
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_INFO_SPC:
                    match next_byte:
                        case Character.left_json_bracket:
                            state = State.INFO_ARG
                            self._data_received = self._data_received[cursor:]
                            cursor = 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.INFO_ARG:
                    if CRLF in self._data_received:
                        end = self._data_received.index(CRLF)
                        data = self._data_received[:end]
                        self._data_received = self._data_received[end + CRLF_SIZE + 1 :]
                        cursor = 0
                        state = State.OP_END
                        state = State.OP_START
                        self._events_received.append(parse_info(data))
                        continue
                    else:
                        yield None
                        continue
                case State.OP_PLUS:
                    match next_byte:
                        case Character.O | Character.o:
                            state = State.OP_PLUS_O
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_PLUS_O:
                    match next_byte:
                        case Character.K | Character.k:
                            state = State.OP_PLUS_OK
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_PLUS_OK:
                    match next_byte:
                        case Character.carriage_return:
                            state = State.OP_END
                            self._events_received.append(Event(Operation.OK))
                            self._data_received = self._data_received[cursor + 1 :]
                            cursor = 0
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_MINUS:
                    match next_byte:
                        case Character.E | Character.e:
                            state = State.OP_MINUS_E
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_MINUS_E:
                    match next_byte:
                        case Character.R | Character.r:
                            state = State.OP_MINUS_ER
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_MINUS_ER:
                    match next_byte:
                        case Character.R | Character.r:
                            state = State.OP_MINUS_ERR
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_MINUS_ERR:
                    match next_byte:
                        case Character.space:
                            state = State.OP_MINUS_ERR_SPC
                            cursor += 1
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)
                case State.OP_MINUS_ERR_SPC:
                    match next_byte:
                        case Character.carriage_return:
                            raise ProtocolError(next_byte, self._data_received)
                        case Character.newline:
                            raise ProtocolError(next_byte, self._data_received)
                        case _:
                            state = State.MINUS_ERR_ARG
                            self._data_received = self._data_received[cursor:]
                            cursor = 1
                            continue
                case State.MINUS_ERR_ARG:
                    match next_byte:
                        case Character.carriage_return:
                            msg = self._data_received[:cursor].decode()
                            state = State.OP_END
                            self._events_received.append(ErrorEvent(Operation.ERR, msg))
                            self._data_received = self._data_received[cursor + 1 :]
                            cursor = 0
                        case Character.newline:
                            raise ProtocolError(next_byte, self._data_received)
                        case _:
                            cursor += 1
                            continue
                case State.OP_END:
                    match next_byte:
                        case Character.newline:
                            state = State.OP_START
                            self._data_received = self._data_received[cursor + 1 :]
                            cursor = 0
                            continue
                        case _:
                            raise ProtocolError(next_byte, self._data_received)


if TYPE_CHECKING:
    from .common import Parser as ParserProtocol

    # Verify that Parser implements ParserProtocol
    parser: ParserProtocol = Parser310()
