#!/usr/bin/env python3

import argparse
import json
import logging
import threading
import time
from contextlib import contextmanager
from getpass import getpass

from tplinkrouterc6u import Connection, TplinkRouterProvider

BANDS = {
    "2g": (Connection.HOST_2G, "wifi_2g_enable"),
    "5g": (Connection.HOST_5G, "wifi_5g_enable"),
    "6g": (Connection.HOST_6G, "wifi_6g_enable"),
}

LOG = logging.getLogger("deco")


def text(value):
    return None if value is None else str(value)


def connection_name(value):
    return getattr(value, "name", str(value))


class DecoBackend:
    """
    Reads mesh-node connectivity and network-wide main-Wi-Fi information.

    Writes are network-wide, experimental, and disabled by default.
    No per-node write endpoint is assumed.
    """

    def __init__(self, host, password, allow_writes=False, verify_ssl=False):
        self.host = host
        self.password = password
        self.allow_writes = allow_writes
        self.verify_ssl = verify_ssl
        self.lock = threading.Lock()

    @contextmanager
    def session(self):
        # Serializes sessions within this process.
        with self.lock:
            router = TplinkRouterProvider.get_client(
                self.host,
                self.password,
                verify_ssl=self.verify_ssl,
                logger=LOG,
            )
            if router is None:
                raise RuntimeError("No supported client detected.")

            try:
                router.authorize()
                yield router
            finally:
                try:
                    router.logout()
                except Exception:
                    LOG.warning("Logout failed", exc_info=True)

    @staticmethod
    def read_wifi(router):
        status = router.get_status()
        return {
            band: getattr(status, attribute, None)
            for band, (_, attribute) in BANDS.items()
        }, status

    def snapshot(self):
        with self.session() as router:
            nodes = router.get_mesh_nodes()
            wifi, status = self.read_wifi(router)

            counts = {
                "host": {band: 0 for band in BANDS},
                "guest": {band: 0 for band in BANDS},
                "iot": {band: 0 for band in BANDS},
            }
            unknown_active_clients = 0

            for device in getattr(status, "devices", []) or []:
                if getattr(device, "active", None) is False:
                    continue

                kind = connection_name(getattr(device, "type", None))
                found = False

                for network in counts:
                    for band in BANDS:
                        if kind == f"{network.upper()}_{band.upper()}":
                            counts[network][band] += 1
                            found = True

                if not found:
                    unknown_active_clients += 1

            result_nodes = []
            for node in nodes:
                state = getattr(node, "status", None)
                online = (
                    True
                    if state == "connected"
                    else False if state == "disconnected" else None
                )

                result_nodes.append(
                    {
                        "id": text(getattr(node, "macaddr", None)),
                        "name": getattr(node, "name", None),
                        "ip": text(getattr(node, "ipaddr", None)),
                        "model": getattr(node, "model", None),
                        "role": getattr(node, "role", None),
                        "reported_status": state,
                        "online": online,
                        # Intentionally not populated from global status.
                        "wifi": {band: None for band in BANDS},
                        "clients_per_band": {band: None for band in BANDS},
                        "individual_toggle_supported": False,
                    }
                )

            return {
                "observed_at": time.time(),
                "nodes": result_nodes,
                "network": {
                    "wifi": wifi,
                    "clients": counts,
                    "other_active_clients": unknown_active_clients,
                },
                "capabilities": {
                    "node_wifi_status": False,
                    "node_client_counts": False,
                    "node_toggle": False,
                    "experimental_network_writes": self.allow_writes,
                },
            }

    def set_node_wifi(self, node_id, enabled):
        raise NotImplementedError(
            "Individual-node Wi-Fi control is not available through "
            "the documented tplinkrouterc6u API. No write was sent."
        )

    def set_network_band(self, band, enabled, attempts=6, interval=3):
        """
        Send one write, then poll reported network configuration.

        Verification means configuration read-back, not proof that every
        physical radio/SSID stopped or started transmitting.
        """
        if not self.allow_writes:
            raise PermissionError(
                "Writes are disabled. Start with --allow-network-writes "
                "to enable experimental network-wide control."
            )
        if band not in BANDS:
            raise ValueError(f"Unknown band: {band}")
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be bool")

        result = {
            "scope": "network",
            "band": band,
            "desired": enabled,
            "write_error": None,
            "verified": False,
            "verification_scope": "reported_configuration",
            "observations": [],
        }

        with self.session() as router:
            before, _ = self.read_wifi(router)
            result["before"] = before[band]

            if before[band] is enabled:
                result["verified"] = True
                result["already_in_desired_state"] = True
                return result

            try:
                router.set_wifi(BANDS[band][0], enabled)
            except Exception as exc:
                # A failed HTTP response does not prove nothing changed.
                result["write_error"] = f"{type(exc).__name__}: {exc}"

            for attempt in range(1, attempts + 1):
                time.sleep(interval)
                observation = {"attempt": attempt}

                try:
                    wifi, _ = self.read_wifi(router)
                    observation["reported"] = wifi[band]
                    if wifi[band] is enabled:
                        result["verified"] = True
                except Exception as exc:
                    observation["error"] = f"{type(exc).__name__}: {exc}"

                result["observations"].append(observation)
                if result["verified"]:
                    break

        return result


def cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="https://192.168.178.172")
    parser.add_argument("--verify-ssl", action="store_true")
    parser.add_argument("--allow-network-writes", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    backend = DecoBackend(
        args.host,
        getpass("Deco web password: "),
        allow_writes=args.allow_network_writes,
        verify_ssl=args.verify_ssl,
    )

    while True:
        try:
            data = backend.snapshot()
            print(json.dumps(data, indent=2, ensure_ascii=False))

            print("\n[r] refresh  [n] select active node")
            print("[w] experimental network-wide band change  [q] quit")
            command = input("> ").strip().lower()

            if command == "q":
                return
            if command == "n":
                active = [n for n in data["nodes"] if n["online"] is True]
                if not active:
                    print("No active nodes.")
                    continue
                for index, node in enumerate(active, 1):
                    print(f"{index}: {node['name']} ({node['id']})")
                index = int(input("Node number: ")) - 1
                if not 0 <= index < len(active):
                    raise ValueError("Invalid node number")
                node = active[index]
                print(
                    f"{node['name']}: individual Wi-Fi state/counts and "
                    "individual switching are unavailable. No write sent."
                )
            elif command == "w":
                if not backend.allow_writes:
                    print("Writes disabled; use --allow-network-writes.")
                    continue

                band = input("Band [2g/5g/6g]: ").strip().lower()
                if band not in BANDS:
                    raise ValueError("Choose 2g, 5g, or 6g")

                action = input("Desired state [on/off]: ").strip().lower()
                if action not in ("on", "off"):
                    raise ValueError("Choose on or off")

                print(
                    f"NETWORK-WIDE change: {band} -> {action}.\n"
                    "This is NOT restricted to a selected node.\n"
                    "Use Ethernet or another independent management path."
                )
                if input("Type APPLY to send this write: ").strip() == "APPLY":
                    print(
                        json.dumps(
                            backend.set_network_band(band, action == "on"),
                            indent=2,
                            ensure_ascii=False,
                        )
                    )
        except (EOFError, KeyboardInterrupt):
            print()
            return
        except Exception as exc:
            print(f"Error: {exc}")


if __name__ == "__main__":
    cli()
