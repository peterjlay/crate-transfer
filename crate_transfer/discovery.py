from __future__ import annotations

import logging
import ipaddress
import socket
import threading

import psutil
from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

from .protocol import identifier, label, machine_id

LOG = logging.getLogger(__name__)
SERVICE = "_seratocrate._tcp.local."


def preferred_address(addresses):
    networks = [
        ipaddress.ip_network(f"{address.address}/{address.netmask}", strict=False)
        for interface in psutil.net_if_addrs().values()
        for address in interface
        if address.family == socket.AF_INET and address.netmask
    ]
    return next((address for address in addresses
                 if any(ipaddress.ip_address(address) in network for network in networks)),
                addresses[0])


class Discovery(ServiceListener):
    def __init__(self, settings: dict, port: int, fingerprint: str, notify=lambda: None):
        self.own_id = settings["id"]
        self.notify = notify
        self.lock = threading.Lock()
        self.peers = {}
        self.zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        addresses = sorted({
            address.address
            for interface in psutil.net_if_addrs().values()
            for address in interface
            if address.family == socket.AF_INET and not address.address.startswith("127.")
        })
        self.info = ServiceInfo(
            SERVICE, f"{self.own_id}.{SERVICE}", port=port,
            addresses=[socket.inet_aton(address) for address in addresses or ["127.0.0.1"]],
            server=f"{self.own_id}.local.",
            properties={"id": self.own_id, "name": settings["name"], "fingerprint": fingerprint, "version": "1"},
        )
        try:
            self.zeroconf.register_service(self.info)
            self.browser = ServiceBrowser(self.zeroconf, SERVICE, self)
        except BaseException:
            self.zeroconf.close()
            raise

    def snapshot(self):
        with self.lock:
            return [dict(peer) for peer in self.peers.values()]

    def add_service(self, zeroconf, type_, name):
        self.update_service(zeroconf, type_, name)

    def update_service(self, zeroconf, type_, name):
        info = zeroconf.get_service_info(type_, name, timeout=1500)
        if info is None:
            LOG.warning("Could not resolve discovered computer: %s", name)
            return
        try:
            props = {key.decode(): value.decode() for key, value in info.properties.items() if value is not None}
            addresses = info.parsed_addresses(IPVersion.V4Only)
            if props.get("version") != "1" or not addresses or props.get("id") == self.own_id:
                return
            peer = {"id": machine_id(props["id"]), "name": label(props["name"], 100),
                    "fingerprint": identifier(props["fingerprint"]),
                    "host": preferred_address(addresses), "port": info.port}
            with self.lock:
                self.peers[name] = peer
            self.notify()
        except (ValueError, KeyError) as error:
            LOG.warning("Ignored malformed discovery announcement: %s", error)

    def remove_service(self, zeroconf, type_, name):
        with self.lock:
            self.peers.pop(name, None)
        self.notify()

    def stop(self):
        self.browser.cancel()
        self.zeroconf.unregister_service(self.info)
        self.zeroconf.close()
