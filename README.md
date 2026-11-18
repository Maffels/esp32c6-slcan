# esp32c6-slcan

ESP32-C6 USB-CAN adapter speaking the Lawicel SLCAN protocol over the C6's USB-Serial/JTAG port.

- **Target**: ESP32-C6 + MCP2562 transceiver
- **Pins**: TWAI TX = GPIO1, TWAI RX = GPIO2
- **Firmware**: ESP-IDF **6.1.1**, on-chip TWAI node driver (this folder). The original Arduino sketch is kept in [`arduino/`](arduino/esp32c6-slcan/) for reference only.
- **Bitrate**: 500 kbit/s (S6) default, all standard Lawicel speeds S0–S8. Sample point 87.5 % (J1939-14), 80 % above 500k
- **Protocol**: SLCAN, works unchanged with python-can `slcan`, SavvyCAN, `can.viewer`

## Build and flash

```powershell
. C:\Espressif\tools\Microsoft.v6.1.1.PowerShell_profile.ps1
cd <esp32c6-slcan>
idf.py set-target esp32c6     # first time only
idf.py build
python -m esptool --port COMx chip-id   # must say ESP32-C6, never flash the bridge's C5
idf.py -p COMx flash -a
```

Logs go to UART0 (GPIO16), never to USB: the USB port carries only SLCAN.

Opening the COM port with pyserial's default DTR/RTS resets the C6 (USB-Serial/JTAG auto-reset), so the ROM boot text can arrive first and the `?` counters restart at 0. Open with `dtr=False, rts=False` to keep them across sessions.

## Protocol

| Command | Reply | Notes |
|---------|-------|-------|
| `S0`..`S8` | CR / BEL | 10k..1M, only while closed |
| `O` / `L` / `C` | CR / BEL | open normal / listen-only / close. `O` while open in the same mode = CR |
| `tiiildd..` `Tiiiiiiiildd..` `riiil` `Riiiiiiiil` | `z` / `Z` + CR, or BEL | strict: hex only, exact length for the DLC. BEL = closed, listen-only, malformed, TX queue full |
| `Z0` / `Z1` | CR | Lawicel timestamps (ms mod 60000, from the TWAI RX timestamp) |
| `F` | `Fxx` CR | Lawicel flags, cleared on read: 01 RX full, 02 TX full, 04 error warning, 08 overrun, 20 error passive, 80 bus error / bus-off |
| `V` / `N` | `V0200` / `NC6SL` | |
| `M` / `m` | CR | ignored: always accept all |
| `?` / `?C` | `?rx=..,rx_overrun=..,usb_drop=..,tx_ok=..,tx_fail=..,tx_nack=..,cmd_err=..,bus_err=..,bus_off=..,state=..,open=..,bitrate=..` | counters (`?C` = read and clear). Not Lawicel |

python-can's `slcan` interface ignores the `z`/`Z`/BEL replies.

## Design

- **CAN RX:** the ISR copies each frame into a 1024-frame ring; a writer task drains the whole ring per wake-up into one USB write (16 KB USB TX buffer). Ring full = `rx_overrun`; host not reading = `usb_drop`.
- **Host TX:** a reader task parses lines and queues frames into a 1024-slot pool feeding the TWAI TX queue (FIFO, ~280 ms of bus time). It never blocks: full = BEL + `tx_nack`. Reason: the IDF USB-Serial/JTAG driver drops whole USB packets when its RX ring is full, without counting them, and on Windows each pyserial `write()` is one packet, so a blocking reader loses whole commands silently.
- Retransmit until acked (standard CAN). Bus-off: the node is closed and reopened after 100 ms (`bus_off` counter).

## Measured (2026-11-18, direct Kvaser Leaf <-> C6, 120 ohm, 500 kbit/s, 29-bit DLC 8)

Tool: `ESP_Can_Bridge_v01/tools/bench/slcan_throughput.py`. A full bus is about 3,480 frames/s.

| Firmware + host reader | Kvaser -> C6 (RX), lossless up to | at full bus | C6 -> Kvaser (TX), lossless up to | above that |
|------------------------|-----------------------------------|-------------|-----------------------------------|-----------|
| Arduino + stock python-can | ~2,000/s (0.1 % at 1-2k) | 2,570/s, 25 % lost | 1,000/s | 45 % lost at 2,000/s, out-of-order and corrupt IDs |
| Arduino + `FastSlcanBus` | same | same | same | same |
| **IDF + `FastSlcanBus`** | **3,483/s (full bus)** | **0 lost** | **3,400/s (97 % bus)** | flood at 11,500/s: excess refused with BEL (`tx_nack`), ~10 % still not accounted for |

