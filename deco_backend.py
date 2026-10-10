#!/usr/bin/env python3

import argparse
import dataclasses
import enum
import json
import logging
import sys
import time
from datetime import datetime, timezone
from getpass import getpass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from tplinkrouterc6u import TplinkRouterProvider

logger = logging.getLogger("deco-diagnostics")


def to_plain(value):
    """Convert library result objects to JSON-compatible values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, enum.Enum):
        return value.name

    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [to_plain(item) for item in value]

    if dataclasses.is_dataclass(value):
        return {
            field.name: to_plain(getattr(value, field.name))
            for field in dataclasses.fields(value)
            if not field.name.startswith("_")
        }

    # Include public instance attributes and properties, but no methods
    # or private implementation/session data.
    attributes = {}

    try:
        attributes.update(
            {key: item for key, item in vars(value).items() if not key.startswith("_")}
        )
    except TypeError:
        pass

    for cls in type(value).__mro__:
        for name, descriptor in vars(cls).items():
            if name.startswith("_") or not isinstance(descriptor, property):
                continue
            try:
                attributes[name] = getattr(value, name)
            except Exception:
                pass

    if attributes:
        return {key: to_plain(item) for key, item in attributes.items()}

    # Handles IP-address and MAC-address value objects.
    return str(value)


def show_json(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def run_query(router, method_name, report):
    """Run one read operation; retain errors without stopping other tests."""
    print(f"\n--- {method_name} ---", flush=True)

    method = getattr(router, method_name, None)
    if not callable(method):
        report["queries"][method_name] = {"state": "unavailable"}
        print("Method is not available in this installed client.")
        return None

    start = time.monotonic()

    try:
        result = method()
        elapsed = round(time.monotonic() - start, 3)
        plain = to_plain(result)

        report["queries"][method_name] = {
            "state": "ok",
            "seconds": elapsed,
            "data": plain,
        }

        print(f"Completed in {elapsed:.3f}s")
        show_json(plain)
        return result

    except NotImplementedError:
        report["queries"][method_name] = {
            "state": "unsupported",
            "seconds": round(time.monotonic() - start, 3),
        }
        print("Not implemented by this client.")
        return None

    except Exception as exc:
        report["queries"][method_name] = {
            "state": "error",
            "seconds": round(time.monotonic() - start, 3),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        logger.exception("%s failed; continuing with other read tests", method_name)
        return None


def display_nodes(nodes, report):
    """Produce a small, stable projection suitable for a dashboard."""
    fields = (
        "name",
        "location",
        "macaddr",
        "ipaddr",
        "model",
        "role",
        "is_main_router",
        "status",
        "connect_type",
        "parent_macaddr",
        "client_num",
        "hardware_version",
        "firmware_version",
        "internet_status",
        "signal_2g",
        "signal_5g",
    )

    dashboard_nodes = []
    for node in nodes:
        entry = {field: to_plain(getattr(node, field, None)) for field in fields}
        dashboard_nodes.append(entry)

    report["dashboard_nodes"] = dashboard_nodes

    print("\n=== Mesh-node names / dashboard summary ===")

    if not dashboard_nodes:
        print("The query returned no mesh nodes.")
        return

    for number, node in enumerate(dashboard_nodes, start=1):
        label = node["name"] or node["location"] or node["macaddr"] or f"Node {number}"

        print(f"\n{number}. {label}")
        print(f"   Reported name: {node['name']}")
        print(f"   Location:      {node['location']}")
        print(f"   Model:         {node['model']}")
        print(f"   Role:          {node['role']}")
        print(f"   Main node:     {node['is_main_router']}")
        print(f"   MAC:           {node['macaddr']}")
        print(f"   IP:            {node['ipaddr']}")
        print(f"   Status:        {node['status']}")
        print(f"   Uplink:        {node['connect_type']}")
        print(f"   Clients:       {node['client_num']}")


def main():
    parser = argparse.ArgumentParser(description="Read-only TP-Link Deco diagnostics.")
    parser.add_argument(
        "--host",
        default="https://192.168.178.172",
        help="Main Deco URL (default: %(default)s)",
    )
    parser.add_argument(
        "--verify-ssl",
        action="store_true",
        help="Enable TLS certificate verification.",
    )
    parser.add_argument(
        "--json",
        type=Path,
        help="Optionally save the diagnostic report to a local JSON file.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        package_version = version("tplinkrouterc6u")
    except PackageNotFoundError:
        package_version = "unknown"

    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "host": args.host,
        "library_version": package_version,
        "verify_ssl": args.verify_ssl,
        "queries": {},
        "dashboard_nodes": [],
    }

    print(f"Library version: {package_version}")
    print(f"Target:          {args.host}")
    print(f"TLS verification: {args.verify_ssl}")
    print("Read-only queries only; no Wi-Fi or configuration changes.")

    password = getpass("Deco web-interface password: ")
    router = None
    authorization_attempted = False
    exit_code = 0

    try:
        print("\nDetecting client...", flush=True)
        start = time.monotonic()

        router = TplinkRouterProvider.get_client(
            args.host,
            password,
            verify_ssl=args.verify_ssl,
            logger=logger,
        )

        if router is None:
            raise RuntimeError("Provider did not return a client.")

        report["client_class"] = type(router).__name__
        report["detection_seconds"] = round(time.monotonic() - start, 3)

        print(
            f"Selected {report['client_class']} "
            f"in {report['detection_seconds']:.3f}s"
        )

        print("\nAuthorizing...", flush=True)
        authorization_attempted = True
        start = time.monotonic()
        router.authorize()
        report["authorization_seconds"] = round(time.monotonic() - start, 3)
        print(f"Authorized in {report['authorization_seconds']:.3f}s")

        # Query nodes early: this is the most interesting dashboard data.
        nodes = run_query(router, "get_mesh_nodes", report)
        if nodes is not None:
            display_nodes(nodes, report)

        run_query(router, "get_firmware", report)
        run_query(router, "get_status", report)

        # These may be absent or unsupported on your firmware/client.
        for method_name in (
            "get_ipv4_status",
            "get_ipv6_status",
            "get_ipv4_reservations",
            "get_ipv4_dhcp_leases",
        ):
            run_query(router, method_name, report)

    except Exception as exc:
        exit_code = 1
        report["fatal_error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        logger.exception("Detection or authorization failed")

    finally:
        if router is not None and authorization_attempted:
            print("\nLogging out...", flush=True)
            try:
                router.logout()
                report["logout"] = "ok"
                print("Logged out.")
            except Exception as exc:
                report["logout"] = {
                    "state": "error",
                    "message": str(exc),
                }
                logger.warning("Logout failed: %s", exc)

        if args.json:
            try:
                args.json.write_text(
                    json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                print(f"\nSaved local report: {args.json}")
            except OSError:
                logger.exception("Could not save JSON report")
                exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
