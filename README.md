# esp32c6-slcan

Minimal ESP32-C6 USB-CAN adapter using the Lawicel SLCAN protocol over USB CDC.

- **Target**: ESP32-C6 + MCP2562 transceiver
- **Pins**: TWAI TX = GPIO1, TWAI RX = GPIO2
- **Bitrate**: 500 kbit/s (S6) primary, supports standard Lawicel speeds
- **Protocol**: SLCAN (compatible with python-can `slcan` interface, SavvyCAN, etc.)
- **Hardware**: MCP2562 (standard pinout, same as many SN65HVD230 examples)

This is a quick adaptation of the excellent TWAI-based sketch from https://github.com/mintynet/esp32-slcan (esp32-twai-can folder) for the user's exact hardware and 500 kbit/s monitoring use-case with a Kvaser Leaf Light v2.

## Tested & Working (2026-07-14)

Confirmed working on ESP32-C6 with MCP2562 (GPIO1=TX, GPIO2=RX):

- `python -m can.viewer -i slcan -c COM8 --bitrate 500000`
- CanMoon (via Kvaser Leaf Light v2) sending messages over the bus
- Bidirectional traffic at 500 kbit/s

The adapter appears as a standard SLCAN device on COM8 after flashing.

## Quick Start (Arduino IDE)

1. Install Arduino IDE + ESP32 board support (3.x+ recommended for TWAI).
2. Open `esp32c6-slcan.ino`
3. Select your ESP32-C6 board (e.g. "ESP32-C6 Dev Module").
4. **Important**: Enable "USB CDC On Boot" in the Tools menu if the option is present for the board.
5. Flash to your board (COM8 in this case).
6. The device should appear as a new COM port (or the same one) speaking SLCAN.

## Usage with python-can (recommended)

```bash
pip install python-can pyserial
python -m can.viewer -i slcan -c COMxx --bitrate 500000
```

Or in code:

```python
import can
bus = can.Bus(interface='slcan', channel='COMxx', bitrate=500000)
# send / receive as usual
bus.shutdown()
```

## Commands (standard Lawicel SLCAN)

- `S6` + `O` : 500 kbit/s, open
- `C` : close
- `t<id><len><data>` / `T<...>` for transmit (std/ext)
- Received frames come back in the same format

See the original sketch for full supported commands (V, N, Z for timestamps, etc.).

## Wiring

- ESP32-C6 GPIO1 → MCP2562 TXD
- ESP32-C6 GPIO2 ← MCP2562 RXD
- MCP2562 CANH / CANL to the bus
- Shared ground with the bus / Kvaser
- 120 Ω termination at the ends of the CAN bus

Use your MCP2562 datasheet for power (typically 5V or 3.3V supply depending on variant) and exact pinout.

## Testing with Kvaser Leaf Light v2

1. Connect both devices to the same CAN bus (twisted pair + common GND + termination).
2. Set Kvaser to 500 kbit/s.
3. Open the ESP SLCAN port with python-can or SavvyCAN at 500 kbit/s.
4. Send frames from Kvaser → verify in slcan client.
5. Send from slcan client → verify in Kvaser tool.

Success = clean bidirectional traffic at 500 kbit/s with matching IDs/DLC/data.

## Notes

- This is deliberately minimal (no extra features).
- The original sketch used legacy `driver/twai.h`. It works for quick results.
- If you run into issues specific to ESP32-C6 (e.g. some speeds or TX/RX), a tiny pure ESP-IDF version using the modern driver can be added later.
- Repo created fresh for this hardware setup.

## Credits

Adapted from https://github.com/mintynet/esp32-slcan (Ian Tabor / mintynet) — thank you for the solid SLCAN + TWAI implementation.

For the user's exact setup (ESP32-C6, GPIO1/2, MCP2562, Kvaser verification, python-can usage).