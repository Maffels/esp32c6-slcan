#!/usr/bin/env python3
"""CAN joystick monitor GUI.

Adapter (slcan / kvaser / virtual), latest frame per source, BJM/EJM decode.

Usage:
    python slcan_monitor_gui.py
    python slcan_monitor_gui.py COM8
    slcan-monitor
"""

from __future__ import annotations

import os
import queue
import struct
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import messagebox, ttk
from typing import Optional


def _ensure_kvaser_dll_search_path() -> None:
    if sys.platform != "win32":
        return
    add_dll_directory = getattr(os, "add_dll_directory", None)
    if add_dll_directory is None:
        return
    candidates = []
    env_bin = os.environ.get("KVASER_BIN")
    if env_bin:
        candidates.append(env_bin)
    program_files = [
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
    ]
    subdir = "bin_x64" if struct.calcsize("P") * 8 == 64 else "Bin"
    for root in program_files:
        candidates.append(os.path.join(root, "Kvaser", "Canlib", subdir))
    for path in candidates:
        if path and os.path.isdir(path):
            try:
                add_dll_directory(path)
            except OSError:
                continue
            break


_ensure_kvaser_dll_search_path()

import can

PGN_BJM1 = 0x00FDD6
PGN_EJM1 = 0x00FDD7
PGN_BJM2 = 0x00FDD8
PGN_NAMES = {PGN_BJM1: "BJM1", PGN_EJM1: "EJM1", PGN_BJM2: "BJM2"}

STATUS = {0: "not in pos", 1: "in pos", 2: "error", 3: "n/a"}
BUTTON = {0: "released", 1: "pressed", 2: "error", 3: "n/a"}

ADAPTERS = ("slcan", "kvaser", "virtual")
ADAPTER_CHANNEL = {"slcan": "COM8", "kvaser": "0", "virtual": "0"}

POLL_MS = 50
DEFAULT_BITRATE = 500000
DEFAULT_DATA_BITRATE = 0


def pgn_of(can_id: int) -> int:
    return (can_id >> 8) & 0x3FFFF


def sa_of(can_id: int) -> int:
    return can_id & 0xFF


def prio_of(can_id: int) -> int:
    return (can_id >> 26) & 0x7


def pair(data: bytes, byte_i: int, shift: int) -> int:
    if byte_i >= len(data):
        return 3
    return (data[byte_i] >> shift) & 0x03


def axis_raw(data: bytes, start: int) -> int:
    if start + 1 >= len(data):
        return 0
    return ((data[start + 1] & 0xFF) << 2) | ((data[start] >> 6) & 0x03)


def axis_decode(data: bytes, start: int) -> dict:
    raw = axis_raw(data, start)
    pos = pair(data, start, 4)
    neg = pair(data, start, 2)
    neut = pair(data, start, 0)
    unused = raw >= 0x3FF
    if unused:
        signed = 0.0
        text = "n/a"
    else:
        pct = raw / 10.0
        if neg == 1:
            signed = -pct
        elif pos == 1:
            signed = pct
        elif neut == 1 or raw == 0:
            signed = 0.0
        else:
            signed = pct
        if signed == 0.0:
            text = "0.0 %"
        else:
            text = f"{signed:+.1f} %"
    return {
        "raw": raw,
        "signed": signed,
        "text": text,
        "unused": unused,
        "pos": pos,
        "neg": neg,
        "neut": neut,
    }


