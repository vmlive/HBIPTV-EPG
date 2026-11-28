#!/usr/bin/env python3
"""Keep one VyOS IPTV routing table in sync with the active DHCP lease."""
import argparse
from dataclasses import dataclass
import fcntl
import grp
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
ROUTE_DESCRIPTION = "HBIPTV-managed DHCP default"
REJECT_DISTANCE = "254"
DHCP_RENEW = "/usr/libexec/vyos/op_mode/dhcp.py"
RENEW_COOLDOWN = 300
RENEW_WAIT = 480


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
    fields = {}
    try:
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key] = value.strip().strip("'")
    except FileNotFoundError:
        return None, f"DHCP lease record missing at {path}"
    if fields.get("interface") != interface:
        return None, "DHCP lease record does not identify this interface"
    reason = fields.get("reason")
    # ISC dhclient can reapply a previous lease on TIMEOUT while it is still valid.
    if reason not in ("BOUND", "RENEW", "REBIND", "REBOOT", "TIMEOUT"):
        return None, f"DHCP lease state is {reason or 'missing'}"
    try:
        if int(fields["new_expiry"]) <= now:
            return None, "DHCP lease has expired"
        address = ipaddress.IPv4Interface(f"{fields['new_ip_address']}/{fields['new_subnet_mask']}")
        routers = [ipaddress.IPv4Address(router) for router in fields.get("new_routers", "").split()]
    except KeyError as exc:
        return None, f"DHCP lease record missing field {exc.args[0]}"
    except ValueError:
        return None, "DHCP lease record contains an invalid address, subnet, router, or expiry"
    if (str(address.ip), address.network.prefixlen) not in addresses:
        current = ", ".join(f"{ip}/{prefix}" for ip, prefix in sorted(addresses)) or "none"
        return None, f"lease address {address} is not assigned (interface addresses: {current})"
    if not routers:
        return None, "DHCP lease does not advertise a router"
    for gateway in routers:
        if gateway in address.network and gateway != address.ip:
            return Lease(address, gateway), None
    advertised = ", ".join(map(str, routers))
    return None, f"DHCP routers {advertised} are not usable in {address.network}"


def interface_state(interface):
    result = run("ip", "-j", "-4", "addr", "show", "dev", interface, check=False)
    if result.returncode:
        error = result.stderr.lower()
        if any(message in error for message in ("does not exist", "cannot find device", "no such device")):
            return False, set()
        result.check_returncode()
    links = json.loads(result.stdout or "[]")
    if not links:
        return False, set()
    link = links[0]
    flags = set(link.get("flags", []))
    addresses = {(a["local"], a["prefixlen"]) for a in link.get("addr_info", []) if a["family"] == "inet"}
    return {"UP", "LOWER_UP"} <= flags, addresses


def gateway_replies(interface, lease):
    result = run("ping", "-4", "-n", "-I", interface, "-c", "2", "-W", "1", str(lease.gateway), check=False)
    if result.returncode == 0:
        return "ICMP"
    # An on-link router can respond to ARP without accepting ICMP echo requests.
    result = run(
        "arping", "-q", "-I", interface, "-s", str(lease.address.ip), "-f", "-c", "2", "-w", "3",
        str(lease.gateway), check=False,
    )
    if result.returncode not in (0, 1):
        ready, addresses = interface_state(interface)
        if not ready or (str(lease.address.ip), lease.address.network.prefixlen) not in addresses:
            return None
        raise RuntimeError(f"{interface}: ARP probe failed: {result.stderr.strip()}")
    return "ARP" if result.returncode == 0 else None


def ensure_oif_rule(interface, table, priority):
    rules = json.loads(run("ip", "-N", "-j", "-4", "rule", "show").stdout)
    at_priority = [rule for rule in rules if rule.get("priority") == priority]
    if at_priority:
        rule = at_priority[0]
        if (
            len(at_priority) == 1
            and (rule.get("oif") or rule.get("oifname")) == interface
            and str(rule.get("table")) == str(table)
            and rule.get("src", "all") == "all"
            and rule.get("dst", "all") == "all"
            and set(rule) <= {"priority", "oif", "oifname", "oif_detached", "src", "dst", "table", "flags", "protocol"}
        ):
            report(f"priority {priority}: oif {interface} already uses table {table}")
            return
        raise RuntimeError(f"policy-rule priority {priority} is already in use")
    run("ip", "-4", "rule", "add", "pref", str(priority), "oif", interface, "lookup", str(table))
    report(f"priority {priority}: oif {interface} now uses table {table}")


def active_config(path):
    from vyos.config import Config

    config = Config()
    return config.get_config_dict(path, effective=True, get_first_key=True) if config.exists_effective(path) else {}


def commit_default(path, current, interface, gateway):
    from vyos.configsession import ConfigSession, ConfigSessionError

    # Match the VyOS boot config loader before creating a native config session.
    # Commits can recreate shared config directories using the process GID and umask.
    try:
        config_group = grp.getgrnam("vyattacfg")
    except KeyError:
        raise RuntimeError("VyOS configuration group vyattacfg is missing") from None
    os.setgid(config_group.gr_gid)
    os.umask(0o002)

    try:
        session = ConfigSession(os.getpid(), app="hbiptv-dhcp-route")
        if active_config(path) != current:
            raise RuntimeError(f"table {path[3]}: default route changed while opening the configuration session")
        if current:
            session.delete(path)
        session.set([*path, "description"], ROUTE_DESCRIPTION)
        session.set([*path, "reject", "distance"], REJECT_DISTANCE)
        if gateway:
            session.set([*path, "next-hop", gateway, "interface"], interface)
        session.commit()
    except ConfigSessionError as exc:
        raise RuntimeError(f"VyOS could not commit table {path[3]}: {exc}") from exc


