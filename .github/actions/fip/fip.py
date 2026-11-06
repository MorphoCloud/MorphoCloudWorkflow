"""Floating IPs, attached and detached without races (AUTOMATION_AUDIT_2026-11.md,
class 1).

Every taker on every runner used to pick an unattached address, then attach
it minutes later; nothing owned it in between, so two takers could pick the
same one, and the one that lost could even believe the other's attachment was
its own (Test-Instances#523 and audit findings 1a-1i). Here an address is
attached with a compare-and-swap on Neutron's revision number
(`revision-if-match`): "attach it to my port only if nobody changed it since I
read it". The loser gets HTTP 412 and tries the next address. Success is
decided only by a fresh read showing the address on this server's port, and a
detach only ever touches addresses on this server's port.

Usage (from a workflow step, with the runner's venv active and OS_CLOUD set):

    python fip.py ensure-spare
    python fip.py attach --server NAME [--prefer ADDRESS] [--deadline SECONDS]
    python fip.py detach --server NAME

`attach` prints `floating_ip_address=...` and `floating_ip_uuid=...` lines for
$GITHUB_OUTPUT. Exit status: 0 done, 2 Jetstream2 has no routable address,
3 the server or its port does not exist, 1 anything else.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

ROUTABLE_PREFIX = "149.165."
# Every conditional call passes the address's id, never a resource object:
# openstacksdk sends only changed headers, so a reused object with the same
# revision silently drops If-Match and Neutron accepts a stale write (seen on
# Jetstream2, 2026-11-06). A string id builds a fresh request every time.
MINT_DESCRIPTION = "MorphoCloud"
EXTERNAL_NETWORK = "public"
EXIT_NO_ADDRESS = 2
EXIT_NO_SERVER = 3
MAX_MINTS_PER_ATTACH = 2
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_PRECONDITION_FAILED = 412
NO_ROUTABLE = "Jetstream2 has no routable address"


class NoAddressError(Exception):
    """Jetstream2's public pool has no routable address for us."""


class NoServerError(Exception):
    """The server, or its port, does not exist."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)  # noqa: T201 — stdout carries the outputs


def routable(ip) -> bool:
    return str(ip.floating_ip_address or "").startswith(ROUTABLE_PREFIX)


def status_of(exc: Exception) -> int | None:
    return getattr(exc, "status_code", None)


def server_port(conn, name: str):
    """The server's port (the one its floating IP binds to)."""
    server = conn.compute.find_server(name, ignore_missing=True)
    if server is None:
        msg = f"no server named {name}"
        raise NoServerError(msg)
    ports = list(conn.network.ports(device_id=server.id))
    if not ports:
        msg = f"{name} has no port yet"
        raise NoServerError(msg)
    # One port per MorphoCloud instance; a compute port first if ever more.
    ports.sort(
        key=lambda p: (not str(p.device_owner or "").startswith("compute:"), p.id)
    )
    return server, ports[0]


def mine(conn, port_id: str):
    """A routable address already on this port, if any."""
    for ip in conn.network.ips(port_id=port_id):
        if routable(ip):
            return ip
    return None


def mint(conn, tries: int = 2):
    """One new routable address, unattached; None when the pool is dry.
    A non-routable answer (the pool is exhausted, decision 24) is deleted."""
    for attempt in range(1, tries + 1):
        try:
            ip = conn.network.create_ip(
                floating_network_id=external_network_id(conn),
                description=MINT_DESCRIPTION,
            )
        except Exception as e:
            log(f"mint failed (attempt {attempt}): {e}")
            continue
        if routable(ip):
            log(f"minted {ip.floating_ip_address}")
            return ip
        log(f"Jetstream2 handed out non-routable {ip.floating_ip_address}; deleting it")
        try:
            conn.network.delete_ip(ip.id, ignore_missing=True)
        except Exception as e:
            log(f"could not delete {ip.floating_ip_address}: {e}")
    return None


_EXTERNAL = {}


def external_network_id(conn) -> str:
    if "id" not in _EXTERNAL:
        net = conn.network.find_network(EXTERNAL_NETWORK, ignore_missing=False)
        _EXTERNAL["id"] = net.id
    return _EXTERNAL["id"]


def try_bind(conn, ip, port_id: str) -> str:
    """One compare-and-swap attach. "mine": it is on my port now. "taken":
    someone else changed or removed it; move on. "retry": the call failed and
    nothing changed (Jetstream2's transient errors); try it again."""
    try:
        conn.network.update_ip(ip.id, if_revision=ip.revision_number, port_id=port_id)
    except Exception as e:
        code = status_of(e)
        if code in (HTTP_NOT_FOUND, HTTP_CONFLICT, HTTP_PRECONDITION_FAILED):
            return "taken"
        log(
            f"attach {ip.floating_ip_address} answered {code or e}; checking what applied"
        )
        time.sleep(2)
    fresh = conn.network.get_ip(ip.id) if ip.id else None
    if fresh is None:
        return "taken"
    if fresh.port_id == port_id:
        return "mine"
    if fresh.port_id is None:
        return "retry"
    return "taken"


def get_or_none(conn, ip_id: str):
    try:
        return conn.network.get_ip(ip_id)
    except Exception as e:
        if status_of(e) == HTTP_NOT_FOUND:
            return None
        raise


