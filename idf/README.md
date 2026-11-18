# esp32c6-slcan, ESP-IDF firmware

Lawicel SLCAN adapter on ESP32-C6 + MCP2562 (TWAI TX = GPIO1, RX = GPIO2) over the C6's USB-Serial/JTAG port. ESP-IDF **6.1.1**, the on-chip TWAI node driver (same stack as `ESP_Can_Bridge_v01`). Replaces the Arduino sketch in the repo root, which stays for reference.

## Build and flash

```powershell
. C:\Espressif\tools\Microsoft.v6.1.1.PowerShell_profile.ps1
cd <esp32c6-slcan>\idf
idf.py set-target esp32c6     # first time only
idf.py build
python -m esptool --port COMx chip-id   # must say ESP32-C6, never flash the bridge's C5
idf.py -p COMx flash -a
```

Logs go to UART0 (GPIO16), never to USB: the USB port carries only SLCAN.

## Protocol

| Command | Reply | Notes |
|---------|-------|-------|
| `S0`..`S8` | CR / BEL | 10k..1M, only while closed. Sample point 87.5 % (J1939-14), 80 % above 500k |
| `O` / `L` / `C` | CR / BEL | open normal / listen-only / close. `O` while open in the same mode = CR |
| `tiiildd..` `Tiiiiiiiildd..` `riiil` `Riiiiiiiil` | `z` / `Z` + CR, or BEL | strict: hex only, exact length for the DLC. BEL = closed, listen-only, malformed, TX queue full |
| `Z0` / `Z1` | CR | Lawicel timestamps (ms mod 60000, from the TWAI RX timestamp) |
| `F` | `Fxx` CR | Lawicel flags, cleared on read: 01 RX full, 02 TX full, 04 error warning, 08 overrun, 20 error passive, 80 bus error / bus-off |
| `V` / `N` | `V0200` / `NC6SL` | |
| `M` / `m` | CR | ignored: always accept all |
| `?` / `?C` | `?rx=..,rx_overrun=..,usb_drop=..,tx_ok=..,tx_fail=..,tx_nack=..,cmd_err=..,bus_err=..,bus_off=..,state=..,open=..,bitrate=..` | counters (`?C` = read and clear). Not Lawicel |

python-can's `slcan` interface works unchanged; it ignores `z`/`Z`/BEL replies.

## Design

- CAN RX: ISR copies each frame into a 1024-frame ring; a writer task drains the whole ring per wake-up into one USB write (16 KB USB TX buffer). Ring full = `rx_overrun`; host not reading = `usb_drop`.
- Host TX: a reader task parses lines and queues frames into a 1024-slot pool feeding the TWAI TX queue (FIFO, ~280 ms of bus time). It never blocks: full = BEL + `tx_nack`. Reason: the IDF USB-Serial/JTAG driver drops whole USB packets when its RX ring is full, without counting them, and on Windows each pyserial `write()` is one packet, so a blocking reader loses whole commands silently.
- Retransmit until acked (standard CAN). Bus-off: the node is closed and reopened after 100 ms (`bus_off` counter).

## Measured (2026-11-18, direct Kvaser Leaf <-> C6, 120 ohm, 500 kbit/s, 29-bit DLC 8)

Tool: `ESP_Can_Bridge_v01/tools/bench/slcan_throughput.py`. Bus full is about 3,480 frames/s.

| Firmware + host reader | Kvaser -> C6 (RX), lossless up to | at full bus | C6 -> Kvaser (TX), lossless up to | above that |
|------------------------|-----------------------------------|-------------|-----------------------------------|-----------|
| Arduino + stock python-can | ~2,000/s (0.1 % at 1-2k) | 2,570/s, 25 % lost | 1,000/s | 45 % lost at 2,000/s, out-of-order and corrupt IDs |
| Arduino + `FastSlcanBus` | same | same | same | same |
| **IDF + `FastSlcanBus`** | **3,483/s (full bus)** | **0 lost** | **3,400/s (97 % bus)** | flood at 11,500/s: excess refused with BEL (`tx_nack`), ~10 % still not accounted for |

Open: under a host flood far above bus capacity, ~10 % of the excess commands are not in any counter (most likely still dropped in the USB driver RX ring). Keep host TX at or below bus capacity.
