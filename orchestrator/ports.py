"""
ports.py

Port allocation backed by orchestrator DB.
- Allocates next free port in a configured range
- Avoids ports already recorded in bot_runtime
- Optionally checks OS availability (recommended)
- Designed so novnc can be toggled: once allocated, keep and reuse
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Set, Tuple
import socket

from db import DB


@dataclass(frozen=True, slots=True)
class PortRanges:
    api: Tuple[int, int] = (8001, 8999)
    vnc: Tuple[int, int] = (5901, 5999)
    novnc: Tuple[int, int] = (6081, 6999)


def _port_in_use_localhost(port: int) -> bool:
    """
    Best-effort check: is something already listening on 127.0.0.1:port
    Works well enough for host-bound ports in v1.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _reserved_ports_from_db(db: DB) -> Set[int]:
    """
    Collect all ports currently reserved in bot_runtime.
    Even if a bot is stopped, we still consider its ports reserved
    to preserve stable reuse (especially for novnc toggle).
    """
    rows = db.fetchall(
        """
        SELECT api_port, vnc_port, novnc_port
        FROM bot_runtime
        """,
        as_dict=False,
    )

    reserved: Set[int] = set()
    for (api_port, vnc_port, novnc_port) in rows:
        if api_port:
            reserved.add(int(api_port))
        if vnc_port:
            reserved.add(int(vnc_port))
        if novnc_port:
            reserved.add(int(novnc_port))
    return reserved


def _find_free_port(
    start: int,
    end: int,
    reserved: Set[int],
    check_os: bool = True,
) -> int:
    for p in range(start, end + 1):
        if p in reserved:
            continue
        if check_os and _port_in_use_localhost(p):
            continue
        return p
    raise RuntimeError(f"No free ports available in range {start}-{end}")


def allocate_ports_for_bot(
    db: DB,
    user_id: str,
    bot_name: str,
    *,
    enable_vnc: bool,
    want_novnc: bool,
    ranges: PortRanges = PortRanges(),
    check_os: bool = True,
    api_port_override: Optional[int] = None,
    vnc_port_override: Optional[int] = None,
    novnc_port_override: Optional[int] = None,
) -> dict:
    """
    Allocate ports for a bot and persist them (UPDATE bot_runtime).

    Rules:
    - If ports already exist in DB for this bot, we keep them.
    - Overrides are only used if DB has NULL for that port.
    - If enable_vnc is False, vnc_port will be set to NULL.
    - If want_novnc is False, we keep existing novnc_port (so it can be reused later),
      but we won't require it to exist.
    """

    # Load current runtime ports
    rt = db.fetchone(
        """
        SELECT api_port, vnc_port, novnc_port
        FROM bot_runtime
        WHERE bot_id = (
            SELECT id FROM bots WHERE user_id=%s AND bot_name=%s
        )
        """,
        (user_id, bot_name),
        as_dict=False,
    )
    if not rt:
        raise RuntimeError("Bot runtime not found in DB (create bot first).")

    cur_api, cur_vnc, cur_novnc = rt
    reserved = _reserved_ports_from_db(db)

    # API port
    api_port = int(cur_api) if cur_api else None
    if api_port is None:
        if api_port_override is not None:
            if api_port_override in reserved or (check_os and _port_in_use_localhost(api_port_override)):
                raise RuntimeError(f"Requested api_port {api_port_override} is not available")
            api_port = api_port_override
        else:
            api_port = _find_free_port(ranges.api[0], ranges.api[1], reserved, check_os)
        reserved.add(api_port)

    # VNC port
    vnc_port = int(cur_vnc) if cur_vnc else None
    if not enable_vnc:
        vnc_port = None
    else:
        if vnc_port is None:
            if vnc_port_override is not None:
                if vnc_port_override in reserved or (check_os and _port_in_use_localhost(vnc_port_override)):
                    raise RuntimeError(f"Requested vnc_port {vnc_port_override} is not available")
                vnc_port = vnc_port_override
            else:
                vnc_port = _find_free_port(ranges.vnc[0], ranges.vnc[1], reserved, check_os)
            reserved.add(vnc_port)

    # noVNC port (toggleable)
    novnc_port = int(cur_novnc) if cur_novnc else None
    if want_novnc and novnc_port is None:
        if novnc_port_override is not None:
            if novnc_port_override in reserved or (check_os and _port_in_use_localhost(novnc_port_override)):
                raise RuntimeError(f"Requested novnc_port {novnc_port_override} is not available")
            novnc_port = novnc_port_override
        else:
            novnc_port = _find_free_port(ranges.novnc[0], ranges.novnc[1], reserved, check_os)
        reserved.add(novnc_port)

    # Persist back to DB
    db.execute(
        """
        UPDATE bot_runtime
        SET api_port=%s, vnc_port=%s, novnc_port=%s, enable_vnc=%s
        WHERE bot_id = (
            SELECT id FROM bots WHERE user_id=%s AND bot_name=%s
        )
        """,
        (api_port, vnc_port, novnc_port, bool(enable_vnc), user_id, bot_name),
    )

    return {
        "api_port": api_port,
        "vnc_port": vnc_port,
        "novnc_port": novnc_port,
    }
