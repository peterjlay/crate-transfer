import socket
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from zeroconf import IPVersion

from crate_transfer.discovery import Discovery, SERVICE, preferred_address


class DiscoveryTests(unittest.TestCase):
    def test_prefers_receiver_address_on_a_shared_subnet(self):
        interfaces = {"en0": [SimpleNamespace(family=socket.AF_INET, address="192.168.1.5", netmask="255.255.255.0")]}
        with patch("crate_transfer.discovery.psutil.net_if_addrs", return_value=interfaces):
            self.assertEqual(preferred_address(["10.100.0.1", "192.168.1.8"]), "192.168.1.8")
            self.assertEqual(preferred_address(["10.100.0.1"]), "10.100.0.1")

    def test_discovery_validates_announcements_and_does_not_pair_them(self):
        own = {"id": "11111111-1111-4111-8111-111111111111", "name": "This Mac"}
        remote_id = "22222222-2222-4222-8222-222222222222"
        info = Mock()
        info.properties = {b"id": remote_id.encode(), b"name": b"Other Mac", b"fingerprint": b"a" * 64, b"version": b"1"}
        info.parsed_addresses.return_value = ["192.168.1.8"]
        info.port = 49783
        changed = Mock()
        with patch("crate_transfer.discovery.Zeroconf") as zeroconf, \
                patch("crate_transfer.discovery.ServiceBrowser") as browser, \
                patch("crate_transfer.discovery.preferred_address", return_value="192.168.1.8"):
            zeroconf.return_value.get_service_info.return_value = info
            discovery = Discovery(own, 49783, "b" * 64, changed)
            discovery.update_service(zeroconf.return_value, SERVICE, "remote")
            self.assertEqual(discovery.snapshot(), [{
                "id": remote_id, "name": "Other Mac", "fingerprint": "a" * 64,
                "host": "192.168.1.8", "port": 49783,
            }])
            info.parsed_addresses.assert_called_with(IPVersion.V4Only)
            changed.assert_called_once()
            info.properties[b"fingerprint"] = b"invalid"
            discovery.update_service(zeroconf.return_value, SERVICE, "malformed")
            self.assertEqual(len(discovery.snapshot()), 1)
            discovery.remove_service(zeroconf.return_value, SERVICE, "remote")
            self.assertEqual(discovery.snapshot(), [])
            discovery.stop()
            browser.return_value.cancel.assert_called_once()
            zeroconf.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
