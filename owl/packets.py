"""OWL wire format and parsing of kernel timestamp control messages.

Packets (UDP payload, all integers big-endian):

    header (16 bytes)  ver:u8  type:u8  flags:u8  reserved:u8  session:u32  seq:u64
    probe              header [+ zero padding up to the requested size]
    report             header + rx_ns:u64                        (24 bytes)

A sender transmits a probe. The reflector stamps its arrival time (rx_ns) and
sends a report back to the probe's source address, echoing session and seq.
The sender computes one-way latency as rx_ns - tx_ns using its own departure
timestamp. Both clocks must be synchronized (PTP), which owl does not do itself.

flags:
    FLAG_HW   probe:  sender wants the reflector's *hardware* RX timestamp
                      (otherwise the kernel software timestamp, CLOCK_REALTIME).
              report: rx_ns is a hardware timestamp. rx_ns == 0 means the
                      reflector had no timestamp of the requested kind.

session is a random value chosen by each sender run, so reports left over from
an earlier run (or another sender sharing the port) are never mistaken for ours.

This module is pure parsing with no Linux dependency, so it can be unit-tested
anywhere.
"""

from __future__ import annotations

import socket
import struct
from typing import Iterable, NamedTuple, Optional, Tuple, Union

VERSION = 1

PROBE = 1
REPORT = 2

FLAG_HW = 0x01

_HEADER = struct.Struct("!BBBxIQ")
_U64 = struct.Struct("!Q")

HEADER_LEN = _HEADER.size  # 16
REPORT_LEN = HEADER_LEN + _U64.size  # 24
MAX_PROBE_LEN = 65507  # largest UDP payload over IPv4


class Probe(NamedTuple):
    session: int
    seq: int
    flags: int


class Report(NamedTuple):
    session: int
    seq: int
    flags: int
    rx_ns: int


def pack_probe(session: int, seq: int, flags: int = 0, size: int = 0) -> bytes:
    """Build a probe. ``size`` pads the payload with zeros (ignored if <= 16)."""
    if size > MAX_PROBE_LEN:
        raise ValueError(f"probe size {size} exceeds {MAX_PROBE_LEN} bytes")
    data = _HEADER.pack(VERSION, PROBE, flags, session, seq)
    if size > len(data):
        data += bytes(size - len(data))
    return data


def pack_report(session: int, seq: int, flags: int, rx_ns: int) -> bytes:
    return _HEADER.pack(VERSION, REPORT, flags, session, seq) + _U64.pack(rx_ns)


def unpack(data: bytes) -> Optional[Union[Probe, Report]]:
    """Decode a packet. Returns None for anything that isn't a valid OWL packet."""
    if len(data) < HEADER_LEN:
        return None
    ver, ptype, flags, session, seq = _HEADER.unpack_from(data)
    if ver != VERSION:
        return None
    if ptype == PROBE:
        return Probe(session, seq, flags)
    if ptype == REPORT and len(data) >= REPORT_LEN:
        (rx_ns,) = _U64.unpack_from(data, HEADER_LEN)
        return Report(session, seq, flags, rx_ns)
    return None


# --- Kernel timestamp control messages -----------------------------------------
#
# See Documentation/networking/timestamping.rst in the Linux source.

# SO_TIMESTAMPING (== SCM_TIMESTAMPING) is 37 on x86_64 and arm64. Python's
# socket module doesn't export it.
SCM_TIMESTAMPING = getattr(socket, "SO_TIMESTAMPING", 37)

# struct scm_timestamping { struct timespec ts[3]; } on 64-bit Linux.
# ts[0] = software timestamp (CLOCK_REALTIME), ts[1] = deprecated, ts[2] = raw
# hardware timestamp (the NIC's PTP hardware clock).
_SCM_TIMESTAMPING = struct.Struct("=qqqqqq")

# struct sock_extended_err: ee_errno:u32 ee_origin:u8 ee_type:u8 ee_code:u8
# ee_pad:u8 ee_info:u32 ee_data:u32. For TX timestamps with
# SOF_TIMESTAMPING_OPT_ID, ee_data carries the per-socket send counter.
_SOCK_EXTENDED_ERR = struct.Struct("=IBBBBII")
SO_EE_ORIGIN_TIMESTAMPING = 4
_RECVERR_CMSGS = {
    (socket.IPPROTO_IP, getattr(socket, "IP_RECVERR", 11)),
    (socket.IPPROTO_IPV6, getattr(socket, "IPV6_RECVERR", 25)),
}

Ancillary = Iterable[Tuple[int, int, bytes]]


class Timestamps(NamedTuple):
    """Kernel timestamps for one packet, in nanoseconds. 0 means "not available"."""

    sw_ns: int
    hw_ns: int


def parse_timestamping(ancdata: Ancillary) -> Optional[Timestamps]:
    """Extract the SCM_TIMESTAMPING timestamps from recvmsg() ancillary data."""
    for level, ctype, data in ancdata:
        if level == socket.SOL_SOCKET and ctype == SCM_TIMESTAMPING and len(data) >= _SCM_TIMESTAMPING.size:
            sw_s, sw_ns, _, _, hw_s, hw_ns = _SCM_TIMESTAMPING.unpack_from(data)
            return Timestamps(sw_s * 1_000_000_000 + sw_ns, hw_s * 1_000_000_000 + hw_ns)
    return None


def parse_tx_key(ancdata: Ancillary) -> Optional[int]:
    """Extract the SOF_TIMESTAMPING_OPT_ID key from an error-queue message.

    Returns None if the message isn't a timestamp notification.
    """
    for level, ctype, data in ancdata:
        if (level, ctype) in _RECVERR_CMSGS and len(data) >= _SOCK_EXTENDED_ERR.size:
            _errno, origin, _type, _code, _pad, _info, key = _SOCK_EXTENDED_ERR.unpack_from(data)
            if origin == SO_EE_ORIGIN_TIMESTAMPING:
                return key
    return None
