#!/usr/bin/env python3
"""Keep one VyOS IPTV routing table in sync with the active DHCP lease."""
import argparse
from dataclasses import dataclass
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import syslog
import time


LEASE_DIR = Path("/run/dhclient")
STATE_DIR = Path("/run/hbiptv-dhcp-route")
PROTOCOL = 242
DEFAULT_METRIC = 100
REJECT_METRIC = 32760
RENEW_COOLDOWN = 300
RENEW_WAIT = 25


@dataclass(frozen=True)
class Lease:
    address: ipaddress.IPv4Interface
    gateway: ipaddress.IPv4Address


def run(*command, check=True, timeout=8):
    return subprocess.run(
        command, check=check, timeout=timeout, capture_output=True, text=True,
        env={**os.environ, "LC_ALL": "C"},
    )


def report(message, priority=syslog.LOG_INFO):
    print(message, flush=True)
    syslog.syslog(priority, message)


def read_lease(path, interface, addresses, now):
    try:
        fields = {}
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key] = value.strip().strip("'")
        if fields["interface"] != interface or fields["reason"] not in ("BOUND", "RENEW", "REBIND", "REBOOT"):
            return None
        if int(fields["new_expiry"]) <= now:
            return None
        address = ipaddress.IPv4Interface(f"{fields['new_ip_address']}/{fields['new_subnet_mask']}")
        if (str(address.ip), address.network.prefixlen) not in addresses:
            return None
        for router in fields.get("new_routers", "").split():
            gateway = ipaddress.IPv4Address(router)
            if gateway in address.network and gateway != address.ip:
                return Lease(address, gateway)
    except (FileNotFoundError, KeyError, ValueError):
        return None
    return None


def interface_state(interface):
    links = json.loads(run("ip", "-j", "-4", "addr", "show", "dev", interface).stdout)
    if not links:
        raise RuntimeError(f"{interface}: interface missing")
    link = links[0]
    flags = set(link.get("flags", []))
    addresses = {(a["local"], a["prefixlen"]) for a in link.get("addr_info", []) if a["family"] == "inet"}
    return {"UP", "LOWER_UP"} <= flags, addresses


def gateway_replies(interface, lease):
    result = run("ping", "-4", "-n", "-I", interface, "-c", "2", "-W", "1", str(lease.gateway), check=False)
    if result.returncode == 2:
        raise RuntimeError(f"{interface}: ping could not run: {result.stderr.strip()}")
    return result.returncode == 0


def table_defaults(table):
    result = run("ip", "-j", "-4", "route", "show", "table", str(table), "default", check=False)
    if result.returncode:
        if "FIB table does not exist" in result.stderr:
            return []
        result.check_returncode()
    return json.loads(result.stdout or "[]")


def owned(route):
    return str(route.get("protocol")) == str(PROTOCOL)


def configure_routes(table, interface, lease):
    routes = table_defaults(table)
    if any(not owned(route) for route in routes):
        raise RuntimeError(f"table {table}: found an unmanaged default route")
    base = ("ip", "-4", "route")
    tag = ("table", str(table), "proto", str(PROTOCOL))
    if not any(route.get("type") == "unreachable" and route.get("metric") == REJECT_METRIC for route in routes):
        run(*base, "replace", "unreachable", "default", *tag, "metric", str(REJECT_METRIC))
    active = [route for route in routes if route.get("metric") == DEFAULT_METRIC]
    if lease is None:
        if active:
            run(*base, "del", "default", *tag, "metric", str(DEFAULT_METRIC))
        return
    gateway = str(lease.gateway)
    source = str(lease.address.ip)
    if any(
        route.get("gateway") == gateway and route.get("dev") == interface and route.get("prefsrc") == source
        for route in active
    ):
        return
    run(*base, "replace", "default", "via", gateway, "dev", interface, "src", source,
        *tag, "metric", str(DEFAULT_METRIC))
    report(f"table {table}: default via {gateway} dev {interface} src {source}")


def lease_mtime(path):
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def maintain(interface, table, state, *, check_only=False):
    path = LEASE_DIR / f"dhclient_{interface}.lease"
    ready, addresses = interface_state(interface)
    lease = read_lease(path, interface, addresses, time.time()) if ready else None
    if lease and gateway_replies(interface, lease):
        # The interface or lease can change while ping is in progress.
        ready, addresses = interface_state(interface)
        validated = read_lease(path, interface, addresses, time.time()) if ready else None
        if validated != lease:
            if not check_only:
                configure_routes(table, interface, None)
            report(f"{interface}: DHCP changed during the gateway probe, retry on the next run")
            return 1
        if check_only:
            report(f"{interface}: {lease.address}, DHCP gateway {lease.gateway} replied")
        else:
            configure_routes(table, interface, lease)
        return 0

    if check_only:
        report(f"{interface}: no active DHCP lease with a responding gateway", syslog.LOG_WARNING)
        return 1

    configure_routes(table, interface, None)
    if not ready:
        report(f"{interface}: link down, waiting for carrier", syslog.LOG_WARNING)
        return 1

    last_renew = float(state.read() or "-inf")
    now = time.monotonic()
    if now - last_renew < RENEW_COOLDOWN:
        report(f"{interface}: gateway unavailable, DHCP restart cooling down", syslog.LOG_WARNING)
        return 1

    mtime = lease_mtime(path)
    state.seek(0)
    state.truncate()
    state.write(str(now))
    state.flush()
    report(f"{interface}: gateway unavailable, restarting DHCP client", syslog.LOG_WARNING)
    run("systemctl", "restart", "--no-block", f"dhclient@{interface}.service")

    deadline = time.monotonic() + RENEW_WAIT
    while time.monotonic() < deadline:
        time.sleep(2)
        if (new_mtime := lease_mtime(path)) is None or new_mtime == mtime:
            continue
        ready, addresses = interface_state(interface)
        lease = read_lease(path, interface, addresses, time.time()) if ready else None
        if lease and gateway_replies(interface, lease) and lease_mtime(path) == new_mtime:
            ready, addresses = interface_state(interface)
            if ready and read_lease(path, interface, addresses, time.time()) == lease:
                configure_routes(table, interface, lease)
                return 0

    report(f"{interface}: no usable lease after DHCP restart, retry on the next run", syslog.LOG_WARNING)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", default="eth0.99")
    parser.add_argument("--table", type=int, default=101)
    parser.add_argument("--check", action="store_true", help="check the lease and gateway without changing routes or restarting DHCP")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", args.interface):
        parser.error("invalid interface name")
    if not 1 <= args.table <= 200:
        parser.error("table must be between 1 and 200")
    if os.geteuid() != 0:
        parser.error("run as root on the VyOS host")

    syslog.openlog("hbiptv-dhcp-route")
    try:
        if args.check:
            return maintain(args.interface, args.table, None, check_only=True)
        STATE_DIR.mkdir(mode=0o700, exist_ok=True)
        with (STATE_DIR / f"table-{args.table}.state").open("a+") as state:
            try:
                fcntl.flock(state, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            state.seek(0)
            return maintain(args.interface, args.table, state)
    except subprocess.CalledProcessError as exc:
        report(f"{exc.cmd[0]} exited with {exc.returncode}: {exc.stderr.strip()}", syslog.LOG_ERR)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        report(str(exc), syslog.LOG_ERR)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
