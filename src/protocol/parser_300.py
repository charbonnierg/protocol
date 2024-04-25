"""
NATS protocol parser.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterator

from .common import (
    CRLF,
    CRLF_SIZE,
    OK_EVENT,
    PING_EVENT,
    PONG_EVENT,
    ErrorEvent,
    Event,
    HMsgEvent,
    MsgEvent,
    ParserClosedError,
    ProtocolError,
    parse_info,
)

STOP_HEADER = bytearray(b"\r\n\r\n")
PING_OP = bytearray(b"PING\r\n")
PONG_OP = bytearray(b"PONG\r\n")
OK_OP = bytearray(b"+OK\r\n")
OK_OP_LEN = len(OK_OP)
PING_OR_PONG_OP_LEN = len(PING_OP)

AWAITING_CONTROL_LINE = 0
AWAITING_MSG_PAYLOAD = 1
AWAITING_HMSG_PAYLOAD = 2


class Parser300:
    """NATS Protocol parser."""

    __slots__ = ["_closed", "_state", "_data_received", "_events_received", "__loop__"]

    def __init__(self) -> None:
        # Initialize the parser state.
        self._closed = False
        self._data_received = bytearray()
        self._events_received: list[Event] = []
        # Initialize the parser iterator
        self.__loop__ = self.__parse__()

    def __repr__(self) -> str:
        return "<nats protocol parser backend=300>"

    def close(self) -> None:
        """Close the parser."""
        self._closed = True

    def events_received(self) -> list[Event]:
        """Pop and return the events generated by the parser."""
        events = self._events_received
        self._events_received = []
        return events

    def parse(self, data: bytes | bytearray) -> None:
        self._data_received.extend(data)
        try:
            self.__loop__.__next__()
        except StopIteration:
            raise ParserClosedError()

    def __parse__(self) -> Iterator[None]:
        """Parse some bytes."""

        expected_header_size = 0
        expected_total_size = 0
        partial_msg: MsgEvent | HMsgEvent | None = None
        state = AWAITING_CONTROL_LINE

        while not self._closed:
            # If there is no data to parse, yield None.
            if not self._data_received:
                yield None
                continue
            # Take the first byte
            next_byte = self._data_received[0]

            if state == AWAITING_CONTROL_LINE:
                if next_byte == 77:  # "M"
                    try:
                        end = self._data_received.index(CRLF)
                    except ValueError:
                        yield None
                        continue

                    data = self._data_received[4:end]
                    args = data.split(b" ")
                    if len(args) == 4:
                        subject, raw_sid, reply_to, raw_total_size = args
                    elif len(args) == 3:
                        reply_to = bytearray()
                        subject, raw_sid, raw_total_size = args
                    else:
                        raise ProtocolError()
                    try:
                        sid = int(raw_sid)
                        expected_total_size = int(raw_total_size)
                    except Exception as e:
                        raise ProtocolError() from e
                    if (
                        len(self._data_received[end + 2 :])
                        >= expected_total_size + CRLF_SIZE
                    ):
                        self._events_received.append(
                            MsgEvent(
                                sid=sid,
                                subject=subject.decode(),
                                reply_to=reply_to.decode(),
                                payload=self._data_received[
                                    end + 2 : end + 2 + expected_total_size
                                ],
                            )
                        )
                        self._data_received = self._data_received[
                            end + expected_total_size + 5 :
                        ]
                        continue
                    else:
                        partial_msg = MsgEvent(
                            sid=sid,
                            subject=subject.decode(),
                            reply_to=reply_to.decode(),
                            payload=bytearray(),
                        )
                        state = AWAITING_MSG_PAYLOAD
                        self._data_received: bytearray = self._data_received[end + 2 :]
                        yield None
                        continue
                elif next_byte == 72:  # "H"
                    # Fast path for HMSG
                    try:
                        end = self._data_received.index(CRLF)
                    except ValueError:
                        yield None
                        continue
                    args = self._data_received[5:end].split(b" ")
                    if len(args) == 5:
                        (
                            subject,
                            raw_sid,
                            reply_to,
                            raw_header_size,
                            raw_total_size,
                        ) = args
                    elif len(args) == 4:
                        reply_to = b""
                        (
                            subject,
                            raw_sid,
                            raw_header_size,
                            raw_total_size,
                        ) = args
                    else:
                        raise ProtocolError()
                    try:
                        expected_header_size = int(raw_header_size)
                        expected_total_size = int(raw_total_size)
                        sid = int(raw_sid)
                    except Exception as e:
                        raise ProtocolError() from e
                    if (
                        len(self._data_received[end + 2 :])
                        >= expected_total_size + CRLF_SIZE
                    ):
                        if (
                            self._data_received[
                                end - 2 + expected_header_size : end
                                + 2
                                + expected_header_size
                            ]
                            != STOP_HEADER
                        ):
                            raise ProtocolError()
                        self._events_received.append(
                            HMsgEvent(
                                sid=sid,
                                subject=subject.decode(),
                                reply_to=reply_to.decode(),
                                payload=self._data_received[
                                    end + 2 + expected_header_size : end
                                    + 2
                                    + expected_total_size
                                ],
                                header=self._data_received[
                                    end + 2 : end - 2 + expected_header_size
                                ],
                            )
                        )
                        self._data_received = self._data_received[
                            end + expected_total_size + 5 :
                        ]
                        continue
                    else:
                        partial_msg = HMsgEvent(
                            sid=sid,
                            subject=subject.decode(),
                            reply_to=reply_to.decode(),
                            payload=bytearray(),
                            header=bytearray(),
                        )
                        state = AWAITING_HMSG_PAYLOAD
                        self._data_received = self._data_received[end + 2 :]
                        yield None
                        continue
                elif next_byte == 80:  # "P"
                    # Fast path for PING or PONG
                    if len(self._data_received) >= PING_OR_PONG_OP_LEN:
                        if self._data_received[:PING_OR_PONG_OP_LEN] == PING_OP:
                            self._events_received.append(PING_EVENT)
                        elif self._data_received[:PING_OR_PONG_OP_LEN] == PONG_OP:
                            self._events_received.append(PONG_EVENT)
                        else:
                            raise ProtocolError()
                        self._data_received = self._data_received[PING_OR_PONG_OP_LEN:]
                        continue
                    # Split buffer
                    else:
                        yield None
                        continue
                elif next_byte == 73:  # "I"
                    try:
                        end = self._data_received.index(CRLF)
                    except ValueError:
                        yield None
                        continue
                    try:
                        self._events_received.append(
                            parse_info(self._data_received[5:end])
                        )
                    except Exception as e:
                        raise ProtocolError() from e
                    self._data_received = self._data_received[end + 3 :]
                    continue
                elif next_byte == 43:  # "+"
                    if len(self._data_received) < 5:
                        yield None
                        continue
                    if self._data_received[:5] != OK_OP:
                        raise ProtocolError()
                    self._data_received = self._data_received[OK_OP_LEN + 1 :]
                    self._events_received.append(OK_EVENT)
                    continue
                elif next_byte == 45:  # "-"
                    try:
                        end = self._data_received.index(CRLF)
                    except ValueError:
                        yield None
                        continue
                    msg = self._data_received[5:end].decode()
                    if msg[0] != "'":
                        raise ProtocolError()
                    if msg[-1] != "'":
                        raise ProtocolError()
                    self._events_received.append(ErrorEvent(msg[1:-1].lower()))
                    self._data_received = self._data_received[end + CRLF_SIZE :]
                    continue
                else:
                    # Anything else is an error
                    raise ProtocolError()
            elif state == AWAITING_HMSG_PAYLOAD:
                assert partial_msg is not None, "pending_msg is None"
                if len(self._data_received) >= expected_total_size + CRLF_SIZE:
                    if (
                        self._data_received[
                            expected_header_size - 4 : expected_header_size
                        ]
                        != STOP_HEADER
                    ):
                        raise ProtocolError()
                    partial_msg.header = self._data_received[: expected_header_size - 4]
                    partial_msg.payload = self._data_received[
                        expected_header_size:expected_total_size
                    ]
                    self._data_received = self._data_received[expected_total_size + 3 :]
                    self._events_received.append(partial_msg)
                    state = AWAITING_CONTROL_LINE
                    continue
                else:
                    yield None
                    continue
            else:
                assert partial_msg is not None, "pending_msg is None"
                if len(self._data_received) >= expected_total_size + CRLF_SIZE:
                    partial_msg.payload = self._data_received[:expected_total_size]
                    self._data_received = self._data_received[expected_total_size + 3 :]
                    self._events_received.append(partial_msg)
                    state = AWAITING_CONTROL_LINE
                    continue
                else:
                    yield None
                    continue


if TYPE_CHECKING:
    from .common import Parser as ParserProtocol

    # Verify that Parser implements ParserProtocol
    parser: ParserProtocol = Parser300()
