"""Linux socket plumbing: SO_TIMESTAMPING, the NIC timestamp filter, and TX
timestamps from the socket error queue.

Reference: Documentation/networking/timestamping.rst in the Linux source.
"""

from __future__ import annotations

import array
import fcntl
import ipaddress
import json
import socket
import struct
import subprocess
from typing import Optional, Tuple

from .packets import Timestamps, parse_timestamping, parse_tx_key

# linux/net_tstamp.h
SOF_TIMESTAMPING_TX_HARDWARE = 1 << 0
SOF_TIMESTAMPING_TX_SOFTWARE = 1 << 1
SOF_TIMESTAMPING_RX_HARDWARE = 1 << 2
SOF_TIMESTAMPING_RX_SOFTWARE = 1 << 3
SOF_TIMESTAMPING_SOFTWARE = 1 << 4
SOF_TIMESTAMPING_RAW_HARDWARE = 1 << 6
SOF_TIMESTAMPING_OPT_ID = 1 << 7
SOF_TIMESTAMPING_TX_SCHED = 1 << 8
SOF_TIMESTAMPING_OPT_TSONLY = 1 << 11

HWTSTAMP_TX_ON = 1
HWTSTAMP_FILTER_ALL = 1

# linux/sockios.h
SIOCSHWTSTAMP = 0x89B0
SIOCGHWTSTAMP = 0x89B1

SO_TIMESTAMPING = getattr(socket, "SO_TIMESTAMPING", 37)
MSG_ERRQUEUE = getattr(socket, "MSG_ERRQUEUE", 0x2000)

_IFREQ_SIZE = 40  # sizeof(struct ifreq) on 64-bit Linux
_RECV_BUFSIZE = 65535
_ANC_BUFSIZE = 512

# Every received packet gets both a software and a raw hardware timestamp.
# The hardware one is non-zero only if the NIC's RX filter covers the packet
# (see enable_hw_timestamping).
_RX_FLAGS = (
    SOF_TIMESTAMPING_RX_SOFTWARE
    | SOF_TIMESTAMPING_RX_HARDWARE
    | SOF_TIMESTAMPING_SOFTWARE
    | SOF_TIMESTAMPING_RAW_HARDWARE
)

TxToken = Tuple[int, int]  # (generation, OPT_ID key)


# --- NIC hardware timestamp configuration ---------------------------------------


def _hwtstamp_ioctl(iface: str, request: int, cfg: array.array) -> None:
    """Issue SIOC[GS]HWTSTAMP. ``cfg`` is struct hwtstamp_config {flags, tx_type, rx_filter}.

    struct ifreq holds a pointer to the config, so pass the address of a buffer
    the kernel can read and write in place.
    """
    name = iface.encode()
    if len(name) >= 16:
        raise ValueError(f"interface name too long: {iface!r}")
    addr, _ = cfg.buffer_info()
    ifr = struct.pack("16sP", name, addr).ljust(_IFREQ_SIZE, b"\0")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        fcntl.ioctl(s.fileno(), request, ifr)


def get_hwtstamp(iface: str) -> Tuple[int, int]:
    """Return the NIC's current (tx_type, rx_filter)."""
    cfg = array.array("i", [0, 0, 0])
    _hwtstamp_ioctl(iface, SIOCGHWTSTAMP, cfg)
    return cfg[1], cfg[2]


def enable_hw_timestamping(iface: str) -> int:
    """Turn on NIC TX timestamps and RX timestamps for all packets on ``iface``.

    Without this, ptp4l's narrower filter (PTP event packets only) means OWL's
    UDP probes get no hardware timestamp. FILTER_ALL is a superset, so ptp4l
    keeps working. This setting applies to the whole NIC, not just our socket,
    and needs CAP_NET_ADMIN. Returns the rx_filter the driver actually applied.
    """
    try:
        tx_type, rx_filter = get_hwtstamp(iface)
        if tx_type == HWTSTAMP_TX_ON and rx_filter == HWTSTAMP_FILTER_ALL:
            return rx_filter
    except OSError:
        pass  # some drivers don't implement SIOCGHWTSTAMP; just try to set it
    cfg = array.array("i", [0, HWTSTAMP_TX_ON, HWTSTAMP_FILTER_ALL])
    _hwtstamp_ioctl(iface, SIOCSHWTSTAMP, cfg)
    return cfg[2]


# linux/ethtool.h: struct ethtool_ts_info, which `ethtool -T` prints
SIOCETHTOOL = 0x8946
ETHTOOL_GET_TS_INFO = 0x41
_TS_INFO_WORDS = 11  # cmd, so_timestamping, phc_index, tx_types, tx_reserved[3], rx_filters, rx_reserved[3]


def phc_index(iface: str) -> Optional[int]:
    """Return N for the NIC's PTP hardware clock /dev/ptpN, or None if it has none."""
    name = iface.encode()
    if len(name) >= 16:
        raise ValueError(f"interface name too long: {iface!r}")
    info = array.array("i", [ETHTOOL_GET_TS_INFO] + [0] * (_TS_INFO_WORDS - 1))
    addr, _ = info.buffer_info()
    ifr = struct.pack("16sP", name, addr).ljust(_IFREQ_SIZE, b"\0")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        fcntl.ioctl(s.fileno(), SIOCETHTOOL, ifr)
    return info[2] if info[2] >= 0 else None


# --- Address and interface lookup --------------------------------------------------


