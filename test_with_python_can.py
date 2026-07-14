#!/usr/bin/env python3
"""
Simple test script for the ESP32-C6 SLCAN adapter.
Usage:
  python test_with_python_can.py COM8

Requires:
  pip install python-can pyserial
"""

import sys
import can
import time

def main():
    if len(sys.argv) < 2:
        print("Usage: python test_with_python_can.py COMxx")
        print("Example: python test_with_python_can.py COM9")
        return

    port = sys.argv[1]
    bitrate = 500000

    print(f"Connecting to slcan on {port} @ {bitrate} bps...")

    try:
        bus = can.Bus(interface="slcan", channel=port, bitrate=bitrate)
    except Exception as e:
        print(f"Failed to open bus: {e}")
        print("Make sure the ESP is flashed, the correct COM port is used,")
        print("and no other program (Arduino Serial Monitor, etc.) has the port open.")
        return

    print("Bus opened. Sending a test frame every 1s. Press Ctrl-C to stop.")
    print("Also listening for frames (from Kvaser or other nodes).")

    count = 0
    try:
        while True:
            # Send a test frame
            msg = can.Message(
                arbitration_id=0x123 + (count % 10),
                data=[0xDE, 0xAD, 0xBE, 0xEF, count % 256, 0x00, 0x01, 0x02],
                is_extended_id=False,
            )
            try:
                bus.send(msg)
                print(f"Sent: {msg}")
            except can.CanError as e:
                print(f"Send error: {e}")

            # Try to receive any incoming
            rx = bus.recv(timeout=0.2)
            if rx:
                print(f"Received: {rx}")

            count += 1
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        bus.shutdown()
        print("Bus closed.")

if __name__ == "__main__":
    main()