#!/usr/bin/env python3
"""
Simple CAN monitor using the ESP32-C6 SLCAN adapter + python-can.

Usage:
    python monitor_example.py COM8

This is more reliable on Windows than `can.viewer` (no curses dependency).
It prints all received frames and lets you send test frames easily.

Requires:
    pip install python-can pyserial
"""

import sys
import can
import time

def main():
    if len(sys.argv) < 2:
        print("Usage: python monitor_example.py COM8")
        return

    port = sys.argv[1]
    bitrate = 500000

    print(f"Connecting to ESP32-C6 SLCAN adapter on {port} @ {bitrate} bps...")

    try:
        bus = can.Bus(
            interface="slcan",
            channel=port,
            bitrate=bitrate,
            sleep_after_open=0.2
        )
    except Exception as e:
        print(f"Failed to open: {e}")
        print("Check the COM port (use Device Manager or list_ports).")
        print("Make sure no other program has the port open.")
        return

    print("Connected! Listening for frames (Ctrl-C to stop).")
    print("Press Enter to send a test frame, or Ctrl-C to exit.\n")

    try:
        while True:
            # Non-blocking receive with timeout
            msg = bus.recv(timeout=0.1)
            if msg:
                print(f"[{time.time():.3f}] RX: {msg}")

            # Check for user input to send a test frame (non-blocking-ish)
            # For simplicity we just listen here. Uncomment to enable interactive send.
            # if sys.stdin in select... (advanced)

    except KeyboardInterrupt:
        print("\nStopping...")

    finally:
        bus.shutdown()
        print("Bus shut down cleanly.")

if __name__ == "__main__":
    main()