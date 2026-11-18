#!/usr/bin/env python3
"""
Simple GUI: show joystick-sim BJM/EJM state as seen on an SLCAN adapter.

Usage:
  pip install python-can pyserial
  python joy_bus_monitor.py
  python joy_bus_monitor.py COM7

Defaults: interface=slcan, port=COM7, bitrate=500000.
No fancy UI — just live axes + buttons per known source address.
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk, messagebox
from typing import Dict, List, Optional, Tuple

try:
    import can
except ImportError:
    print("Need python-can:  pip install python-can pyserial")
    sys.exit(1)

# J1939 PGNs (PDU2) used by the Volvo joystick simulator / bridge
PGN_BJM1 = 0x00FDD6
PGN_EJM1 = 0x00FDD7
PGN_BJM2 = 0x00FDD8

# Source addresses from the simulator mappings
SA_NAMES = {
    126: "Hendels base L (stick)",
    127: "Hendels base R (stick)",
    144: "Grip L (L8/A9)",
    145: "Grip R (L8/A9)",
}


def pgn_from_id(can_id: int) -> int:
    return (can_id >> 8) & 0x03FFFF


def sa_from_id(can_id: int) -> int:
    return can_id & 0xFF


def _bit_pair(byte: int, pair: int) -> int:
    """pair 0 = bits 1-2 (LSB) .. pair 3 = bits 7-8 (MSB)."""
    return (byte >> (pair * 2)) & 0x03


def decode_axis(status_byte: int, next_byte: int) -> Tuple[float, str]:
    """Return (signed percent -100..+100, label)."""
    raw = ((next_byte & 0xFF) << 2) | ((status_byte >> 6) & 0x03)
    if raw >= 0x3FF:
        return 0.0, "n/a"
    if raw == 0x3FE:
        return 0.0, "err"
    mag = raw / 10.0  # 0..100 %
    pos = _bit_pair(status_byte, 2)  # +dir bits 5-4
    neg = _bit_pair(status_byte, 1)  # -dir bits 3-2
    neu = _bit_pair(status_byte, 0)  # neutral bits 1-0
    if raw == 0 or neu == 0b01:
        return 0.0, "0"
    if neg == 0b01:
        return -mag, f"-{mag:.1f}"
    if pos == 0b01:
        return mag, f"+{mag:.1f}"
    # Ambiguous: treat as positive magnitude
    return mag, f"+{mag:.1f}"


def decode_buttons(b6: int, b7: int, b8: int) -> List[Optional[bool]]:
    """Buttons 1..12 → True pressed, False released, None not available."""
    out: List[Optional[bool]] = []
    bytes_ = (b6, b7, b8)
    for logical in range(1, 13):
        group = (logical - 1) // 4
        index_in_group = (logical - 1) % 4
        bit_pair = 3 - index_in_group
        st = _bit_pair(bytes_[group], bit_pair)
        if st == 0b11:
            out.append(None)
        elif st == 0b01:
            out.append(True)
        else:
            out.append(False)
    return out


@dataclass
class SourceState:
    sa: int
    name: str
    # BJM main axes
    x: float = 0.0
    y: float = 0.0
    x_txt: str = "—"
    y_txt: str = "—"
    buttons: List[Optional[bool]] = field(default_factory=lambda: [None] * 12)
    # EJM rollers
    roller_a: float = 0.0
    roller_b: float = 0.0
    roller_c: float = 0.0
    roller_a_txt: str = "—"
    roller_b_txt: str = "—"
    roller_c_txt: str = "—"
    # BJM2 mini buttons (A9)
    mini_buttons: List[Optional[bool]] = field(default_factory=lambda: [None] * 12)
    last_ms: float = 0.0
    bjm_hz: float = 0.0
    ejm_hz: float = 0.0
    _last_bjm: float = 0.0
    _last_ejm: float = 0.0

    def touch_bjm(self, now: float) -> None:
        if self._last_bjm > 0:
            dt = now - self._last_bjm
            if dt > 0:
                self.bjm_hz = 0.8 * self.bjm_hz + 0.2 * (1.0 / dt) if self.bjm_hz else 1.0 / dt
        self._last_bjm = now
        self.last_ms = now

    def touch_ejm(self, now: float) -> None:
        if self._last_ejm > 0:
            dt = now - self._last_ejm
            if dt > 0:
                self.ejm_hz = 0.8 * self.ejm_hz + 0.2 * (1.0 / dt) if self.ejm_hz else 1.0 / dt
        self._last_ejm = now
        self.last_ms = now


def apply_frame(states: Dict[int, SourceState], msg: can.Message) -> None:
    if not msg.is_extended_id or msg.dlc < 8 or not msg.data:
        return
    data = bytes(msg.data[:8])
    sa = sa_from_id(msg.arbitration_id)
    pgn = pgn_from_id(msg.arbitration_id)
    now = time.monotonic()

    if sa not in states:
        states[sa] = SourceState(sa=sa, name=SA_NAMES.get(sa, f"SA {sa}"))
    st = states[sa]

    if pgn == PGN_BJM1:
        st.x, st.x_txt = decode_axis(data[0], data[1])
        st.y, st.y_txt = decode_axis(data[2], data[3])
        st.buttons = decode_buttons(data[5], data[6], data[7])
        st.touch_bjm(now)
    elif pgn == PGN_EJM1:
        st.roller_a, st.roller_a_txt = decode_axis(data[0], data[1])
        st.roller_b, st.roller_b_txt = decode_axis(data[2], data[3])
        st.roller_c, st.roller_c_txt = decode_axis(data[4], data[5])
        st.touch_ejm(now)
    elif pgn == PGN_BJM2:
        st.mini_buttons = decode_buttons(data[5], data[6], data[7])
        st.touch_bjm(now)


class SourcePanel(ttk.LabelFrame):
    def __init__(self, master: tk.Misc, sa: int, name: str) -> None:
        super().__init__(master, text=f"{name}  ·  SA {sa}", padding=6)
        self.sa = sa

        self.lbl_rate = ttk.Label(self, text="rate —")
        self.lbl_rate.grid(row=0, column=0, columnspan=4, sticky="w")

        ttk.Label(self, text="X").grid(row=1, column=0, sticky="w")
        self.var_x = tk.StringVar(value="—")
        ttk.Label(self, textvariable=self.var_x, width=10, font=("Consolas", 11)).grid(
            row=1, column=1, sticky="w"
        )
        self.bar_x = ttk.Progressbar(self, length=140, maximum=200, mode="determinate")
        self.bar_x.grid(row=1, column=2, columnspan=2, sticky="we", padx=4)

        ttk.Label(self, text="Y").grid(row=2, column=0, sticky="w")
        self.var_y = tk.StringVar(value="—")
        ttk.Label(self, textvariable=self.var_y, width=10, font=("Consolas", 11)).grid(
            row=2, column=1, sticky="w"
        )
        self.bar_y = ttk.Progressbar(self, length=140, maximum=200, mode="determinate")
        self.bar_y.grid(row=2, column=2, columnspan=2, sticky="we", padx=4)

        ttk.Label(self, text="Rollers A/B/C").grid(row=3, column=0, sticky="w")
        self.var_r = tk.StringVar(value="—  —  —")
        ttk.Label(self, textvariable=self.var_r, font=("Consolas", 10)).grid(
            row=3, column=1, columnspan=3, sticky="w"
        )

        ttk.Label(self, text="Buttons").grid(row=4, column=0, sticky="nw", pady=(6, 0))
        self.btn_frame = ttk.Frame(self)
        self.btn_frame.grid(row=4, column=1, columnspan=3, sticky="w", pady=(6, 0))
        self.btn_labels: List[tk.Label] = []
        for i in range(12):
            lb = tk.Label(
                self.btn_frame,
                text=str(i + 1),
                width=3,
                relief="groove",
                bg="#ddd",
                fg="#333",
            )
            lb.grid(row=0, column=i, padx=1)
            self.btn_labels.append(lb)

        self.mini_frame = ttk.Frame(self)
        self.mini_frame.grid(row=5, column=1, columnspan=3, sticky="w", pady=(4, 0))
        self.mini_labels: List[tk.Label] = []
        ttk.Label(self, text="Mini (BJM2)").grid(row=5, column=0, sticky="nw")
        for i in range(12):
            lb = tk.Label(
                self.mini_frame,
                text=str(i + 1),
                width=3,
                relief="groove",
                bg="#ddd",
                fg="#333",
            )
            lb.grid(row=0, column=i, padx=1)
            self.mini_labels.append(lb)

    @staticmethod
    def _bar_value(percent: float) -> float:
        # Map -100..+100 → 0..200 (100 = center)
        return max(0.0, min(200.0, 100.0 + percent))

    @staticmethod
    def _paint_buttons(labels: List[tk.Label], states: List[Optional[bool]]) -> None:
        for i, lb in enumerate(labels):
            st = states[i] if i < len(states) else None
            if st is None:
                lb.configure(bg="#e8e8e8", fg="#aaa")
            elif st:
                lb.configure(bg="#2ecc71", fg="white")
            else:
                lb.configure(bg="#555", fg="white")

    def update_from(self, st: SourceState, now: float) -> None:
        age = now - st.last_ms if st.last_ms else 999.0
        live = "live" if age < 0.5 else f"stale {age:.1f}s"
        self.lbl_rate.configure(
            text=f"{live}  ·  BJM ~{st.bjm_hz:.0f} Hz  ·  EJM ~{st.ejm_hz:.0f} Hz"
        )
        self.var_x.set(st.x_txt)
        self.var_y.set(st.y_txt)
        self.bar_x["value"] = self._bar_value(st.x)
        self.bar_y["value"] = self._bar_value(st.y)
        self.var_r.set(f"{st.roller_a_txt}   {st.roller_b_txt}   {st.roller_c_txt}")
        self._paint_buttons(self.btn_labels, st.buttons)
        self._paint_buttons(self.mini_labels, st.mini_buttons)


class JoyBusMonitorApp:
    def __init__(self, default_channel: str = "COM7") -> None:
        self.root = tk.Tk()
        self.root.title("Joystick bus monitor (SLCAN)")
        self.root.minsize(520, 400)

        self.bus: Optional[can.BusABC] = None
        self.rx_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.msg_q: queue.Queue = queue.Queue(maxsize=2000)
        self.states: Dict[int, SourceState] = {}
        self.panels: Dict[int, SourcePanel] = {}
        self.frame_count = 0

        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="Interface").grid(row=0, column=0, sticky="w")
        self.var_iface = tk.StringVar(value="slcan")
        ttk.Entry(top, textvariable=self.var_iface, width=10).grid(row=0, column=1, padx=4)

        ttk.Label(top, text="Channel").grid(row=0, column=2, sticky="w")
        self.var_channel = tk.StringVar(value=default_channel)
        ttk.Entry(top, textvariable=self.var_channel, width=10).grid(row=0, column=3, padx=4)

        ttk.Label(top, text="Bitrate").grid(row=0, column=4, sticky="w")
        self.var_bitrate = tk.StringVar(value="500000")
        ttk.Entry(top, textvariable=self.var_bitrate, width=10).grid(row=0, column=5, padx=4)

        self.btn_connect = ttk.Button(top, text="Connect", command=self.toggle_connect)
        self.btn_connect.grid(row=0, column=6, padx=8)

        self.var_status = tk.StringVar(value="Disconnected")
        ttk.Label(top, textvariable=self.var_status).grid(row=1, column=0, columnspan=7, sticky="w", pady=(6, 0))

        # Preferred order of known SAs
        body = ttk.Frame(self.root, padding=8)
        body.pack(fill="both", expand=True)
        self.body = body

        for sa in (126, 144, 127, 145):
            panel = SourcePanel(body, sa, SA_NAMES[sa])
            panel.pack(fill="x", pady=4)
            self.panels[sa] = panel
            self.states[sa] = SourceState(sa=sa, name=SA_NAMES[sa])

        # Catch-all for unexpected SAs
        self.other = ttk.LabelFrame(body, text="Other SAs (raw)", padding=6)
        self.other.pack(fill="x", pady=4)
        self.var_other = tk.StringVar(value="(none yet)")
        ttk.Label(self.other, textvariable=self.var_other, font=("Consolas", 9)).pack(anchor="w")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(50, self.ui_tick)

    def toggle_connect(self) -> None:
        if self.bus is not None:
            self.disconnect()
        else:
            self.connect()

    def connect(self) -> None:
        iface = self.var_iface.get().strip()
        channel = self.var_channel.get().strip()
        try:
            bitrate = int(self.var_bitrate.get().strip())
        except ValueError:
            messagebox.showerror("Bitrate", "Bitrate must be an integer")
            return

        ch: object = channel
        if iface in ("kvaser", "ixxat"):
            try:
                ch = int(channel.split(":", 1)[0].strip())
            except ValueError:
                pass

        try:
            kwargs = {"interface": iface, "channel": ch, "bitrate": bitrate}
            if iface == "slcan":
                kwargs["sleep_after_open"] = 0.3
            elif iface == "ixxat":
                kwargs["extended"] = True
                kwargs["rx_fifo_size"] = 256
                if ":" in channel:
                    hwid = channel.split(":", 1)[1].strip()
                    if hwid:
                        kwargs["unique_hardware_id"] = hwid
            self.bus = can.Bus(**kwargs)
        except Exception as e:
            messagebox.showerror("Connect failed", str(e))
            self.bus = None
            return

        self.stop_event.clear()
        self.rx_thread = threading.Thread(target=self._rx_loop, name="can-rx", daemon=True)
        self.rx_thread.start()
        self.btn_connect.configure(text="Disconnect")
        self.var_status.set(f"Connected  {iface}:{channel} @ {bitrate}")
        self.frame_count = 0

    def disconnect(self) -> None:
        self.stop_event.set()
        if self.rx_thread is not None:
            self.rx_thread.join(timeout=1.0)
            self.rx_thread = None
        if self.bus is not None:
            try:
                self.bus.shutdown()
            except Exception:
                pass
            self.bus = None
        self.btn_connect.configure(text="Connect")
        self.var_status.set("Disconnected")

    def _rx_loop(self) -> None:
        assert self.bus is not None
        while not self.stop_event.is_set():
            try:
                msg = self.bus.recv(timeout=0.05)
            except Exception as e:
                try:
                    self.msg_q.put_nowait(("err", str(e)))
                except queue.Full:
                    pass
                time.sleep(0.1)
                continue
            if msg is None:
                continue
            try:
                self.msg_q.put_nowait(("msg", msg))
            except queue.Full:
                pass

    def ui_tick(self) -> None:
        # Drain queue
        n = 0
        while n < 500:
            try:
                kind, payload = self.msg_q.get_nowait()
            except queue.Empty:
                break
            n += 1
            if kind == "err":
                self.var_status.set(f"Bus error: {payload}")
            else:
                apply_frame(self.states, payload)
                self.frame_count += 1

        now = time.monotonic()
        for sa, panel in self.panels.items():
            panel.update_from(self.states[sa], now)

        # Other SAs
        known = set(self.panels)
        extras = []
        for sa, st in sorted(self.states.items()):
            if sa in known:
                continue
            if st.last_ms and (now - st.last_ms) < 2.0:
                extras.append(
                    f"SA {sa}: X={st.x_txt} Y={st.y_txt}  "
                    f"R={st.roller_a_txt}/{st.roller_b_txt}/{st.roller_c_txt}"
                )
        self.var_other.set("\n".join(extras) if extras else "(none yet)")

        if self.bus is not None:
            self.var_status.set(
                f"Connected  {self.var_iface.get()}:{self.var_channel.get()}  "
                f"·  frames {self.frame_count}"
            )

        self.root.after(50, self.ui_tick)

    def on_close(self) -> None:
        self.disconnect()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    ap = argparse.ArgumentParser(description="Simple SLCAN joystick BJM/EJM monitor")
    ap.add_argument(
        "channel",
        nargs="?",
        default="COM7",
        help="CAN channel (default COM7 for slcan)",
    )
    ap.add_argument("--interface", default="slcan", help="python-can interface")
    ap.add_argument("--bitrate", type=int, default=500_000)
    args = ap.parse_args()

    app = JoyBusMonitorApp(default_channel=str(args.channel))
    app.var_iface.set(args.interface)
    app.var_bitrate.set(str(args.bitrate))
    app.run()


if __name__ == "__main__":
    main()
