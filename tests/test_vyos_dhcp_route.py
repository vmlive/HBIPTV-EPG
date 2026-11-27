import importlib.util
import io
import ipaddress
from pathlib import Path
from subprocess import CompletedProcess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import call, patch


script = Path(__file__).resolve().parents[1] / "scripts/vyos-dhcp-route.py"
spec = importlib.util.spec_from_file_location("vyos_dhcp_route", script)
route = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = route
spec.loader.exec_module(route)

INTERFACE = "eth0.99"
OLD = route.Lease(ipaddress.IPv4Interface("192.0.2.2/24"), ipaddress.IPv4Address("192.0.2.1"))
NEW = route.Lease(ipaddress.IPv4Interface("198.51.100.2/24"), ipaddress.IPv4Address("198.51.100.1"))


class DhcpRouteTest(unittest.TestCase):
    def test_uncreated_routing_table(self):
        missing = CompletedProcess([], 2, "[\n", "Error: ipv4: FIB table does not exist.")
        with patch.object(route, "run", return_value=missing):
            self.assertEqual(route.table_defaults(101), [])

    def test_lease_is_current_and_assigned(self):
        with TemporaryDirectory() as directory:
            lease_file = Path(directory) / "dhclient_eth0.99.lease"
            lease_file.write_text(
                "Fri Nov 27 08:00:00 CST 2026\n"
                "reason='RENEW'\ninterface='eth0.99'\nnew_ip_address='198.51.100.2'\n"
                "new_subnet_mask='255.255.255.0'\nnew_routers='192.0.2.1 198.51.100.1'\n"
                "new_expiry='200'\n"
            )
            self.assertEqual(route.read_lease(lease_file, INTERFACE, {("198.51.100.2", 24)}, 100), (NEW, None))
            lease, reason = route.read_lease(lease_file, INTERFACE, {("192.0.2.2", 24)}, 100)
            self.assertIsNone(lease)
            self.assertIn("not assigned", reason)
            self.assertEqual(route.read_lease(lease_file, INTERFACE, {("198.51.100.2", 24)}, 200),
                             (None, "DHCP lease has expired"))
            lease_file.write_text(lease_file.read_text().replace("'RENEW'", "'TIMEOUT'"))
            self.assertEqual(route.read_lease(lease_file, INTERFACE, {("198.51.100.2", 24)}, 100), (NEW, None))

    def test_gateway_replies_to_arp_after_ping_fails(self):
        with patch.object(
            route, "run", side_effect=[CompletedProcess([], 1), CompletedProcess([], 0)]
        ) as run:
            self.assertEqual(route.gateway_replies(INTERFACE, NEW), "ARP")
            run.assert_has_calls([
                call("ping", "-4", "-n", "-I", INTERFACE, "-c", "2", "-W", "1", "198.51.100.1", check=False),
                call("arping", "-q", "-I", INTERFACE, "-s", "198.51.100.2", "-f", "-c", "2", "-w", "3",
                     "198.51.100.1", check=False),
            ])

    def test_arp_reply_is_accepted_without_restarting_dhcp(self):
        with (
            patch.object(route, "interface_state", return_value=(True, set())),
            patch.object(route, "read_lease", return_value=(NEW, None)),
            patch.object(route, "gateway_replies", return_value="ARP"),
            patch.object(route, "configure_routes") as configure,
            patch.object(route, "run") as run,
        ):
            self.assertEqual(route.maintain(INTERFACE, 101, io.StringIO()), 0)
            configure.assert_called_once_with(101, INTERFACE, NEW)
            run.assert_not_called()

    def test_check_prints_probe_failure_without_restarting_dhcp_or_changing_routes(self):
        with (
            patch.object(route, "interface_state", return_value=(True, set())),
            patch.object(route, "read_lease", return_value=(NEW, None)),
            patch.object(route, "gateway_replies", return_value=None),
            patch.object(route, "configure_routes") as configure,
            patch.object(route, "run") as run,
            patch.object(route, "report") as report,
        ):
            self.assertEqual(route.maintain(INTERFACE, 101, None, check_only=True), 1)
            self.assertIn("198.51.100.1 did not reply to ICMP or ARP", report.call_args.args[0])
            configure.assert_not_called()
            run.assert_not_called()

    def test_changed_gateway_replaces_only_selected_table(self):
        reject = {"type": "unreachable", "protocol": "242", "metric": 32760}
        old_default = {
            "protocol": "242", "metric": 100, "dev": INTERFACE, "gateway": str(OLD.gateway),
            "prefsrc": str(OLD.address.ip),
        }
        with (
            patch.object(route, "table_defaults", return_value=[reject, old_default]),
            patch.object(route, "run") as run,
            patch.object(route, "report"),
        ):
            route.configure_routes(101, INTERFACE, NEW)
            run.assert_called_once_with(
                "ip", "-4", "route", "replace", "default", "via", str(NEW.gateway), "dev", INTERFACE,
                "src", str(NEW.address.ip), "table", "101", "proto", "242", "metric", "100",
            )

    def test_other_default_owner_is_preserved(self):
        with (
            patch.object(route, "table_defaults", return_value=[{"protocol": "static", "gateway": str(OLD.gateway)}]),
            patch.object(route, "run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "unmanaged"):
                route.configure_routes(101, INTERFACE, NEW)
            run.assert_not_called()

    def test_failed_gateway_is_reprobed_after_restart(self):
        with (
            patch.object(route, "interface_state", return_value=(True, set())),
            patch.object(route, "read_lease", side_effect=[(OLD, None), (NEW, None), (NEW, None)]),
            patch.object(route, "lease_mtime", side_effect=[100, 200, 200]),
            patch.object(route, "gateway_replies", side_effect=[None, "ICMP"]),
            patch.object(route, "configure_routes") as configure,
            patch.object(route, "run") as run,
            patch.object(route, "report"),
            patch.object(route.time, "monotonic", return_value=500),
            patch.object(route.time, "sleep"),
        ):
            state = io.StringIO()
            self.assertEqual(route.maintain(INTERFACE, 101, state), 0)
            self.assertEqual(configure.call_args_list, [call(101, INTERFACE, None), call(101, INTERFACE, NEW)])
            run.assert_called_once_with("systemctl", "restart", "--no-block", "dhclient@eth0.99.service")
            self.assertEqual(state.getvalue(), "500")

    def test_cooldown_prevents_repeated_restart(self):
        with (
            patch.object(route, "interface_state", return_value=(True, set())),
            patch.object(route, "read_lease", return_value=(None, "DHCP lease has expired")),
            patch.object(route, "configure_routes"),
            patch.object(route, "run") as run,
            patch.object(route, "report"),
            patch.object(route.time, "monotonic", return_value=500),
        ):
            self.assertEqual(route.maintain(INTERFACE, 101, io.StringIO("400")), 1)
            run.assert_not_called()

    def test_lease_change_during_probe_withdraws_stale_route(self):
        with (
            patch.object(route, "interface_state", return_value=(True, set())),
            patch.object(route, "read_lease", side_effect=[(OLD, None), (NEW, None)]),
            patch.object(route, "gateway_replies", return_value="ICMP"),
            patch.object(route, "configure_routes") as configure,
            patch.object(route, "report"),
        ):
            self.assertEqual(route.maintain(INTERFACE, 101, io.StringIO()), 1)
            configure.assert_called_once_with(101, INTERFACE, None)


if __name__ == "__main__":
    unittest.main()
