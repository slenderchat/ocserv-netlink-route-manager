#!/usr/bin/python3
"""Keep selected IPv4 routes in sync with ocserv point-to-point peers.

Requires Debian python3-pyroute2 (targets the 0.7.7 API).
"""

import argparse
from dataclasses import dataclass
import ipaddress
import logging
from pathlib import Path
import re
import signal
import socket
import sys
import threading
import tomllib

LOG = logging.getLogger("vpn-route-manager")
MAIN_TABLE = 254
RTN_UNICAST = 1
IFF_UP = 0x1
IFF_POINTOPOINT = 0x10
RTMGRP_LINK = 0x1
RTMGRP_IPV4_IFADDR = 0x10
RTMGRP_IPV4_ROUTE = 0x40
EVENTS = {"RTM_NEWLINK", "RTM_DELLINK", "RTM_NEWADDR", "RTM_DELADDR",
          "RTM_NEWROUTE", "RTM_DELROUTE"}


@dataclass(frozen=True)
class Rule:
    destination: ipaddress.IPv4Network
    via: ipaddress.IPv4Address


@dataclass(frozen=True)
class Settings:
    pattern: re.Pattern
    interval: int
    proto: int
    routes: tuple[Rule, ...]


def load_settings(path):
    with open(path, "rb") as file:
        cfg = tomllib.load(file)
    options = cfg.get("settings", {})
    expression = options.get("interface_pattern", r"vpns[0-9]+")
    if not isinstance(expression, str):
        raise ValueError("interface_pattern must be a string")
    pattern = re.compile(expression)
    interval = options.get("resync_seconds", 60)
    proto = options.get("route_protocol", 240)
    if type(interval) is not int or not 1 <= interval <= 86400:
        raise ValueError("resync_seconds must be an integer from 1 to 86400")
    if type(proto) is not int or not 200 <= proto <= 252:
        raise ValueError("route_protocol must be an integer from 200 to 252")
    entries = cfg.get("routes", [])
    if not isinstance(entries, list) or not entries:
        raise ValueError("at least one [[routes]] entry is required")
    result = []
    unique = set()
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("each [[routes]] must be a table")
        destination = ipaddress.IPv4Network(item["destination"], strict=True)
        via = ipaddress.IPv4Address(item["via"])
        if destination in unique:
            raise ValueError(f"duplicate destination: {destination}")
        unique.add(destination)
        result.append(Rule(destination, via))
    return Settings(pattern, interval, proto, tuple(result))


def peer_interfaces(ipr, settings):
    """Find client peers from IFA_ADDRESS != IFA_LOCAL on UP P2P vpns* links."""
    links = {}
    for link in ipr.get_links():
        name = link.get_attr("IFLA_IFNAME")
        flags = link.get("flags", 0)
        if (name and settings.pattern.fullmatch(name)
                and flags & IFF_UP and flags & IFF_POINTOPOINT):
            links[link["index"]] = name

    wanted = {rule.via for rule in settings.routes}
    peers = {peer: set() for peer in wanted}
    for address in ipr.get_addr(family=socket.AF_INET):
        index = address.get("index")
        if index not in links or address.get("prefixlen") != 32:
            continue
        remote = address.get_attr("IFA_ADDRESS")
        local = address.get_attr("IFA_LOCAL")
        if not remote or not local or remote == local:
            continue
        try:
            peer = ipaddress.IPv4Address(remote)
        except ipaddress.AddressValueError:
            continue
        if peer in wanted:
            peers[peer].add((index, links[index]))
    return peers


def find_routes(ipr, settings):
    """Return all IPv4 main-table routes with the configured exact prefixes."""
    wanted = {rule.destination for rule in settings.routes}
    found = {destination: [] for destination in wanted}
    for route in ipr.get_routes(family=socket.AF_INET, table=MAIN_TABLE):
        if route.get("table", MAIN_TABLE) != MAIN_TABLE:
            continue
        length = route.get("dst_len")
        value = route.get_attr("RTA_DST")
        if length is None or value is None:
            continue
        try:
            prefix = ipaddress.IPv4Network(f"{value}/{length}", strict=False)
        except ValueError:
            continue
        if prefix in wanted:
            found[prefix].append(route)
    return found


def owned(route, settings):
    return route.get("proto") == settings.proto


def correct(route, settings, peer, index):
    return (owned(route, settings)
            and route.get("type") == RTN_UNICAST
            and route.get_attr("RTA_GATEWAY") == str(peer)
            and route.get_attr("RTA_OIF") == index
            and route.get_attr("RTA_MULTIPATH") is None
            and route.get_attr("RTA_NH_ID") is None
            and route.get_attr("RTA_PRIORITY") in (None, 0))


def deletion_kwargs(destination, route, settings):
    """Identify a route narrowly, without replacing any unmanaged route."""
    kwargs = {"dst": str(destination), "family": socket.AF_INET,
              "table": MAIN_TABLE, "proto": settings.proto}
    for key, nla in (("gateway", "RTA_GATEWAY"), ("oif", "RTA_OIF"),
                     ("priority", "RTA_PRIORITY")):
        value = route.get_attr(nla)
        if value is not None:
            kwargs[key] = value
    return kwargs


