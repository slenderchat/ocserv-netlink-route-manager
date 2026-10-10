"""Offline tests for route reconciliation; no kernel changes or pyroute2 required."""
import importlib.util
from pathlib import Path
import socket
import unittest

SCRIPT = Path(__file__).with_name("vpn-route-manager.py")
spec = importlib.util.spec_from_file_location("vpn_route_manager", SCRIPT)
import sys
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
CFG = mod.load_settings(Path(__file__).with_name("config.toml"))


class Msg(dict):
    def __init__(self, *, attrs=None, **kwargs):
        super().__init__(**kwargs)
        self.attrs = attrs or {}

    def get_attr(self, name):
        return self.attrs.get(name)


def route(dst, gateway, oif, proto=240):
    network, prefix = dst.split("/")
    return Msg(attrs={"RTA_DST": network, "RTA_GATEWAY": gateway, "RTA_OIF": oif},
               family=socket.AF_INET, table=254, dst_len=int(prefix), proto=proto,
               type=1)


class FakeIPR:
    def __init__(self, endpoints=None, routes=None):
        self.endpoints = endpoints or []
        self.routes = routes or []
        self.calls = []

    def get_links(self):
        return [Msg(attrs={"IFLA_IFNAME": name}, index=index, flags=0x11)
                for index, name, _peer in self.endpoints]

    def get_addr(self, family):
        assert family == socket.AF_INET
        return [Msg(attrs={"IFA_ADDRESS": peer, "IFA_LOCAL": "10.60.20.1"},
                    index=index, prefixlen=32)
                for index, _name, peer in self.endpoints]

    def get_routes(self, family, table):
        assert family == socket.AF_INET and table == 254
        return list(self.routes)

    def route(self, operation, **kwargs):
        self.calls.append((operation, kwargs))
        dest = kwargs["dst"]
        if operation == "add":
            self.routes.append(route(dest, kwargs["gateway"], kwargs["oif"], kwargs["proto"]))
        elif operation == "del":
            matching = [r for r in self.routes
                        if str(r.get_attr("RTA_DST")) == dest.split("/")[0]
                        and r["dst_len"] == int(dest.split("/")[1])
                        and r["proto"] == kwargs["proto"]
                        and r.get_attr("RTA_OIF") == kwargs.get("oif")]
            if not matching:
                raise ValueError("route missing")
            self.routes.remove(matching[0])
        else:
            raise ValueError(operation)


class TestReconcile(unittest.TestCase):
    def test_both_clients_and_idempotence(self):
        fake = FakeIPR([(10, "vpns0", "10.60.20.145"),
                        (11, "vpns1", "10.60.20.172")])
        mod.reconcile(fake, CFG)
        self.assertEqual([(name, kwargs["dst"]) for name, kwargs in fake.calls],
                         [("add", "192.168.1.0/24"), ("add", "192.168.2.0/24")])
        fake.calls.clear()
        mod.reconcile(fake, CFG)
        self.assertEqual(fake.calls, [])

    def test_reconnect_with_changed_interface(self):
        fake = FakeIPR([(22, "vpns5", "10.60.20.172")],
                       [route("192.168.1.0/24", "10.60.20.172", 11)])
        mod.reconcile(fake, CFG)
        self.assertEqual([name for name, _ in fake.calls], ["del", "add"])
        self.assertEqual(fake.calls[1][1]["oif"], 22)

    def test_disconnected_deletes_owned(self):
        fake = FakeIPR(routes=[route("192.168.2.0/24", "10.60.20.145", 10)])
        mod.reconcile(fake, CFG)
        self.assertEqual([name for name, _ in fake.calls], ["del"])

    def test_foreign_route_is_not_changed(self):
        fake = FakeIPR([(11, "vpns1", "10.60.20.172")],
                       [route("192.168.1.0/24", "10.60.20.172", 11, proto=4)])
        mod.reconcile(fake, CFG)
        self.assertEqual(fake.calls, [])

    def test_ambiguous_peer_removes_managed(self):
        fake = FakeIPR([(11, "vpns1", "10.60.20.172"),
                        (12, "vpns2", "10.60.20.172")],
                       [route("192.168.1.0/24", "10.60.20.172", 11)])
        mod.reconcile(fake, CFG)
        self.assertEqual([name for name, _ in fake.calls], ["del"])

    def test_dry_run_does_not_modify(self):
        fake = FakeIPR([(10, "vpns0", "10.60.20.145")])
        mod.reconcile(fake, CFG, dry_run=True)
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
