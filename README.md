# sense-u-ble

A minimal driver for the **Sense-U Baby Pro** sleep monitor (BLE wearable).
Speaks the device's reverse-engineered GATT protocol, runs a continuous
connect / poll / reconnect loop, and **streams sensor and alert events to a
configurable HTTP consumer** so any app can subscribe.

Originally extracted from [baby-sentinel](https://github.com/mao-mao-yu/baby-sentinel).
Designed to run on a Raspberry Pi (so the Pi's built-in Bluetooth handles BLE
and your main host's BT stays free), but it works anywhere `bleak` runs:
Linux, macOS, Windows.

```
┌──────────────────┐    BLE      ┌──────────────────┐    HTTP POST    ┌──────────────┐
│ Sense-U Baby Pro │ ──────────→ │  sense-u-ble     │ ───────────────→│  your app    │
│   (the wearable) │   GATT      │  service :8082   │  /ingest        │  (consumer)  │
└──────────────────┘             └──────────────────┘                 └──────────────┘
```

The wearable measures **breath rate, posture, in-clothing temperature,
battery**. sense-u-ble decodes those plus generates two derived alerts:

- **Prone alert** — baby has been belly-down for ≥ N seconds
- **Breathing alert** — breath rate < N for ≥ M seconds

Device-initiated alerts (face-down, weak breath, temperature out of range, …)
are also acknowledged back to the wearable so its LED stops flashing.

---

## Features

- **Pure Python** (one binary dep: `bleak`) — no native build needed
- **Self-contained config** — single `config.json`, no env vars, no DB
- **HTTP-out only** — the service POSTs JSON to your `consumer_url`; you don't
  need a websocket client or special SDK
- **Auto-reconnect** — survives BLE drops, baby moving out of range, etc.
- **Cross-platform** — Linux (Pi recommended), macOS, Windows
- **i18n** — alert messages in Chinese / Japanese / English (config-selected)
- **Pairing tool included** — first-time pairing handshake reverse-engineered
  from APK analysis of the official Sense-U app
- **Alert ACK** — replies `0xF6` to device alert notifications so the wearable's
  LED stops flashing immediately

---

## Install

```bash
git clone https://github.com/mao-mao-yu/sense-u-ble.git
cd sense-u-ble
python -m venv venv && source venv/bin/activate
pip install -e .                    # editable install with deps
cp config.example.json config.json  # then edit
```

`bleak` on Linux/Pi needs **BlueZ ≥ 5.50**. On Raspberry Pi OS that's
already there. macOS uses CoreBluetooth (no extra setup, but needs Bluetooth
permission for the terminal/IDE the first time).

---

## Configure

Edit `config.json`. Minimal required fields:

```jsonc
{
  "ble_address":  "AA:BB:CC:DD:EE:FF",  // see "Find the device address" below
  "ble_mac":      "",                    // macOS only — see below
  "consumer_url": "http://192.168.1.10:8080/ingest"
}
```

| Field | Default | What it does |
|---|---|---|
| `ble_address` | — | The wearable's address. **Linux/Windows** = real MAC (`D4:92:DB:03:D7:59`). **macOS** = CoreBluetooth UUID (`0B5602EE-…`, different per Mac, found via `tools/scan.py`). |
| `ble_mac` | `""` | **macOS only** — the device's real MAC, used to build the GATT characteristic UUIDs. Leave empty on Linux/Windows. |
| `ble_dump_raw` | `false` | Hex-dump every BLE notification for protocol debugging. |
| `ble_scan_timeout_s` | 20 | Max scan window before giving up and retrying. |
| `ble_connect_timeout_s` | 15 | GATT connect timeout. |
| `ble_reconnect_delay_s` | 10 | Wait before next attempt after disconnect. |
| `ble_poll_interval_s` | 2 | How often to poke 0xBA for fresh data. |
| `prone_alert_threshold_s` | 30 | Prone for this long → first alert fires. |
| `prone_alert_cooldown_s` | 300 | While still prone, repeat every this many seconds. |
| `breath_alert_threshold_rate` | 8 | Breath/min below this is "low". |
| `breath_alert_duration_s` | 20 | Low breath for this long → first alert fires. |
| `breath_alert_cooldown_s` | 300 | While still low, repeat every this many seconds. |
| `consumer_url` | — | URL to POST sensor/alert events to. Leave empty to skip pushing (events are still queryable via `GET /api/sensor`). |
| `consumer_api_key` | `""` | If non-empty, sent as `X-API-Key: …` header to consumer. |
| `code_file` | `./baby_code.json` | Where to load the pairing token. |
| `language` | `zh` | Alert message language: `zh` / `ja` / `en`. |
| `host` | `0.0.0.0` | HTTP bind address. |
| `port` | `8082` | HTTP listen port. |
| `log_level` | `INFO` | `DEBUG` / `INFO` / `WARNING`. |

---

## Find the device address

```bash
python tools/scan.py
# Prints all nearby BLE devices.
# Look for "Sense-U Baby Pro" — that line's address goes into ble_address.
```

On macOS the `address` column is a CoreBluetooth UUID. Also fill `ble_mac`
with the real MAC (printed on the device or visible briefly in scan output).

---

## First-time pairing

Before the service can connect you need a `baby_code.json` from a one-shot
pairing handshake:

```bash
python tools/pairing.py
# Follow the prompt — long-press the device button twice (slow blue blink).
```

A successful pairing writes `baby_code.json` next to `config.json`. The
code is **portable across machines**: copy the file to your Pi after pairing
on your laptop.

---

## Run

```bash
sense-u-ble                     # uses config.json next to where you ran it
# or
python -m sense_u_ble.service
```

**Endpoints:**

| Method | Path | Returns |
|---|---|---|
| `GET`  | `/health` | `{ok, ble_ok}` for liveness checks |
| `GET`  | `/api/sensor` | Latest sensor snapshot (no consumer needed for read-only use) |
| `POST` | `/api/sensor/refresh` | Force device to push a fresh frame now |

---

## Consumer protocol

Whenever a sensor frame is parsed or an alert fires, sense-u-ble POSTs JSON
to `consumer_url`. Two event shapes:

```jsonc
// type=sensor — on every CHAR_2 push or 0xBA poll response
{
  "type":        "sensor",
  "breath_rate": 32,
  "temperature": 36.5,
  "posture":     "仰卧",         // 仰卧 / 俯卧 / 左侧卧 / 右侧卧 / 坐姿
  "battery":     78,
  "ble_ok":      true,
  "last_update": "13:42:55"
}

// type=alert — prone or low-breath threshold crossed
{
  "type":      "alert",
  "level":     "danger",          // danger / warning / info
  "message":   "🚨 俯卧警告\n持续 35 秒处于俯卧状态，请立即确认。",
  "timestamp": "13:42:30"
}
```

Minimal Python receiver:

```bash
python examples/consumer.py    # listens on :9000/ingest
```

In your config.json:

```json
"consumer_url": "http://localhost:9000/ingest"
```

If the consumer is offline, sense-u-ble logs a debug line and keeps running.
Sensor/alert events are **fire-and-forget** — no retry queue. If you need
delivery guarantees, run the consumer behind a queue (Redis Streams,
RabbitMQ, etc.).

---

## Run on a Raspberry Pi (recommended)

The Pi has a built-in BT chip and is happy to babysit BLE 24/7. Below uses
**user-level systemd** so you don't need sudo (works on any modern distro):

```bash
# On the Pi
git clone https://github.com/mao-mao-yu/sense-u-ble.git ~/sense-u-ble
cd ~/sense-u-ble
python -m venv venv && source venv/bin/activate
pip install -e .

# After pairing on a machine that's easier to interact with, copy baby_code.json over:
scp baby_code.json pi@PI:~/sense-u-ble/

# Edit ~/sense-u-ble/config.json — set ble_address, ble_mac (if mac), consumer_url

mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/sense-u-ble.service <<'UNIT'
[Unit]
Description=sense-u-ble — Sense-U Baby Pro BLE driver
After=network-online.target bluetooth.target

[Service]
Type=simple
ExecStart=%h/sense-u-ble/venv/bin/sense-u-ble
WorkingDirectory=%h/sense-u-ble
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now sense-u-ble.service

# Make it survive reboot without an active SSH session.
# Most distros' polkit lets the user enable their own linger without sudo:
loginctl enable-linger $USER
```

Logs: `journalctl --user -u sense-u-ble -f`.

---

## Project layout

```
sense-u-ble/
├── config.example.json         # Config template; copy to config.json
├── pyproject.toml
├── sense_u_ble/                # The Python package
│   ├── config.py               # Config dataclass + loader
│   ├── i18n.py                 # zh/ja/en alert messages
│   ├── state.py                # Sensor state + broadcast hook
│   ├── protocol.py             # Pure parsing + alert state machine
│   ├── client.py               # BLE connect/poll/reconnect loop
│   └── service.py              # FastAPI HTTP + consumer push
├── tools/
│   ├── scan.py                 # List nearby BLE devices
│   ├── pairing.py              # First-time pairing handshake
│   ├── discover.py             # GATT service dump (debug)
│   └── adv_scan.py             # Capture broadcast frames (debug)
└── examples/
    └── consumer.py             # Minimal HTTP receiver to plug into consumer_url
```

---

## Protocol notes

The protocol was reverse-engineered from BLE traffic captures and confirmed
against decompiled sources of the official Sense-U Android APK.

**Characteristics** (last 12 hex chars = device MAC):

| Name | UUID prefix | Role |
|---|---|---|
| CHAR_1 | `01021921-9e06-a079-2e3f` | Auth (`0x69/0x68/0x70`) |
| CHAR_2 | `01021922-9e06-a079-2e3f` | Device → host real-time push |
| CHAR_4 | `01021925-9e06-a079-2e3f` | Host → device commands & responses |

**Connection flow:**

1. **Pairing** (once): `0x69` UID → `0x68` RegisterType → device replies with
   6-byte `baby_code` → saved to `baby_code.json`.
2. **Reconnect**: subscribe CHAR_1 + CHAR_2 + CHAR_4, write `0x70 + baby_code + ts`
   to CHAR_1.
3. **Init chain** (on 0x70 ACK): `0xC0` → `0xF5` → `0xB2` → `0xB3` → `0xB0`
   written to CHAR_4 in sequence, each triggered by the device's reply.
4. **Data**: device pushes posture / breath / temperature / battery / alert
   packets to **CHAR_2** in real time. `0xBA GetBabyData` on CHAR_4 gives a
   full snapshot on demand (used for the polling loop and duration-based alerts).
5. **Alert ACK**: on receiving a device-initiated alert packet from CHAR_2,
   sense-u-ble sends `0xF6 BabyAlertAck` to CHAR_4 so the LED stops flashing.

See [`sense_u_ble/protocol.py`](sense_u_ble/protocol.py) for byte-level packet
layouts. See [`tools/pairing.py`](tools/pairing.py) for the full pairing
handshake with verbose logging.

---

## Caveats

- **Reverse-engineered protocol.** Future device firmware updates from
  Sense-U might break things. Last verified against APK decompiled sources
  and Baby Pro firmware as of 2026-Q1.
- **Alert thresholds are fixed.** The init chain sets device-side thresholds
  to hardcoded defaults (high-temp 36°C / low-temp 20°C / breath 1–25 bpm).
  Changing them requires editing the `pkt_*_alarm()` functions in `protocol.py`.
- **Single device per service instance.** If you have multiple wearables,
  run multiple processes with different `port` and `ble_address`.

---

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

- Protocol connection flow originally traced in
  [esphome-sense-u](https://github.com/esphome) community projects; byte-level
  details confirmed and extended via APK decompilation of the official Sense-U
  Android app.
- [bleak](https://github.com/hbldh/bleak) for cross-platform BLE.