def reconcile(ipr, settings, dry_run=False):
    # Read snapshots before making any modifications. Notifications request a new
    # snapshot, never direct replay of an event with potentially stale ifindexes.
    peers = peer_interfaces(ipr, settings)
    current = find_routes(ipr, settings)

    for rule in settings.routes:
        routes = current[rule.destination]
        foreign = [route for route in routes if not owned(route, settings)]
        if foreign:
            LOG.error("Conflict: %s already has %s unmanaged route(s); leaving unchanged",
                      rule.destination, len(foreign))
            continue

        endpoints = peers[rule.via]
        if len(endpoints) > 1:
            LOG.error("Ambiguous peer %s on %s; refusing to install route",
                      rule.via, sorted(endpoints))
            endpoint = None
        else:
            endpoint = next(iter(endpoints)) if endpoints else None

        if (endpoint is not None and len(routes) == 1
                and correct(routes[0], settings, rule.via, endpoint[0])):
            continue

        # Remove only routes bearing the explicitly assigned protocol number.
        # A stale route is not replaced in-place if the interface has changed.
        failed = False
        for route in routes:
            kwargs = deletion_kwargs(rule.destination, route, settings)
            if dry_run:
                LOG.info("DRY-RUN delete %s via %s oif=%s proto=%s",
                         rule.destination, route.get_attr("RTA_GATEWAY"),
                         route.get_attr("RTA_OIF"), settings.proto)
                continue
            try:
                ipr.route("del", **kwargs)
                LOG.info("Deleted %s via %s oif=%s", rule.destination,
                         route.get_attr("RTA_GATEWAY"), route.get_attr("RTA_OIF"))
            except Exception:
                failed = True
                LOG.exception("Failed to delete managed route %s", rule.destination)
        if failed or endpoint is None:
            continue

        index, name = endpoint
        kwargs = {"dst": str(rule.destination), "gateway": str(rule.via),
                  "oif": index, "family": socket.AF_INET,
                  "table": MAIN_TABLE, "proto": settings.proto}
        if dry_run:
            LOG.info("DRY-RUN add %s via %s dev %s (ifindex=%s, proto=%s)",
                     rule.destination, rule.via, name, index, settings.proto)
            continue
        try:
            ipr.route("add", **kwargs)
            LOG.info("Added %s via %s dev %s (ifindex=%s)",
                     rule.destination, rule.via, name, index)
        except Exception:
            # It may disappear between reading addresses and adding the route.
            # Another Netlink event (or periodic scan) will correct the state.
            LOG.exception("Failed to add %s via %s dev %s", rule.destination, rule.via, name)


def watch(monitor, trigger, state):
    try:
        while not state["stopping"]:
            for message in monitor.get():
                event = message.get("event")
                if event in {"NLMSG_OVERRUN", "NLMSG_ERROR"}:
                    raise RuntimeError(f"Netlink monitor reported {event}")
                if event in EVENTS:
                    trigger.set()
    except Exception as exc:
        if not state["stopping"]:
            state["error"] = exc
            trigger.set()


def run(settings, dry_run=False, once=False):
    from pyroute2 import IPRoute

    if once:
        with IPRoute() as ipr:
            reconcile(ipr, settings, dry_run)
        return

    trigger = threading.Event()
    state = {"stopping": False, "error": None}

    def stop(_signum, _frame):
        state["stopping"] = True
        trigger.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    # Subscribe BEFORE taking the initial snapshot to avoid missing new peers.
    with IPRoute() as monitor, IPRoute() as control:
        monitor.bind(groups=RTMGRP_LINK | RTMGRP_IPV4_IFADDR | RTMGRP_IPV4_ROUTE)
        listener = threading.Thread(target=watch, args=(monitor, trigger, state),
                                    name="netlink-listener", daemon=True)
        listener.start()
        LOG.info("Monitoring Netlink; resync every %ss; protocol %s",
                 settings.interval, settings.proto)
        while not state["stopping"]:
            trigger.clear()
            if state["error"] is not None:
                raise RuntimeError("Netlink event listener failed") from state["error"]
            reconcile(control, settings, dry_run)
            if state["error"] is not None:
                raise RuntimeError("Netlink event listener failed") from state["error"]
            if not state["stopping"]:
                trigger.wait(timeout=settings.interval)
        LOG.info("Stopping")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/vpn-route-manager/config.toml")
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--once", action="store_true", help="reconcile once and exit")
    parser.add_argument("--dry-run", action="store_true", help="log actions without changing routes")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = load_settings(Path(args.config))
        if args.check_config:
            LOG.info("Config OK: %s route(s), protocol %s", len(settings.routes), settings.proto)
            return 0
        run(settings, dry_run=args.dry_run, once=args.once)
        return 0
    except (Exception, KeyboardInterrupt):
        LOG.exception("vpn-route-manager stopped with error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