def button_status(data: bytes, index: int) -> int:
    byte_i = 5 + (index // 4)
    shift = 6 - ((index % 4) * 2)
    return pair(data, byte_i, shift)


def decode_joystick(can_id: int, data: bytes) -> Optional[dict]:
    pgn = pgn_of(can_id)
    name = PGN_NAMES.get(pgn)
    if name is None or len(data) < 8:
        return None
    out = {
        "type": name,
        "pgn": pgn,
        "sa": sa_of(can_id),
        "prio": prio_of(can_id),
        "can_id": can_id,
        "data": data,
        "axes": {},
        "buttons": [],
    }
    if pgn in (PGN_BJM1, PGN_BJM2):
        out["axes"]["X"] = axis_decode(data, 0)
        out["axes"]["Y"] = axis_decode(data, 2)
        out["axes"]["X"]["detent"] = pair(data, 4, 6)
        out["axes"]["Y"]["detent"] = pair(data, 4, 4)
        out["buttons"] = [button_status(data, i) for i in range(12)]
    else:
        out["axes"]["Thumb A"] = axis_decode(data, 0)
        out["axes"]["Thumb B"] = axis_decode(data, 2)
        out["axes"]["Thumb C"] = axis_decode(data, 4)
        out["axes"]["Thumb A"]["detent"] = pair(data, 6, 6)
        out["axes"]["Thumb B"]["detent"] = pair(data, 6, 4)
        out["axes"]["Thumb C"]["detent"] = pair(data, 6, 2)
    return out


def source_key(can_id: int) -> tuple:
    return (pgn_of(can_id), sa_of(can_id))


def traffic_iid(can_id: int, extended: bool) -> str:
    return f"{1 if extended else 0}:{can_id:08X}"


def parse_channel(interface: str, text: str):
    text = text.strip()
    if interface == "kvaser":
        try:
            return int(text)
        except ValueError:
            return text
    return text or ADAPTER_CHANNEL[interface]


@dataclass
class Source:
    key: tuple
    decoded: dict
    last_seen: float = field(default_factory=time.time)
    count: int = 0


class SlcanMonitorGui:
    def __init__(self, port: str, interface: str = "slcan") -> None:
        self.bus: Optional[can.BusABC] = None
        self.rx_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.q: queue.Queue = queue.Queue()
        self.sources: dict[tuple, Source] = {}
        self.selected_key: Optional[tuple] = None
        self.traffic_counts: dict[str, int] = {}

        self.root = tk.Tk()
        self.root.title("CAN joystick monitor")
        self.root.geometry("1100x720")
        self.interface_var = tk.StringVar(value=interface if interface in ADAPTERS else "slcan")
        self.channel_var = tk.StringVar(value=port)
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(POLL_MS, self._drain)

    def _build(self) -> None:
        bar = ttk.Frame(self.root, padding=8)
        bar.pack(fill="x")

        ttk.Label(bar, text="Adapter").pack(side="left")
        iface = ttk.Combobox(
            bar,
            textvariable=self.interface_var,
            values=ADAPTERS,
            width=10,
            state="readonly",
        )
        iface.pack(side="left", padx=(4, 10))
        iface.bind("<<ComboboxSelected>>", self._on_adapter_change)

        self.channel_lbl = ttk.Label(bar, text="Channel")
        self.channel_lbl.pack(side="left")
        ttk.Entry(bar, textvariable=self.channel_var, width=12).pack(side="left", padx=(4, 8))
        self._on_adapter_change()

        self.btn_conn = ttk.Button(bar, text="Connect", command=self._toggle)
        self.btn_conn.pack(side="left")
        self.status_var = tk.StringVar(value="Disconnected")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left", padx=12)

        mid = ttk.Panedwindow(self.root, orient="horizontal")
        mid.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        src_fr = ttk.Labelframe(mid, text="BJM / EJM sources", padding=4)
        self.src_tree = ttk.Treeview(
            src_fr,
            columns=("type", "sa", "prio", "id"),
            show="headings",
            height=10,
            selectmode="browse",
        )
        for col, text, width, anchor in (
            ("type", "Type", 70, "w"),
            ("sa", "Address", 70, "center"),
            ("prio", "Priority", 70, "center"),
            ("id", "CAN ID", 110, "w"),
        ):
            self.src_tree.heading(col, text=text)
            self.src_tree.column(col, width=width, anchor=anchor, stretch=True)
        sy = ttk.Scrollbar(src_fr, orient="vertical", command=self.src_tree.yview)
        self.src_tree.configure(yscrollcommand=sy.set)
        self.src_tree.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")
        self.src_tree.bind("<<TreeviewSelect>>", self._on_select)
        mid.add(src_fr, weight=1)

        det_fr = ttk.Labelframe(mid, text="Selected source", padding=6)
        self.meta_var = tk.StringVar(value="Select a BJM/EJM source")
        ttk.Label(det_fr, textvariable=self.meta_var).pack(anchor="w", pady=(0, 6))

        ttk.Label(det_fr, text="Axes").pack(anchor="w")
        self.axis_tree = ttk.Treeview(
            det_fr,
            columns=("axis", "value", "pos", "neg", "neut", "detent"),
            show="headings",
            height=4,
            selectmode="none",
        )
        for col, text, width in (
            ("axis", "Axis", 80),
            ("value", "Value", 80),
            ("pos", "+ dir", 80),
            ("neg", "− dir", 80),
            ("neut", "Neutral", 80),
            ("detent", "Detent", 80),
        ):
            self.axis_tree.heading(col, text=text)
            self.axis_tree.column(col, width=width, anchor="center", stretch=True)
        self.axis_tree.pack(fill="x", pady=(0, 8))

        ttk.Label(det_fr, text="Buttons").pack(anchor="w")
        self.btn_tree = ttk.Treeview(
            det_fr,
            columns=("n", "state"),
            show="headings",
            height=12,
            selectmode="none",
        )
        self.btn_tree.heading("n", text="#")
        self.btn_tree.heading("state", text="State")
        self.btn_tree.column("n", width=40, anchor="center", stretch=False)
        self.btn_tree.column("state", width=120, anchor="w", stretch=True)
        self.btn_tree.pack(fill="both", expand=True)
        mid.add(det_fr, weight=2)

        log_fr = ttk.Labelframe(self.root, text="Latest message per source", padding=4)
        log_fr.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log = ttk.Treeview(
            log_fr,
            columns=("id", "type", "dlc", "data", "count", "time"),
            show="headings",
            height=10,
            selectmode="browse",
        )
        for col, text, width, anchor in (
            ("id", "CAN ID", 110, "w"),
            ("type", "Type", 70, "w"),
            ("dlc", "DLC", 40, "center"),
            ("data", "Data", 220, "w"),
            ("count", "Count", 60, "e"),
            ("time", "Last", 80, "center"),
        ):
            self.log.heading(col, text=text)
            self.log.column(col, width=width, anchor=anchor, stretch=(col == "data"))
        ly = ttk.Scrollbar(log_fr, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=ly.set)
        self.log.pack(side="left", fill="both", expand=True)
        ly.pack(side="right", fill="y")

    def _on_adapter_change(self, _event=None) -> None:
        iface = self.interface_var.get()
        self.channel_lbl.configure(text="Port" if iface == "slcan" else "Channel")
        current = self.channel_var.get().strip()
        if current in ADAPTER_CHANNEL.values() or current == "":
            self.channel_var.set(ADAPTER_CHANNEL[iface])

    def _toggle(self) -> None:
        if self.bus is None:
            self._connect()
        else:
            self._disconnect()

    def _connect(self) -> None:
        iface = self.interface_var.get().strip() or "slcan"
        channel = parse_channel(iface, self.channel_var.get())
        try:
            if iface == "slcan":
                bus = can.Bus(interface="slcan", channel=str(channel), sleep_after_open=0.2)
                bus.set_bitrate(DEFAULT_BITRATE, DEFAULT_DATA_BITRATE)
            elif iface == "kvaser":
                bus = can.Bus(interface="kvaser", channel=channel, bitrate=DEFAULT_BITRATE)
            else:
                bus = can.Bus(interface=iface, channel=channel, bitrate=DEFAULT_BITRATE)
        except Exception as exc:
            messagebox.showerror("CAN", f"Could not open {iface}:{channel}:\n{exc}")
            return
        self.bus = bus
        self.stop_event.clear()
        self.rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self.rx_thread.start()
        self.btn_conn.configure(text="Disconnect")
        self.status_var.set(f"{iface}:{channel} @ {DEFAULT_BITRATE}")

    def _disconnect(self) -> None:
        self.stop_event.set()
        bus = self.bus
        self.bus = None
        if bus is not None:
            try:
                bus.shutdown()
            except Exception:
                pass
        self.btn_conn.configure(text="Connect")
        self.status_var.set("Disconnected")

    def _rx_loop(self) -> None:
        while not self.stop_event.is_set() and self.bus is not None:
            try:
                msg = self.bus.recv(timeout=0.1)
            except Exception:
                break
            if msg is not None:
                self.q.put(msg)

    def _drain(self) -> None:
        n = 0
        while n < 200:
            try:
                msg = self.q.get_nowait()
            except queue.Empty:
                break
            n += 1
            self._handle(msg)
        self.root.after(POLL_MS, self._drain)

    def _handle(self, msg: can.Message) -> None:
        can_id = int(msg.arbitration_id)
        data = bytes(msg.data)
        ts = time.strftime("%H:%M:%S")
        hexdata = " ".join(f"{b:02X}" for b in data)
        kind = PGN_NAMES.get(pgn_of(can_id), "")
        iid = traffic_iid(can_id, bool(msg.is_extended_id))
        count = self.traffic_counts.get(iid, 0) + 1
        self.traffic_counts[iid] = count
        values = (f"0x{can_id:08X}", kind or "—", str(len(data)), hexdata, str(count), ts)
        if self.log.exists(iid):
            self.log.item(iid, values=values)
        else:
            self.log.insert("", "end", iid=iid, values=values)

        decoded = decode_joystick(can_id, data)
        if decoded is None:
            return
        key = source_key(can_id)
        src = self.sources.get(key)
        row = (
            decoded["type"],
            str(decoded["sa"]),
            str(decoded["prio"]),
            f"0x{can_id:08X}",
        )
        if src is None:
            src = Source(key=key, decoded=decoded, count=1)
            self.sources[key] = src
            self.src_tree.insert("", "end", iid=self._iid(key), values=row)
        else:
            src.decoded = decoded
            src.last_seen = time.time()
            src.count += 1
            self.src_tree.item(self._iid(key), values=row)
        if self.selected_key == key:
            self._render_detail(decoded)
        elif self.selected_key is None:
            self.src_tree.selection_set(self._iid(key))
            self.selected_key = key
            self._render_detail(decoded)

    @staticmethod
    def _iid(key: tuple) -> str:
        return f"{key[0]:05X}:{key[1]:02X}"

    def _on_select(self, _event=None) -> None:
        sel = self.src_tree.selection()
        if not sel:
            return
        iid = sel[0]
        pgn_s, sa_s = iid.split(":")
        key = (int(pgn_s, 16), int(sa_s, 16))
        self.selected_key = key
        src = self.sources.get(key)
        if src:
            self._render_detail(src.decoded)

    def _render_detail(self, d: dict) -> None:
        self.meta_var.set(
            f"{d['type']}   SA {d['sa']}   priority {d['prio']}   "
            f"ID 0x{d['can_id']:08X}   PGN 0x{d['pgn']:05X}"
        )
        for item in self.axis_tree.get_children():
            self.axis_tree.delete(item)
        for name, ax in d["axes"].items():
            detent = ax.get("detent")
            self.axis_tree.insert(
                "",
                "end",
                values=(
                    name,
                    "n/a" if ax["unused"] else ax["text"],
                    STATUS[ax["pos"]],
                    STATUS[ax["neg"]],
                    STATUS[ax["neut"]],
                    STATUS.get(detent, "—") if detent is not None else "—",
                ),
            )
        for item in self.btn_tree.get_children():
            self.btn_tree.delete(item)
        if d["buttons"]:
            for i, st in enumerate(d["buttons"], start=1):
                self.btn_tree.insert("", "end", values=(str(i), BUTTON[st]))

    def _on_close(self) -> None:
        self._disconnect()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    interface = "slcan"
    port = ADAPTER_CHANNEL["slcan"]
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args and args[0].lower() in ADAPTERS:
        interface = args[0].lower()
        port = args[1] if len(args) > 1 else ADAPTER_CHANNEL[interface]
    elif args:
        port = args[0]
    SlcanMonitorGui(port, interface).run()


if __name__ == "__main__":
    main()
