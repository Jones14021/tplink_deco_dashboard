#!/usr/bin/env python3

import argparse
import logging
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from deco_backend import DecoBackend, configure_logging

LOG = logging.getLogger("deco.gui")


class Dashboard:
    def __init__(self, root, backend, refresh_seconds=30):
        self.root = root
        self.backend = backend
        self.refresh_seconds = refresh_seconds
        self.events = queue.Queue()
        self.busy = False
        self.data = None
        self.closed = False
        self.next_refresh = None

        root.title("Deco dashboard")
        root.geometry("850x390")
        root.minsize(700, 330)
        root.protocol("WM_DELETE_WINDOW", self.close)

        columns = ("name", "status", "role", "ip")
        self.tree = ttk.Treeview(root, columns=columns, show="headings", height=7)

        for key, title, width in (
            ("name", "Node", 260),
            ("status", "Connectivity", 160),
            ("role", "Role", 120),
            ("ip", "IP", 180),
        ):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width)

        self.tree.tag_configure("online", foreground="#187333")
        self.tree.tag_configure("offline", foreground="#a02020")
        self.tree.pack(fill="both", expand=True, padx=14, pady=(14, 8))

        totals = ttk.Frame(root)
        totals.pack(fill="x", padx=14, pady=4)

        self.band_labels = {}
        for band, title in (
            ("2g", "2.4 GHz"),
            ("5g", "5 GHz"),
            ("6g", "6 GHz"),
        ):
            frame = ttk.LabelFrame(totals, text=title)
            frame.pack(side="left", fill="x", expand=True, padx=4)
            label = ttk.Label(frame, text="Loading...", anchor="center")
            label.pack(fill="x", padx=8, pady=8)
            self.band_labels[band] = label

        actions = ttk.Frame(root)
        actions.pack(fill="x", padx=14, pady=10)

        self.refresh_button = ttk.Button(actions, text="Refresh", command=self.refresh)
        self.refresh_button.pack(side="left")

        # Classic Tk button supports explicit colors independent of ttk theme.
        self.toggle = tk.Button(
            actions,
            text="Loading...",
            command=self.toggle_wifi,
            font=("Sans", 12, "bold"),
            width=27,
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=10,
        )
        self.toggle.pack(side="right")

        self.status = tk.StringVar(value="Loading...")
        ttk.Label(root, textvariable=self.status, wraplength=810).pack(
            anchor="w", padx=14, pady=(0, 12)
        )

        self.paint_button("loading")
        root.after(100, self.process_events)
        self.refresh()

    def close(self):
        LOG.info("Closing dashboard")
        self.closed = True
        if self.next_refresh is not None:
            self.root.after_cancel(self.next_refresh)
        self.root.destroy()

    def paint_button(self, state):
        styles = {
            "loading": ("Loading...", "#888888", False),
            "on": ("Main Wi-Fi ON — turn OFF", "#258b45", True),
            "off": ("Main Wi-Fi OFF — turn ON", "#c0392b", True),
            "mixed": ("Main Wi-Fi mixed — turn ON", "#b77916", True),
            "unknown": ("Main Wi-Fi unavailable", "#888888", False),
        }
        text, color, enabled = styles[state]

        self.toggle.configure(
            text=text,
            background=color,
            activebackground=color,
            foreground="white",
            activeforeground="white",
            disabledforeground="white",
            state="normal" if enabled else "disabled",
        )

    def cancel_auto_refresh(self):
        if self.next_refresh is not None:
            self.root.after_cancel(self.next_refresh)
            self.next_refresh = None

    def schedule_refresh(self):
        self.cancel_auto_refresh()
        if self.refresh_seconds > 0:
            self.next_refresh = self.root.after(
                self.refresh_seconds * 1000,
                self.refresh,
            )

    def launch(self, operation):
        if self.busy or self.closed:
            return

        self.cancel_auto_refresh()
        self.busy = True
        self.paint_button("loading")
        self.refresh_button.configure(state="disabled")
        self.status.set("Querying router...")

        def worker():
            try:
                self.events.put((operation(), None))
            except Exception as exc:
                LOG.exception("Dashboard operation failed")
                self.events.put((None, str(exc)))

        threading.Thread(
            target=worker,
            name="deco-worker",
            daemon=True,
        ).start()

    def refresh(self):
        LOG.info("Refresh requested")
        self.launch(
            lambda: {
                "snapshot": self.backend.snapshot(),
                "change": None,
            }
        )

    def toggle_wifi(self):
        if self.busy or self.data is None:
            return

        current = self.data["network"]["state"]
        if current == "unknown":
            return

        desired = current != "on"
        action = "ON" if desired else "OFF"

        if not messagebox.askyesno(
            "Main Wi-Fi",
            f"Turn all main-network Wi-Fi bands {action}?",
            parent=self.root,
        ):
            return

        LOG.info("User confirmed main Wi-Fi -> %s", action)

        def operation():
            change = self.backend.set_main_wifi(desired)
            # Final fresh query is the only source for the displayed state.
            snapshot = self.backend.snapshot()
            return {"snapshot": snapshot, "change": change}

        self.launch(operation)

    def render(self, snapshot):
        self.data = snapshot

        for item in self.tree.get_children():
            self.tree.delete(item)

        for index, node in enumerate(snapshot["nodes"]):
            status = node["status"]
            connectivity = (
                "online"
                if status == "connected"
                else "offline" if status == "disconnected" else status or "unknown"
            )
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    node["name"] or node["id"],
                    connectivity,
                    node["role"] or "—",
                    node["ip"] or "—",
                ),
                tags=(connectivity,),
            )

        network = snapshot["network"]
        for band, label in self.band_labels.items():
            value = network["wifi"][band]
            state = "ON" if value is True else "OFF" if value is False else "unknown"
            label.configure(text=f"Wi-Fi {state} · Clients {network['clients'][band]}")

        self.paint_button(network["state"])

    def process_events(self):
        if self.closed:
            return

        try:
            result, error = self.events.get_nowait()
        except queue.Empty:
            self.root.after(100, self.process_events)
            return

        self.busy = False
        self.refresh_button.configure(state="normal")

        if error:
            self.data = None
            self.paint_button("unknown")
            for label in self.band_labels.values():
                label.configure(text="Unavailable")
            self.status.set("Query failed — see console logs")
            LOG.error("Displayed state invalidated: %s", error)
        else:
            self.render(result["snapshot"])
            timestamp = time.strftime(
                "%H:%M:%S",
                time.localtime(result["snapshot"]["observed_at"]),
            )
            change = result["change"]

            if change is None:
                self.status.set(f"Updated {timestamp}")
            else:
                target = change["desired"]
                actual = result["snapshot"]["network"]["wifi"]
                matches = all(value is target for value in actual.values())

                if matches:
                    self.status.set(f"Wi-Fi state verified · {timestamp}")
                    LOG.info("Final refresh confirms requested state")
                else:
                    self.status.set(
                        f"Requested change not confirmed · Updated {timestamp}"
                    )
                    LOG.warning("Final queried state differs from target: %s", actual)

        self.schedule_refresh()
        self.root.after(100, self.process_events)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="https://192.168.178.172")
    parser.add_argument("--verify-ssl", action="store_true")
    parser.add_argument("--refresh-seconds", type=int, default=30)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    configure_logging(args.debug)

    root = tk.Tk()
    root.withdraw()

    password = simpledialog.askstring(
        "Deco login",
        "Deco web-interface password:",
        show="*",
        parent=root,
    )
    if password is None:
        root.destroy()
        return

    backend = DecoBackend(
        args.host,
        password,
        verify_ssl=args.verify_ssl,
    )

    root.deiconify()
    Dashboard(
        root,
        backend,
        refresh_seconds=max(0, args.refresh_seconds),
    )
    root.mainloop()


if __name__ == "__main__":
    main()
