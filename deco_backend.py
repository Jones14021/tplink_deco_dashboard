#!/usr/bin/env python3

import argparse
import logging
import threading
import time
from contextlib import contextmanager
from getpass import getpass

from tplinkrouterc6u import Connection, TplinkRouterProvider

LOG = logging.getLogger("deco.backend")

BANDS = {
    "2g": (Connection.HOST_2G, "wifi_2g_enable"),
    "5g": (Connection.HOST_5G, "wifi_5g_enable"),
    "6g": (Connection.HOST_6G, "wifi_6g_enable"),
}


def configure_logging(debug=False):
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    # Keep transport chatter out of normal logs.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def wifi_state(wifi):
    values = list(wifi.values())
    if not all(isinstance(value, bool) for value in values):
        return "unknown"
    if all(values):
        return "on"
    if not any(values):
        return "off"
    return "mixed"


def format_wifi(wifi):
    return ", ".join(
        f"{band}={'ON' if value is True else 'OFF' if value is False else '?'}"
        for band, value in wifi.items()
    )


class DecoBackend:
    def __init__(self, host, password, verify_ssl=False):
        self.host = host
        self.password = password
        self.verify_ssl = verify_ssl
        self.lock = threading.Lock()

    @contextmanager
    def session(self):
        with self.lock:
            router = None
            started = time.monotonic()

            try:
                LOG.info("Connecting to %s", self.host)

                router = TplinkRouterProvider.get_client(
                    self.host,
                    self.password,
                    verify_ssl=self.verify_ssl,
                    logger=LOG,
                )

                if router is None:
                    raise RuntimeError("No supported router client found.")

                LOG.info("Selected client: %s", type(router).__name__)
                router.authorize()
                LOG.info("Authenticated in %.2fs", time.monotonic() - started)
                yield router
            finally:
                if router is not None:
                    try:
                        router.logout()
                        LOG.info("Logged out")
                    except Exception:
                        LOG.exception("Logout failed")

    @staticmethod
    def read_status(router):
        started = time.monotonic()
        LOG.info("Querying Wi-Fi state and clients")
        status = router.get_status()

        wifi = {
            band: getattr(status, attribute, None)
            for band, (_, attribute) in BANDS.items()
        }
        counts = {band: 0 for band in BANDS}

        for device in getattr(status, "devices", []) or []:
            if getattr(device, "active", None) is False:
                continue

            kind = getattr(device, "type", None)
            kind = getattr(kind, "name", str(kind))

            for band in BANDS:
                if kind == f"HOST_{band.upper()}":
                    counts[band] += 1

        LOG.info(
            "Status in %.2fs: %s; main-network clients=%s",
            time.monotonic() - started,
            format_wifi(wifi),
            counts,
        )

        return {
            "wifi": wifi,
            "state": wifi_state(wifi),
            "clients": counts,
        }

    def snapshot(self):
        started = time.monotonic()

        with self.session() as router:
            LOG.info("Querying mesh nodes")
            raw_nodes = router.get_mesh_nodes()
            nodes = []

            for node in raw_nodes:
                address = getattr(node, "macaddr", None)
                ip = getattr(node, "ipaddr", None)

                entry = {
                    "id": str(address) if address is not None else None,
                    "name": getattr(node, "name", None),
                    "status": getattr(node, "status", None),
                    "role": getattr(node, "role", None),
                    "ip": str(ip) if ip is not None else None,
                }
                nodes.append(entry)

                LOG.info(
                    "Node: name=%s status=%s role=%s ip=%s",
                    entry["name"],
                    entry["status"],
                    entry["role"],
                    entry["ip"],
                )

            network = self.read_status(router)

        LOG.info("Refresh completed in %.2fs", time.monotonic() - started)

        return {
            "observed_at": time.time(),
            "nodes": nodes,
            "network": network,
        }

    def set_main_wifi(self, enabled, attempts=6, interval=3):
        """
        Change the three main-network bands and verify configuration read-back.

        No automatic rollback or repeated writes are performed.
        """
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be bool")

        result = {
            "desired": enabled,
            "verified": False,
            "write_errors": {},
            "observations": [],
        }

        LOG.warning(
            "MAIN WI-FI CHANGE requested: all bands -> %s",
            "ON" if enabled else "OFF",
        )

        with self.session() as router:
            before = self.read_status(router)
            result["before"] = before["wifi"]

            # Do not overwrite bands whose state could not be queried.
            if before["state"] == "unknown":
                raise RuntimeError(
                    "Cannot change Wi-Fi: current band state is unknown."
                )

            for band, (connection, _) in BANDS.items():
                if before["wifi"][band] is enabled:
                    LOG.info("%s already matches target; skipping", band)
                    continue

                LOG.warning(
                    "Writing main %s -> %s",
                    band,
                    "ON" if enabled else "OFF",
                )
                try:
                    router.set_wifi(connection, enabled)
                    LOG.info("%s write returned successfully", band)
                except Exception as exc:
                    result["write_errors"][band] = f"{type(exc).__name__}: {exc}"
                    LOG.exception(
                        "%s write failed; will still query actual state", band
                    )

            for attempt in range(1, attempts + 1):
                LOG.info(
                    "Verification %d/%d in %ss",
                    attempt,
                    attempts,
                    interval,
                )
                time.sleep(interval)

                try:
                    network = self.read_status(router)
                    observation = {
                        "attempt": attempt,
                        "wifi": network["wifi"],
                    }
                    result["observations"].append(observation)

                    if all(value is enabled for value in network["wifi"].values()):
                        result["verified"] = True
                        LOG.info("Main Wi-Fi configuration verified")
                        break

                    LOG.warning(
                        "Target not reached: %s",
                        format_wifi(network["wifi"]),
                    )
                except Exception as exc:
                    result["observations"].append(
                        {
                            "attempt": attempt,
                            "error": str(exc),
                        }
                    )
                    LOG.exception("Verification read failed")

        if not result["verified"]:
            LOG.error("Main Wi-Fi change NOT verified after %d attempts", attempts)

        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="https://192.168.178.172")
    parser.add_argument("--verify-ssl", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    configure_logging(args.debug)
    backend = DecoBackend(
        args.host,
        getpass("Deco web password: "),
        verify_ssl=args.verify_ssl,
    )

    while True:
        try:
            snapshot = backend.snapshot()
            network = snapshot["network"]

            print(f"\nMain Wi-Fi: {network['state'].upper()}")
            print(format_wifi(network["wifi"]))
            print(f"Clients: {network['clients']}")
            print("[r] refresh  [t] toggle main Wi-Fi  [q] quit")

            command = input("> ").strip().lower()
            if command == "q":
                return
            if command != "t":
                continue

            if network["state"] == "unknown":
                print("State unknown; refresh first.")
                continue

            target = network["state"] != "on"
            desired = "ON" if target else "OFF"

            if (
                input(f"Set all main Wi-Fi bands {desired}? Type APPLY: ").strip()
                != "APPLY"
            ):
                continue

            result = backend.set_main_wifi(target)
            print("Verified." if result["verified"] else "Not verified.")

        except (KeyboardInterrupt, EOFError):
            print()
            return
        except Exception:
            LOG.exception("Operation failed")


if __name__ == "__main__":
    main()