def attach(conn, name: str, prefer: str = "", deadline: float = 300, sleep=time.sleep):
    """Attach a routable address to the server's port and return it, as read
    back from Neutron. Idempotent: an address already on the port is kept."""
    _, port = server_port(conn, name)
    have = mine(conn, port.id)
    if have is not None:
        log(f"{name} already has {have.floating_ip_address}")
        return have
    # A dead (non-routable) address on the port would make every attach fail
    # with a conflict: it never reaches anyone, so it is released first.
    for ip in list(conn.network.ips(port_id=port.id)):
        if not routable(ip):
            release(conn, ip, port.id, name, sleep)
    start = time.monotonic()
    taken: set[str] = set()
    minted = [0]
    while True:
        got = bind_first(
            conn, candidates(conn, taken, prefer, minted, name), port.id, name, taken
        )
        if got is not None:
            return got
        if time.monotonic() - start >= deadline:
            msg = f"no address attached to {name} within {deadline:.0f}s"
            raise TimeoutError(msg)
        sleep(5)


def candidates(conn, taken: set, prefer: str, minted: list, name: str) -> list:
    """Unattached routable addresses not yet lost to another actor, the
    preferred one first; a new one when there are none. `minted` counts this
    attach's mints: never more than MAX_MINTS_PER_ATTACH."""
    # Filtered here, not by a query: the unattached ones are those with no
    # port in a fresh listing.
    free = [
        ip
        for ip in conn.network.ips()
        if ip.port_id is None and routable(ip) and ip.id not in taken
    ]
    # The preferred address first (the unshelve's old one), then id order for
    # a stable choice.
    free.sort(key=lambda ip: (ip.floating_ip_address != prefer, ip.id))
    if free:
        return free
    if minted[0] >= MAX_MINTS_PER_ATTACH:
        # Minted ones that would not attach stay as spares; something other
        # than the pool is refusing. Never mint in a loop.
        msg = f"{minted[0]} new addresses would not attach to {name}"
        raise RuntimeError(msg)
    minted[0] += 1
    ip = mint(conn)
    if ip is None:
        raise NoAddressError(NO_ROUTABLE)
    return [ip]


def bind_first(conn, free: list, port_id: str, name: str, taken: set):
    """The first of `free` this call attaches to the port, read back; None to
    list again (one was taken, or a call failed and nothing applied)."""
    for ip in free:
        result = try_bind(conn, ip, port_id)
        if result == "mine":
            fresh = get_or_none(conn, ip.id)
            if fresh is not None and fresh.port_id == port_id:
                log(f"attached {fresh.floating_ip_address} to {name}")
                return fresh
            taken.add(ip.id)
        elif result == "taken":
            log(f"{ip.floating_ip_address} was taken by another actor; next")
            taken.add(ip.id)
        else:
            return None  # re-list and try again after a pause
    return None


def detach(conn, name: str, sleep=time.sleep) -> list[str]:
    """Detach every address on this server's port, and only those. Returns the
    addresses released. A server already gone has nothing to detach."""
    try:
        _, port = server_port(conn, name)
    except NoServerError as e:
        log(f"nothing to detach: {e}")
        return []
    released = []
    for ip in list(conn.network.ips(port_id=port.id)):
        if release(conn, ip, port.id, name, sleep):
            released.append(ip.floating_ip_address)
    return released


def release(conn, ip, port_id: str, name: str, sleep=time.sleep) -> bool:
    """Detach one address, only while it is still on this port. True when
    this call detached it; False when it was already gone or not ours."""
    for _ in range(5):
        fresh = get_or_none(conn, ip.id)
        if fresh is None or fresh.port_id != port_id:
            return False  # gone, or no longer ours: nothing of ours to detach
        try:
            conn.network.update_ip(
                fresh.id, if_revision=fresh.revision_number, port_id=None
            )
        except Exception as e:
            log(
                f"detach {fresh.floating_ip_address} answered {status_of(e) or e}; re-reading"
            )
            sleep(2)
            continue
        log(f"detached {fresh.floating_ip_address} from {name}")
        return True
    msg = f"could not detach {ip.floating_ip_address} from {name}"
    raise RuntimeError(msg)


def ensure_spare(conn) -> None:
    """At least one routable unattached address exists (decision 24), so a
    create fails fast on a dry pool. Reserves nothing."""
    if any(ip.port_id is None and routable(ip) for ip in conn.network.ips()):
        return
    if mint(conn) is None:
        raise NoAddressError(NO_ROUTABLE)


def keep_one_in_hand(conn) -> None:
    """After taking an address, leave one spare for the next start (decision
    24). Best effort: the policing script is the backstop."""
    if any(ip.port_id is None and routable(ip) for ip in conn.network.ips()):
        return
    if mint(conn, tries=1) is None:
        log(
            "::warning ::could not mint a spare (pool dry?); the policing script keeps trying"
        )


def connect():
    import openstack  # noqa: PLC0415 — the runner's venv; tests pass a fake

    return openstack.connect(cloud=os.environ["OS_CLOUD"])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ensure-spare")
    a = sub.add_parser("attach")
    a.add_argument("--server", required=True)
    a.add_argument("--prefer", default="")
    a.add_argument("--deadline", type=float, default=300)
    d = sub.add_parser("detach")
    d.add_argument("--server", required=True)
    args = parser.parse_args(argv)
    if not os.environ.get("OS_CLOUD"):
        log("OS_CLOUD is not set")
        return 1
    conn = connect()
    try:
        if args.cmd == "ensure-spare":
            ensure_spare(conn)
        elif args.cmd == "attach":
            ip = attach(conn, args.server, args.prefer, args.deadline)
            print(f"floating_ip_address={ip.floating_ip_address}")  # noqa: T201
            print(f"floating_ip_uuid={ip.id}")  # noqa: T201
            keep_one_in_hand(conn)
        else:
            detach(conn, args.server)
    except NoAddressError as e:
        log(f"::error ::{e}")
        return EXIT_NO_ADDRESS
    except NoServerError as e:
        log(f"::error ::{e}")
        return EXIT_NO_SERVER
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