def source_sockaddr_for(peer_sockaddr: tuple, family: int) -> tuple:
    """Return the local sockaddr (port 0) the kernel would use to reach ``peer_sockaddr``."""
    with socket.socket(family, socket.SOCK_DGRAM) as s:
        s.connect(peer_sockaddr)  # UDP connect sends nothing; it only picks a route
        local = s.getsockname()
    return (local[0], 0) + tuple(local[2:])


def iface_for_addr(addr: str) -> Optional[str]:
    """Return the name of the interface that owns local address ``addr``, if any."""
    target = ipaddress.ip_address(addr.split("%", 1)[0])
    try:
        out = subprocess.run(
            ["ip", "-j", "addr", "show"], check=True, capture_output=True, text=True
        ).stdout
        links = json.loads(out)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None
    for link in links:
        for info in link.get("addr_info", []):
            try:
                if ipaddress.ip_address(info.get("local", "")) == target:
                    return link.get("ifname")
            except ValueError:
                continue
    return None


# --- Timestamped socket ---------------------------------------------------------------


class TimestampedSocket:
    """Non-blocking UDP socket that timestamps every packet in the kernel.

    RX: recv() returns each datagram with its software and hardware arrival times.

    TX (optional): the kernel puts each packet's departure time on the socket's
    error queue, tagged with a per-socket send counter (SOF_TIMESTAMPING_OPT_ID).
    send() returns a token for that counter value, and recv_tx_timestamp()
    returns the same token, so timestamps can be matched to packets regardless
    of the order they come back in.

    ``hw`` selects which TX timestamp is generated: raw hardware, or software.
    The software one is SOF_TIMESTAMPING_TX_SCHED, taken by the kernel as the
    packet enters the device's queue, not TX_SOFTWARE, taken by the driver
    just before the NIC sends it. TX_SOFTWARE depends on the driver calling
    skb_tx_timestamp(), and some don't: ConnectX VFs (mlx5_core) on FABRIC
    never produce it. TX_SCHED works on every device, so sw mode means the
    same thing everywhere. It is earlier by the time spent in the qdisc and
    driver, usually a few µs, which is small next to the software RX timestamp's
    own interrupt and softirq latency.
    """

    def __init__(
        self,
        family: int,
        bind_sockaddr: tuple,
        *,
        tx_timestamps: bool = False,
        hw: bool = False,
    ) -> None:
        self.family = family
        self._tx_flags = 0
        if tx_timestamps:
            self._tx_flags = (
                (SOF_TIMESTAMPING_TX_HARDWARE if hw else SOF_TIMESTAMPING_TX_SCHED)
                | SOF_TIMESTAMPING_OPT_ID
                | SOF_TIMESTAMPING_OPT_TSONLY  # don't loop the payload back with the timestamp
            )
        self._generation = 0
        self._next_key = 0
        self.sock = socket.socket(family, socket.SOCK_DGRAM)
        try:
            if family == socket.AF_INET6:
                self.sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            self.sock.bind(bind_sockaddr)
            self._enable_timestamping()
            self.sock.setblocking(False)
        except BaseException:
            self.sock.close()
            raise

    def fileno(self) -> int:
        return self.sock.fileno()

    @property
    def sockname(self) -> tuple:
        return self.sock.getsockname()

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> "TimestampedSocket":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _enable_timestamping(self) -> None:
        self.sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPING, _RX_FLAGS | self._tx_flags)
        self._next_key = 0

    def _resync_tx_keys(self) -> None:
        """Get our send counter back in step with the kernel's after a failed send.

        The kernel increments its OPT_ID counter part-way through sending. When
        sendto() fails we can't tell whether that happened, and being off by one
        would pair every later probe with the wrong timestamp. Turning
        timestamping off and on resets the kernel counter to 0. Starting a new
        generation means tokens issued before the reset never match.
        """
        self.sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPING, 0)
        while self.recv_tx_timestamp() is not None:
            pass
        self._generation += 1
        self._enable_timestamping()

    def send(self, data: bytes, sockaddr: tuple) -> Optional[TxToken]:
        """Send a datagram. Returns its TX-timestamp token (None if TX timestamps are off).

        Raises OSError if the send fails.
        """
        try:
            self.sock.sendto(data, sockaddr)
        except OSError:
            if self._tx_flags:
                self._resync_tx_keys()
            raise
        if not self._tx_flags:
            return None
        token = (self._generation, self._next_key)
        self._next_key = (self._next_key + 1) & 0xFFFFFFFF
        return token

    def recv(self) -> Optional[Tuple[bytes, tuple, Optional[Timestamps]]]:
        """Read one datagram: (data, source sockaddr, timestamps). None if nothing is waiting."""
        try:
            data, anc, _flags, addr = self.sock.recvmsg(_RECV_BUFSIZE, _ANC_BUFSIZE)
        except (BlockingIOError, InterruptedError):
            return None
        return data, addr, parse_timestamping(anc)

    def recv_tx_timestamp(self) -> Optional[Tuple[Optional[TxToken], Optional[Timestamps]]]:
        """Pop one entry from the error queue: (token, timestamps). None if the queue is empty.

        The token is None for entries that aren't TX timestamps.
        """
        try:
            _data, anc, _flags, _addr = self.sock.recvmsg(_RECV_BUFSIZE, _ANC_BUFSIZE, MSG_ERRQUEUE)
        except (BlockingIOError, InterruptedError):
            return None
        key = parse_tx_key(anc)
        token = None if key is None else (self._generation, key)
        return token, parse_timestamping(anc)