Stock python-can on the IDF firmware (through the bridge) also received a full bus and sent 3,000/s without loss.

Open: under a host flood far above bus capacity, ~10 % of the excess commands are not in any counter (most likely still dropped in the USB driver RX ring). Keep host TX at or below bus capacity.

## Wiring

- ESP32-C6 GPIO1 → MCP2562 TXD
- ESP32-C6 GPIO2 ← MCP2562 RXD
- MCP2562 CANH / CANL to the bus
- Shared ground with the bus / Kvaser
- 120 Ω termination at the ends of the CAN bus (a short direct bench link to a Kvaser works with one 120 Ω)

Use your MCP2562 datasheet for power (typically 5V or 3.3V supply depending on variant) and exact pinout.

## Usage with python-can

```bash
pip install python-can pyserial
cd esp32c6-slcan
python slcan_monitor_gui.py
python slcan_monitor_gui.py COM8
```

GUI: pick adapter (**slcan** / **kvaser** / **ixxat** / virtual) and bitrate **500 kbit/s** (S6, default) or **250 kbit/s** (S5). Latest frame per CAN ID (not a scrollback). Auto-scan BJM1 / EJM1 / BJM2 (type, SA, priority). Select a source for axis/button tables.

- **slcan** — ESP32-C6 adapter, channel = COM port (e.g. `COM8`)
- **kvaser** — Leaf Light, channel = `0`
- **ixxat** — USB-to-CAN V2 (HMS VCI, `vcinpl.dll`). Channel `0` is CAN-A (Compact has one channel). Optional hardware id: `0:HW441489`. Needs the IXXAT VCI driver installed.

CLI: `python slcan_monitor_gui.py COM8 --bitrate 250000` or `python slcan_monitor_gui.py ixxat 0`.

Joystick-sim view: `python joy_bus_monitor.py COM8` (live BJM/EJM axes and buttons per source address).

Offline: `python -m pytest test_bitrate_modes.py`.

Terminal log only: `python monitor_example.py COM8` or `python monitor_example.py COM8 250000`.

Optional PATH name `slcan-monitor` is **not** created by `git pull`. This repo has `slcan-monitor.cmd` (GUI; `--cli` → `monitor_example.py`). On a new Windows lab PC the local agent must recreate `C:\code\bin` wrappers — full recipe: sibling **`ESP_Can_Bridge_v01/documentation/lab-path-wrappers.md`**. Short form: ensure `C:\code\bin` is on the user PATH and contains:

```bat
@echo off
call "%~dp0..\repos\esp32c6-slcan\slcan-monitor.cmd" %*
```

```bash
python -m can.viewer -i slcan -c COMxx --bitrate 500000
```

Or in code:

```python
import can
bus = can.Bus(interface='slcan', channel='COMxx', bitrate=500000)
# send / receive as usual
bus.shutdown()
```

For high rates on Windows, `ESP_Can_Bridge_v01/tools/bench/buses.py` has `FastSlcanBus` (chunked reads, no per-frame `flush()`, which can sleep 50 ms on Windows).

## Testing with Kvaser Leaf Light v2

1. Connect both devices to the same CAN bus (twisted pair + common GND + termination).
2. Set Kvaser to 500 kbit/s.
3. Open the ESP SLCAN port with python-can or SavvyCAN at 500 kbit/s.
4. Send frames from Kvaser → verify in slcan client.
5. Send from slcan client → verify in Kvaser tool.

Throughput and loss: `python ESP_Can_Bridge_v01/tools/bench/slcan_throughput.py --slcan COMx` (prints the `?` counters too).

## Legacy Arduino sketch

[`arduino/esp32c6-slcan/esp32c6-slcan.ino`](arduino/esp32c6-slcan/esp32c6-slcan.ino), confirmed working 2026-07-14 with `can.viewer` and a Kvaser Leaf Light v2. Not recommended: it loses frames above ~1,000 frames/s TX and ~2,000 frames/s RX (5-deep TWAI queues, TX dropped after one tick but still acked, single loop for both directions). Arduino IDE + ESP32 core 3.x, board "ESP32-C6 Dev Module", enable "USB CDC On Boot".

## Credits

The Arduino sketch is adapted from https://github.com/mintynet/esp32-slcan (Ian Tabor / mintynet), thank you for the solid SLCAN + TWAI implementation. The ESP-IDF firmware is a rewrite on the modern TWAI node driver with the same command set.
