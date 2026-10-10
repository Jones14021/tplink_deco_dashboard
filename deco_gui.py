#!/usr/bin/env python3

import argparse
import queue
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from deco_backend import BANDS, DecoBackend


def state_label(value):
    if value is True:
        return "ON"
    if value is False:
        return "OFF"
    return "unknown"


class Dashboard:
    def __init__(self, root, backend):
        self.root = root
        self.backend = backend
        self.events = queue.Queue()
        self.busy = False
        self.data = None

        root.title("Deco dashboard POC")
        root.geometry("1050x470")

        self.message = tk.StringVar(value="Not loaded")
        ttk.Label(
            root,
            text=(
                "Node connectivity is real. Per-node Wi-Fi and client "
                "counts are unavailable; network totals appear below."
            ),
            wraplength=1000,
        ).pack(anchor="w", padx=12, pady=10)

        columns = ("name", "online", "role", "ip", "wifi", "clients")
        self.tree = ttk.Treeview(root, columns=columns, show="headings", height=7)
        titles = {
            "name": "Node",
            "online": "Connectivity",
            "role": "Role",
            "ip": "IP",
            "wifi": "Node Wi-Fi",
            "clients": "Node clients 2.4/5/6 GHz",
        }
        for column in columns:
            self.tree.heading(column, text=titles[column])
            self.tree.column(column, width=160)
        self.tree.pack(fill="both", expand=True, padx=12)
        self.tree.bind("<Double-1>", self.node_clicked)

        controls = ttk.Frame(root)
        controls.pack(fill="x", padx=12, pady=10)

        self.refresh_button = ttk.Button(controls, text="Refresh", command=self.refresh)
        self.refresh_button.pack(side="left")

        self.wifi_labels = {}
        self.toggle_buttons = {}

        for band in BANDS:
            frame = ttk.LabelFrame(controls, text=f"Network {band}")
            frame.pack(side="left", padx=8, fill="x", expand=True)
            label = ttk.Label(frame, text="Unknown")
            label.pack(padx=8, pady=4)
            self.wifi_labels[band] = label

            button = ttk.Button(
                frame,
                text="Toggle network band",
                command=lambda b=band: self.toggle_band(b),
            )
            button.pack(padx=8, pady=4)
            self.toggle_buttons[band] = button

        ttk.Label(root, textvariable=self.message, wraplength=1000).pack(
            anchor="w", padx=12, pady=8
        )

        self.update_buttons()
        root.after(100, self.process_events)
        self.refresh()

    def update_buttons(self):
        self.refresh_button.configure(state="disabled" if self.busy else "normal")
        for band, button in self.toggle_buttons.items():
            known = self.data is not None and isinstance(
                self.data["network"]["wifi"][band], bool
            )
            enabled = not self.busy and self.backend.allow_writes and known
            button.configure(state="normal" if enabled else "disabled")

    def launch(self, kind, operation):
        if self.busy:
            return
        self.busy = True
        self.message.set("Working...")
        self.update_buttons()

        def worker():
            try:
                result = operation()
                self.events.put((kind, result, None))
            except Exception as exc:
                self.events.put((kind, None, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def refresh(self):
        self.launch("snapshot", self.backend.snapshot)

    def render(self, data):
        self.data = data
        for item in self.tree.get_children():
            self.tree.delete(item)

        for index, node in enumerate(data["nodes"]):
            online = (
                "online"
                if node["online"] is True
                else "offline" if node["online"] is False else "unknown"
            )
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    node["name"] or node["id"],
                    online,
                    node["role"],
                    node["ip"] or "—",
                    "unknown",
                    "unknown / unknown / unknown",
                ),
            )

        for band, label in self.wifi_labels.items():
            network = data["network"]
            label.configure(
                text=(
                    f"Main Wi-Fi: {state_label(network['wifi'][band])}\n"
                    f"Clients: host {network['clients']['host'][band]}, "
                    f"guest {network['clients']['guest'][band]}, "
                    f"IoT {network['clients']['iot'][band]}"
                )
            )

    def node_clicked(self, event):
        row = self.tree.identify_row(event.y)
        if not row or self.data is None:
            return
        node = self.data["nodes"][int(row)]
        messagebox.showinfo(
            node["name"] or "Deco node",
            f"MAC: {node['id']}\n"
            f"Connectivity: {node['reported_status']}\n\n"
            "Individual-node Wi-Fi control is not exposed by this "
            "backend. No change was sent.\n\n"
            "Network-wide controls are separate.",
        )

    def toggle_band(self, band):
        if self.busy or self.data is None:
            return

        current = self.data["network"]["wifi"][band]
        if not isinstance(current, bool):
            return
        desired = not current

        if not messagebox.askyesno(
            "Confirm network-wide change",
            f"Set main-network {band} Wi-Fi "
            f"{'ON' if desired else 'OFF'}?\n\n"
            "This is NOT a selected-node operation.\n"
            "The firmware may reject this experimental write.\n"
            "Use an independent management connection.",
        ):
            return

        self.launch(
            "write",
            lambda: self.backend.set_network_band(band, desired),
        )

    def process_events(self):
        try:
            kind, result, error = self.events.get_nowait()
        except queue.Empty:
            self.root.after(100, self.process_events)
            return

        self.busy = False
        if error:
            self.message.set(f"Error: {error}")
            messagebox.showerror("Operation failed", error)
        elif kind == "snapshot":
            self.render(result)
            self.message.set("Read completed. Unknown is not OFF or zero.")
        else:
            verified = result["verified"]
            detail = (
                "Reported configuration matches the requested state."
                if verified
                else "NOT VERIFIED: state did not match, or reads failed."
            )
            if result["write_error"]:
                detail += f"\nWrite error: {result['write_error']}"
            detail += (
                "\nVerification is configuration read-back, " "not an RF measurement."
            )
            messagebox.showinfo("Verification result", detail)
            self.message.set(detail)
            self.update_buttons()
            self.refresh()

        self.update_buttons()
        self.root.after(100, self.process_events)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="https://192.168.178.172")
    parser.add_argument("--verify-ssl", action="store_true")
    parser.add_argument("--allow-network-writes", action="store_true")
    args = parser.parse_args()

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
        allow_writes=args.allow_network_writes,
        verify_ssl=args.verify_ssl,
    )
    root.deiconify()
    Dashboard(root, backend)
    root.mainloop()


if __name__ == "__main__":
    main()