def configure_routes(table, interface, lease):
    path = ["protocols", "static", "table", str(table), "route", "0.0.0.0/0"]
    current = active_config(path)
    if current and current.get("description") != ROUTE_DESCRIPTION:
        raise RuntimeError(f"table {table}: found an unmanaged default route in VyOS configuration")
    gateway = str(lease.gateway) if lease else None
    target = {
        "description": ROUTE_DESCRIPTION,
        "reject": {"distance": REJECT_DISTANCE},
    }
    if gateway:
        target["next-hop"] = {gateway: {"interface": interface}}
    if current == target:
        return
    commit_default(path, current, interface, gateway)
    if gateway:
        report(f"table {table}: committed default via {gateway} dev {interface}")
    else:
        report(f"table {table}: committed reject route while waiting for DHCP")


def maintain(interface, table, state, *, check_only=False, wait_seconds=RENEW_WAIT):
    path = LEASE_DIR / f"dhclient_{interface}.lease"
    ready, addresses = interface_state(interface)
    lease, problem = read_lease(path, interface, addresses, time.time()) if ready else (None, "interface absent or link down")
    probe = gateway_replies(interface, lease) if lease else None
    lease_changed = False
    if lease:
        # Recheck after even a failed probe, since DHCP can replace the link mid-probe.
        ready, addresses = interface_state(interface)
        validated = read_lease(path, interface, addresses, time.time())[0] if ready else None
        lease_changed = validated != lease
    if probe and not lease_changed:
        if check_only:
            report(f"{interface}: {lease.address}, DHCP gateway {lease.gateway} replied to {probe}")
        else:
            configure_routes(table, interface, lease)
        return 0

    if lease_changed:
        problem = "interface or DHCP lease changed during the gateway probe"
    elif lease:
        problem = f"DHCP gateway {lease.gateway} did not reply to ICMP or ARP (lease address {lease.address})"
    if check_only:
        report(f"{interface}: {problem}", syslog.LOG_WARNING)
        return 1

    configure_routes(table, interface, None)
    last_renew = float(state.read() or "-inf")
    now = time.monotonic()
    if lease_changed or not ready:
        report(f"{interface}: {problem}", syslog.LOG_WARNING)
    elif not addresses and run(
        "systemctl", "show", "--property=ActiveState", "--value", f"dhclient@{interface}.service", check=False
    ).stdout.strip() in ("active", "activating", "reloading"):
        report(f"{interface}: DHCP client is acquiring an address")
    elif now - last_renew < RENEW_COOLDOWN:
        report(f"{interface}: {problem}, waiting during DHCP restart cooldown", syslog.LOG_WARNING)
    else:
        state.seek(0)
        state.truncate()
        state.write(str(now))
        state.flush()
        report(f"{interface}: {problem}, requesting VyOS DHCP renewal", syslog.LOG_WARNING)
        try:
            run(DHCP_RENEW, "renew_client_lease", "--family", "inet", "--interface", interface, timeout=30)
        except subprocess.TimeoutExpired:
            report(f"{interface}: DHCP renewal command timed out, checking for a new lease", syslog.LOG_WARNING)

    deadline = time.monotonic() + wait_seconds
    while (remaining := deadline - time.monotonic()) > 0:
        time.sleep(min(5, remaining))
        ready, addresses = interface_state(interface)
        lease, _ = read_lease(path, interface, addresses, time.time()) if ready else (None, None)
        if lease and gateway_replies(interface, lease):
            ready, addresses = interface_state(interface)
            if ready and read_lease(path, interface, addresses, time.time())[0] == lease:
                configure_routes(table, interface, lease)
                return 0

    report(f"{interface}: no usable DHCP lease or gateway before the preparation deadline", syslog.LOG_WARNING)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", default="eth0.99")
    parser.add_argument("--table", type=int, default=101)
    parser.add_argument("--wait-seconds", type=int, default=RENEW_WAIT, help="maximum time to wait for a usable lease")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="check the lease and gateway without changing routes or restarting DHCP")
    mode.add_argument("--ensure-oif-rule", type=int, metavar="PRIORITY", help="restore the output-interface policy rule only")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", args.interface):
        parser.error("invalid interface name")
    if not 1 <= args.table <= 200:
        parser.error("table must be between 1 and 200")
    if args.wait_seconds <= 0:
        parser.error("--wait-seconds must be positive")
    if args.ensure_oif_rule is not None and not 1 <= args.ensure_oif_rule <= 32765:
        parser.error("policy-rule priority must be between 1 and 32765")
    if os.geteuid() != 0:
        parser.error("run as root on the VyOS host")

    syslog.openlog("hbiptv-dhcp-route")
    try:
        if args.check:
            return maintain(args.interface, args.table, None, check_only=True)
        if args.ensure_oif_rule is not None:
            ensure_oif_rule(args.interface, args.table, args.ensure_oif_rule)
            return 0
        STATE_DIR.mkdir(mode=0o700, exist_ok=True)
        with (STATE_DIR / f"table-{args.table}.state").open("a+") as state:
            try:
                fcntl.flock(state, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            state.seek(0)
            return maintain(args.interface, args.table, state, wait_seconds=args.wait_seconds)
    except subprocess.CalledProcessError as exc:
        report(f"{exc.cmd[0]} exited with {exc.returncode}: {exc.stderr.strip()}", syslog.LOG_ERR)
    except (ImportError, OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        report(str(exc), syslog.LOG_ERR)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
