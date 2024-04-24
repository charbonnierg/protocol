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
    ProtocolError,
    State3102,
    parse_info,
)

STOP_HEADER = bytearray(b"\r\n\r\n")
PING_OP = bytearray(b"PING")
PONG_OP = bytearray(b"PONG")
PING_OR_PONG_LEN = len(PING_OP)
PING_OR_PONG_OP_LEN = PING_OR_PONG_LEN + CRLF_SIZE


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

        expected_header_size = 0
        expected_total_size = 0
        partial_msg: MsgEvent | HMsgEvent | None = None
        state = State3102.AWAITING_CONTROL_LINE

        while not self._closed:
            # If there is no data to parse, yield None.
            if not self._data_received:
                yield None
                continue
            # Take the first byte
            next_byte = self._data_received[0]

            if state == State3102.AWAITING_CONTROL_LINE:
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
                        reply_to = b""
                        subject, raw_sid, raw_total_size = args
                    else:
                        raise ProtocolError(next_byte, self._data_received)
                    try:
                        sid = int(raw_sid)
                        expected_total_size = int(raw_total_size)
                    except Exception as e:
                        raise ProtocolError(next_byte, self._data_received) from e
                    payload_start = end + 2
                    if (
                        len(self._data_received[payload_start:])
                        >= expected_total_size + CRLF_SIZE
                    ):
                        payload_end = payload_start + expected_total_size
                        self._events_received.append(
                            MsgEvent(
                                sid=sid,
                                subject=subject.decode("ascii"),
                                reply_to=reply_to.decode("ascii"),
                                payload=self._data_received[payload_start:payload_end],
                            )
                        )
                        self._data_received = self._data_received[
                            payload_end + CRLF_SIZE + 1 :
                        ]
                        continue
                    else:
                        partial_msg = MsgEvent(
                            sid=sid,
                            subject=subject.decode("ascii"),
                            reply_to=reply_to.decode("ascii"),
                            payload=bytearray(),
                        )
                        state = State3102.AWAITING_MSG_PAYLOAD
                        self._data_received: bytearray = self._data_received[
                            payload_start:
                        ]
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
                        raise ProtocolError(next_byte, self._data_received)
                    try:
                        expected_header_size = int(raw_header_size)
                        expected_total_size = int(raw_total_size)
                        sid = int(raw_sid)
                    except Exception as e:
                        raise ProtocolError(next_byte, self._data_received) from e
                    header_start = end + 2
                    if (
                        len(self._data_received[header_start:])
                        >= expected_total_size + CRLF_SIZE
                    ):
                        header_stop = header_start + expected_header_size
                        payload_stop = header_start + expected_total_size
                        header = self._data_received[header_start:header_stop]
                        if header[-4:] != STOP_HEADER:
                            raise ProtocolError(next_byte, header)
                        self._events_received.append(
                            HMsgEvent(
                                sid=sid,
                                subject=subject.decode("ascii"),
                                reply_to=reply_to.decode("ascii"),
                                payload=self._data_received[header_stop:payload_stop],
                                header=header[:-4],
                            )
                        )
                        self._data_received = self._data_received[
                            payload_stop + CRLF_SIZE + 1 :
                        ]
                        continue
                    else:
                        partial_msg = HMsgEvent(
                            sid=sid,
                            subject=subject.decode("ascii"),
                            reply_to=reply_to.decode("ascii"),
                            payload=bytearray(),
                            header=bytearray(),
                        )
                        state = State3102.AWAITING_HMSG_PAYLOAD
                        self._data_received = self._data_received[header_start:]
                        yield None
                        continue
                elif next_byte == 80:  # "P"
                    # Fast path for PING or PONG
                    if len(self._data_received) >= PING_OR_PONG_OP_LEN:
                        data = self._data_received[:PING_OR_PONG_LEN].upper()
                        if data == PING_OP:
                            self._events_received.append(PING_EVENT)
                        elif data == PONG_OP:
                            self._events_received.append(PONG_EVENT)
                        else:
                            raise ProtocolError(next_byte, self._data_received)
                        self._data_received = self._data_received[PING_OR_PONG_OP_LEN:]
                        continue
                    # Split buffer
                    else:
                        yield None
                        continue
                elif next_byte == 73:  # "I"
                    if CRLF in self._data_received:
                        end = self._data_received.index(CRLF)
                        data = self._data_received[5:end]
                        self._data_received = self._data_received[end + CRLF_SIZE + 1 :]
                        try:
                            self._events_received.append(parse_info(data))
                        except Exception as e:
                            raise ProtocolError(next_byte, data) from e
                        continue
                    else:
                        yield None
                        continue
                elif next_byte == 43:  # "+"
                    if CRLF in self._data_received:
                        end = self._data_received.index(CRLF)
                        data = self._data_received[1:end]
                        self._data_received = self._data_received[end + CRLF_SIZE + 1 :]
                        self._events_received.append(OK_EVENT)
                        continue
                    else:
                        yield None
                        continue
                elif next_byte == 45:  # "-"
                    if CRLF in self._data_received:
                        end = self._data_received.index(CRLF)
                        msg = self._data_received[5:end].decode("ascii")
                        self._events_received.append(ErrorEvent(msg))
                        self._data_received = self._data_received[end + CRLF_SIZE :]
                        continue
                    else:
                        yield None
                        continue
                else:
                    # Anything else is an error
                    raise ProtocolError(next_byte, self._data_received)
            elif state == State3102.AWAITING_HMSG_PAYLOAD:
                assert partial_msg is not None, "pending_msg is None"
                if len(self._data_received) >= expected_total_size + CRLF_SIZE:
                    header = self._data_received[:expected_header_size]
                    if header[-4:] != STOP_HEADER:
                        raise ProtocolError(next_byte, header)
                    partial_msg.header = header[:-4]
                    payload = self._data_received[
                        expected_header_size:expected_total_size
                    ]
                    partial_msg.payload = payload
                    self._data_received = self._data_received[
                        expected_total_size + CRLF_SIZE + 1 :
                    ]
                    self._events_received.append(partial_msg)
                    state = State3102.AWAITING_CONTROL_LINE
                    continue
                else:
                    yield None
                    continue
            else:
                assert partial_msg is not None, "pending_msg is None"
                if len(self._data_received) >= expected_total_size + CRLF_SIZE:
                    partial_msg.payload = self._data_received[:expected_total_size]
                    self._data_received = self._data_received[
                        expected_total_size + CRLF_SIZE + 1 :
                    ]
                    self._events_received.append(partial_msg)
                    state = State3102.AWAITING_CONTROL_LINE
                    continue
                else:
                    yield None
                    continue


if TYPE_CHECKING:
    from .common import Parser as ParserProtocol

    # Verify that Parser implements ParserProtocol
    parser: ParserProtocol = Parser300()
