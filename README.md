# ocserv Netlink Route Manager

A lightweight, event-driven Linux daemon for maintaining IPv4 routes through
[ocserv](https://ocserv.gitlab.io/www/) VPN clients. It identifies a client by
its **stable IPv4 peer address**, not by the name of its current `vpnsN`
point-to-point interface. No ocserv configuration changes are required.

> **Status:** The route reconciliation logic has offline unit tests. This
> project has **not yet been integration-tested** with a live ocserv instance.
> Check its proposed changes using `--dry-run` before enabling the service.

## Example

Given these addresses on a VPN server:

```text
vpns0: inet 10.60.20.1 peer 10.60.20.145/32
vpns1: inet 10.60.20.1 peer 10.60.20.172/32
```

The daemon maintains:

```text
192.168.1.0/24 via 10.60.20.172 dev vpns1 proto 240
192.168.2.0/24 via 10.60.20.145 dev vpns0 proto 240
```

When a client disconnects, its managed route is removed. When it reconnects
under another `vpnsN` name, the route is automatically recreated using its
new interface.

## How it works

- Subscribes to Linux Netlink link, IPv4 address, and IPv4 route events using
  `pyroute2`.
- Looks for UP point-to-point interfaces matching `interface_pattern`. The
  remote `/32` peer is read from `IFA_ADDRESS`, while `IFA_LOCAL` identifies
  the server-side address.
- Reconciles configured routes with the IPv4 `main` table on startup, after
  relevant events, and periodically (60 seconds by default).
- Marks routes with an explicitly selected route protocol (`240` by default)
  to distinguish them from routes managed by other software.
- Does not overwrite foreign routes with the same destination prefix, and
  does not arbitrarily pick an interface if the same peer address appears on
  multiple interfaces.
- Runs as a systemd service and restarts after failures.

The daemon manages **routes only**: it does not set up forwarding, NAT,
firewall rules, client-side routing, or IPv6.

## Requirements

- Linux and systemd (tested against mock data; designed for Debian 13)
- Python 3.11+ (uses standard-library `tomllib`)
- Debian `python3-pyroute2` package
- Root privileges or appropriate `CAP_NET_ADMIN` permissions

Install the dependency:

```bash
sudo apt update
sudo apt install python3-pyroute2
```

## Configuration

The supplied `config.toml` contains:

```toml
[settings]
interface_pattern = "vpns[0-9]+"
resync_seconds = 60
route_protocol = 240

[[routes]]
destination = "192.168.1.0/24"
via = "10.60.20.172"

[[routes]]
destination = "192.168.2.0/24"
via = "10.60.20.145"
```

`interface_pattern` is a **full-match regular expression**, so this example
also accepts interfaces like `vpns10`. `resync_seconds` accepts 1–86400;
`route_protocol` accepts 200–252.

**Before installation, ensure route protocol `240` is not already used** by
another component. Check both existing routes and local protocol mappings:

```bash
ip -4 route show table all proto 240
# Inspect /etc/iproute2/rt_protos or /usr/lib/iproute2/rt_protos, if present.
```

Choose another unused protocol number if necessary. Protocol IDs are ownership
markers, **not** security boundaries.

## Installation

On the VPN server, from the root of this checkout:

```bash
sudo install -D -m 0755 vpn-route-manager.py \
  /usr/local/sbin/vpn-route-manager.py
sudo install -D -m 0644 config.toml \
  /etc/vpn-route-manager/config.toml
sudo install -D -m 0644 vpn-route-manager.service \
  /etc/systemd/system/vpn-route-manager.service
```

Validate and preview, **without modifying the routing table**:

```bash
sudo /usr/bin/python3 /usr/local/sbin/vpn-route-manager.py --check-config
sudo /usr/bin/python3 /usr/local/sbin/vpn-route-manager.py --once --dry-run
```

Investigate any unexpected action or conflicting route before continuing.
When the preview is correct:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vpn-route-manager.service
```

Verify:

```bash
systemctl status vpn-route-manager.service
ip -4 route show table main proto 240
ip -4 route get 192.168.1.1
ip -4 route get 192.168.2.1
journalctl -fu vpn-route-manager.service
```

After changing `/etc/vpn-route-manager/config.toml`, reload by restarting
`vpn-route-manager.service`.

## Command-line options

| Option | Meaning |
| --- | --- |
| `--config PATH` | Read another TOML configuration |
| `--check-config` | Validate the configuration and exit |
| `--once` | Perform one reconciliation pass and exit |
| `--dry-run` | Log proposed operations without modifying routes |

For a local preview, run:

```bash
sudo /usr/bin/python3 ./vpn-route-manager.py --config ./config.toml --once --dry-run
```

## Tests

Six offline tests cover route creation, idempotence, interface reassignment,
client disconnection, unmanaged-route protection, ambiguous peer addresses,
and dry-run behavior. They do not require root or a working `pyroute2` module:

```bash
python3 -m unittest -v test_manager.py
```

## Limitations

- Only stable `/32` IPv4 point-to-point peers and the IPv4 `main` table are
  supported; other tables and VRFs are out of scope.
- Unmanaged routes with the same destination prefix are logged as conflicts
  rather than overwritten.
- If a destination is removed from TOML, an already installed route for that
  *old* destination is **not automatically removed**. Inspect and remove it
  manually after confirming ownership.
- Kernel state can change between reading Netlink snapshots and making route
  changes. The daemon logs failures and tries again on events or its next
  periodic reconciliation.
- Live ocserv + kernel integration still needs verification on the target host.

## License

**All rights reserved.** This is **not** an open-source project. The code is
not licensed for use, modification, or redistribution without prior written
permission from the copyright holder, except where the law or GitHub's terms
expressly permit otherwise. See [LICENSE](LICENSE). Third-party dependencies
have their own license terms.
