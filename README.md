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
battery, activity level, wearing state, and charge status**. Device-initiated
alerts (prone, weak breath, temperature out of range, …) are forwarded to the
consumer as `type=alert` events; the consumer's HTTP response controls whether
the driver sends a `0xF6` ACK to stop the wearable's LED.

---

## Features

- **Pure Python** (one binary dep: `bleak`) — no native build needed
- **Self-contained config** — single `config.json`, no env vars, no DB
- **HTTP-out only** — the service POSTs JSON to your `consumer_url`; you don't
  need a websocket client or special SDK
- **Auto-reconnect** — survives BLE drops, baby moving out of range, etc.
- **Cross-platform** — Linux (Pi recommended), macOS, Windows
- **Pairing tool included** — first-time pairing handshake reverse-engineered
  from APK analysis of the official Sense-U app
- **Consumer-controlled Alert ACK** — device alerts are forwarded to the
  consumer; consumer replies `{"ack": true}` to send `0xF6` and stop the LED

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
| `consumer_url` | — | URL to POST sensor/alert events to. Leave empty to skip pushing (events are still queryable via `GET /api/sensor`). |
| `consumer_api_key` | `""` | If non-empty, sent as `X-API-Key: …` header to consumer. |
| `code_file` | `./baby_code.json` | Where to load the pairing token. |
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

sense-u-ble POSTs JSON to `consumer_url` on every sensor frame and on every
device-initiated alert. Two event shapes:

```jsonc
// type=sensor — on every CHAR_2 push or 0xBA poll response
{
  "type":        "sensor",
  "breath_rate": 32,          // breaths/min, int, null if unknown
  "temperature": 36.5,        // in-clothing °C, float, null if unknown
  "posture":     "supine",    // supine/prone/left_side/right_side/sitting, null if unknown
  "battery":     78,          // 0–100 %, int, null if unknown
  "activity":    12,          // activity level 0–255, int, null if unknown
  "wearing":     true,        // sensor worn by baby, bool, null if unknown
  "charge":      0,           // 0=not charging, 1=charging, 2=full, null if unknown
  "ble_ok":      true,        // BLE connection alive
  "last_update": "13:42:55"   // HH:MM:SS of last successful parse
}

// type=alert — device-initiated alert from CHAR_2 (prone, weak breath, temperature, etc.)
{
  "type":      "alert",
  "level":     "danger",
  "mode":      2,             // raw alertMode from device (see alert mode table)
  "message":   "prone alert", // human-readable label (see alert mode table)
  "timestamp": "13:42:30"
}
```

### Alert ACK — controlling the device LED

When the driver receives a device alert, it POSTs `type=alert` to the consumer
**in a background task** (does not block BLE callbacks) and waits for the HTTP
response. The consumer's response body controls whether the driver sends a
`0xF6` ACK to stop the device's flashing LED:

```jsonc
// Consumer response body — tell the driver to stop the LED:
{ "ack": true }

// Consumer response body — keep LED flashing (user hasn't confirmed yet):
{ "ack": false }
// or an empty / non-JSON body → treated as false
```

The HTTP call has a **2-second timeout** (1-second connect). If the consumer is
unreachable or slow, `ack` defaults to `false` — the LED keeps flashing.

If `consumer_url` is empty, the driver always ACKs immediately (LED stops).

**On device restart**: if the user power-cycles the wearable instead of
confirming via the consumer, the BLE connection drops. The driver cancels all
in-flight alert tasks at that point — no stale ACKs are sent to the new
connection.

Minimal Python receiver:

```bash
python examples/consumer.py    # listens on :9000/ingest
```

In your config.json:

```json
"consumer_url": "http://localhost:9000/ingest"
```

Sensor events are **fire-and-forget** — no retry queue. Alert pushes block
until the consumer responds (up to 2 s); use the `ack` field to signal
confirmation.

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
│   ├── state.py                # Sensor state dict + broadcast hook injection
│   ├── protocol.py             # Pure parsing functions + alert state machine
│   ├── client.py               # BLE connect/poll/reconnect loop (bleak)
│   └── service.py              # FastAPI HTTP layer + consumer push
├── tools/
│   ├── scan.py                 # List nearby BLE devices
│   ├── pairing.py              # First-time pairing handshake
│   ├── discover.py             # GATT service dump (debug)
│   └── adv_scan.py             # Capture broadcast frames (debug)
└── examples/
    └── consumer.py             # Minimal HTTP receiver to plug into consumer_url
```

**Module responsibilities:**

| Module | Role |
|---|---|
| `config.py` | Loads `config.json`, exposes typed `Config` dataclass |
| `state.py` | Singleton `sensor_state` dict; `set_broadcast()` / `broadcast()` hook |
| `protocol.py` | Pure functions — UUID builders, packet builders, `parse_realtime_data`, `parse_baby_data`; alert debounce state machine |
| `client.py` | `run_loop()` — BLE scan → connect → subscribe → auth → init chain → poll loop → reconnect |
| `service.py` | FastAPI app; wires `on_alert` and `broadcast` hook; starts `run_loop` as background task |

---

## Protocol reference

> Reverse-engineered from BLE traffic captures and confirmed against
> decompiled sources of the official Sense-U Android APK (Baby Pro,
> verified 2026-Q1). All integers are unsigned unless noted.

### GATT characteristics

All UUID suffixes are the last 12 hex digits of the device's **real MAC**
(colons removed, lowercase). On macOS, `ble_address` is a CoreBluetooth UUID
and cannot be used to derive the suffix — supply the real MAC in `ble_mac`.

| Name   | UUID prefix              | Direction          | Role |
|--------|--------------------------|--------------------|------|
| CHAR_1 | `01021921-9e06-a079-2e3f` | write + notify    | Auth (0x69 / 0x68 / 0x70) |
| CHAR_2 | `01021922-9e06-a079-2e3f` | notify (device→host) | Real-time push: posture, breath, temperature, battery, alerts |
| CHAR_4 | `01021925-9e06-a079-2e3f` | write + notify    | Commands host→device; response notify device→host |

CHAR_3 (`01021923-…`) exists in the GATT table but is not used by this driver.

---

### Connection flow (full session)

```
HOST                                    DEVICE
 │  scan for BLE address                   │
 │─────────────────────────────────────────▶│  (BLE advertisement)
 │  connect GATT                           │
 │  subscribe CHAR_1 notify                │
 │  subscribe CHAR_2 notify                │
 │  subscribe CHAR_4 notify                │
 │  write CHAR_1: 0x70 pkt_reconnect       │  ← auth token (baby_code + timestamp)
 │                                         │
 │◀────────── CHAR_1 notify: 70 00 …  ─────│  auth success (d[1]==0x00)
 │  write CHAR_4: 0xC0 01 (GET_BATCH)      │  ← init chain step 1
 │◀────────── CHAR_4 notify: C0 …     ─────│
 │  write CHAR_4: 0xF5 F2 32 03 (LEANING)  │  ← step 2: enable all alert switches
 │◀────────── CHAR_4 notify: F5 …     ─────│
 │  write CHAR_4: 0xB2 … (TEMP_ALARM)      │  ← step 3: set temperature thresholds
 │◀────────── CHAR_4 notify: B2 …     ─────│
 │  write CHAR_4: 0xB3 … (KICKING_ALARM)   │  ← step 4
 │◀────────── CHAR_4 notify: B3 …     ─────│
 │  write CHAR_4: 0xB0 01 19 (BREATH_ALARM)│  ← step 5: breath 1–25 bpm
 │◀────────── CHAR_4 notify: B0 …     ─────│  init chain complete
 │                                         │
 │  write CHAR_4: 0xBA (get snapshot)      │  ← immediate first poll
 │◀────────── CHAR_4 notify: BA …     ─────│  full snapshot (0xBA response)
 │                                         │
 │◀────────── CHAR_2 notify: …        ─────│  real-time push (continuous)
 │  (every ble_poll_interval_s seconds)    │
 │  write CHAR_4: 0xBA                     │  ← periodic poll
 │◀────────── CHAR_4 notify: BA …     ─────│
 │  (on device alert)                      │
 │◀────────── CHAR_2 notify: alert    ─────│
 │  write CHAR_4: 0xF6 (alert ACK)         │  ← stops LED flash immediately
```

The init chain **must complete** before the device sends CHAR_2 alert
notifications. Each step is triggered by the previous ACK — do not send
the next packet until the previous header byte echoes back on CHAR_4.

---

### Packet reference — host → device

All host→device packets are 20 bytes unless noted. Unspecified bytes are 0x00.

#### 0x70 — ReconnectType (auth) → CHAR_1

| Offset | Len | Value |
|--------|-----|-------|
| 0 | 1 | `0x70` |
| 1–6 | 6 | `baby_code` (6-byte pairing token from `baby_code.json`) |
| 7–10 | 4 | Unix timestamp, big-endian |
| 11–17 | 7 | `0x00` padding |

Total: 18 bytes. Written with `response=False`.

#### 0xC0 01 — GET_BATCH → CHAR_4

| Offset | Len | Value |
|--------|-----|-------|
| 0 | 1 | `0xC0` |
| 1 | 1 | `0x01` |
| 2–19 | 18 | `0x00` |

Triggers the init chain; device replies with `0xC0` echo on CHAR_4.

#### 0xF5 F2 — LeaningType → CHAR_4

| Offset | Len | Value |
|--------|-----|-------|
| 0 | 1 | `0xF5` |
| 1 | 1 | `0xF2` |
| 2 | 1 | `0x32` |
| 3 | 1 | `0x03` |
| 4–19 | 16 | `0x00` |

Enables all alert switches on the device.

#### 0xB2 — TempAlarm → CHAR_4

| Offset | Len | Value |
|--------|-----|-------|
| 0 | 1 | `0xB2` |
| 2–3 | 2 | High threshold: `0x68 0x01` = 360 = **36.0 °C × 10**, little-endian |
| 4–5 | 2 | Low threshold: `0xC8 0x00` = 200 = **20.0 °C × 10**, little-endian |

#### 0xB3 — KickingAlarm → CHAR_4

| Offset | Len | Value |
|--------|-----|-------|
| 0 | 1 | `0xB3` |
| 2 | 1 | `0x0F` |
| 3 | 1 | `0x03` |

#### 0xB0 — BreathAlarm → CHAR_4

| Offset | Len | Value |
|--------|-----|-------|
| 0 | 1 | `0xB0` |
| 1 | 1 | `0x01` — lower bound (1 bpm) |
| 2 | 1 | `0x19` — upper bound (25 bpm) |

#### 0xBA — GetBabyData → CHAR_4

| Offset | Len | Value |
|--------|-----|-------|
| 0 | 1 | `0xBA` |
| 1–19 | 19 | `0x00` |

Written with `response=True`. Device replies immediately on CHAR_4 with the
full sensor snapshot (see 0xBA response below). Used for periodic polling and
duration-based alert logic.

#### 0xF6 — BabyAlertAck → CHAR_4

**18 bytes**, written with `response=False`. Must be sent within seconds of
receiving an alert packet from CHAR_2, otherwise the device continues flashing
its LED.

| Offset | Len | Meaning |
|--------|-----|---------|
| 0 | 1 | `0xF6` |
| 1 | 1 | `0x02` — recordType (always 2 from APP) |
| 2 | 1 | Alert bitmask low byte (see table below) |
| 3 | 1 | Alert bitmask high byte |
| 4–5 | 2 | `0x00` |
| 6–7 | 2 | `delaySecond` — seconds before device may re-send same alert, little-endian |
| 8–17 | 10 | `0x00` |

Alert mode → ACK bitmask mapping (from APK `BleProtocol.getBabyAlertAckData`):

| Alert mode (data[5]) | Label | byte[2] | byte[3] |
|--------|-------|---------|---------|
| 1 | — | `0x01` | `0x00` |
| 2 | prone alert | `0x02` | `0x00` |
| 3 | temperature high | `0x04` | `0x00` |
| 4 | temperature low | `0x08` | `0x00` |
| 5 | — | `0x10` | `0x00` |
| 6 | — | `0x20` | `0x00` |
| 7 | cooling reminder | `0x40` | `0x00` |
| 8 | breath fast | `0x80` | `0x00` |
| 9 | breath weak | `0x80` | `0x00` |
| 10 | prone + breath weak | `0x80` | `0x00` |
| 11 | activity alert | `0x00` | `0x01` |
| 48 | — | `0x80` | `0x00` |
| 51 | — | `0x80` | `0x00` |
| 65 | prone sleep breath weak | `0x80` | `0x00` |
| other | unknown | `0xFF` | `0xFF` |

`delaySecond` is set to 300 (5 min) by default in this driver. The official
APK uses 65535 (never re-alert) for Baby Pro.

---

### Packet reference — device → host

#### CHAR_2 real-time push — header decode

Every CHAR_2 notification starts with a 2-byte header from which two fields
are extracted:

```
recordType  = (data[0] >> 3) & 0x1F
statusType  = ((data[0] << 8 | data[1]) >> 6) & 0x1F
```

`data[1..4]` typically contains a 4-byte timestamp (device epoch).

**recordType = 6 (STATUS_RUNNING_RECORD)**

| statusType | Meaning | Key bytes |
|------------|---------|-----------|
| 1 | Battery level | `data[6]` = 0–100 % |
| 2 | Sensor removed (not wearing) | — |
| 3 | Sensor attached (wearing) | — |

**recordType = 8 (SPECIAL_RECORD)**

| statusType | Meaning | Key bytes |
|------------|---------|-----------|
| 1 | Temperature (CHAR_2 variant) | `data[5..6]` = raw 16-bit BE; if raw ≥ 32768: raw = 32768 − raw; value = raw / 10.0 °C |
| 2 | Device-initiated **alert** | `data[5]` = alertMode, `data[6]` = notify (0=cleared, non-zero=active) |
| 4 | Posture | `data[5]` = posture ID (see table below) |
| 5 | Breath rate | `data[5]` = rate bpm (valid if < 200) |
| 7 | Activity level | `data[5]` = 0–255 |

Posture IDs (value of `sensor_state["posture"]`):

| ID | Value |
|----|-------|
| 0 | `"supine"` |
| 1 | `"prone"` — triggers prone alert |
| 2 | `"left_side"` |
| 3 | `"right_side"` |
| 4 | `"sitting"` |

Alert modes — CHAR_2 rt=8, st=2 (value of alert `"message"` field):

| alertMode | message |
|-----------|---------|
| 2 | `"prone alert"` |
| 3 | `"temperature high"` |
| 4 | `"temperature low"` |
| 7 | `"cooling reminder"` |
| 8 | `"breath fast"` |
| 9 | `"breath weak"` |
| 10 | `"prone + breath weak"` |
| 11 | `"activity alert"` |
| 65 | `"prone sleep breath weak"` |

On receiving `notify != 0`, the driver calls `on_device_alert(mode)`, which
POSTs the alert to the consumer and conditionally sends `0xF6` based on the
consumer's `{"ack": true/false}` response.

#### CHAR_4 response — 0xBA snapshot

Response to `0xBA GetBabyData`, notified on CHAR_4 (header byte = `0xBA`).
Minimum 12 bytes.

| Offset | Len | Field | Notes |
|--------|-----|-------|-------|
| 0 | 1 | `0xBA` header | identifies this as a snapshot response |
| 1 | 1 | — | unused |
| 2 | 1 | posture ID | same ID table as CHAR_2 |
| 3–4 | 2 | temperature | little-endian uint16 / 10.0 = °C; valid if 10.0 < value < 50.0 |
| 5 | 1 | humidity | raw / 100.0 = fraction (0.0–1.0); capped at 1.0 |
| 6 | 1 | breath rate | bpm; valid if < 200 |
| 7 | 1 | activity level | 0–255 |
| 8 | 1 | RSSI | signed, device-reported signal strength |
| 9 | 1 | battery | 0–100 % |
| 10 | 1 | wearing | `0x81` = wearing, else not |
| 11 | 1 | charge | 0=not charging, 1=charging, 2=full |

CHAR_4 also echoes the header byte of every init-chain command (C0, F5, B2,
B3, B0) as the first byte of its response notification. `on_settings` in
`client.py` uses this to drive the init chain state machine.

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
- **Humidity field exists but is not exposed.** The 0xBA snapshot includes
  humidity at byte 5 (raw/100 = fraction), but `sensor_state` does not
  currently track it. Add `"humidity": None` to `state.py` and read
  `data[5] / 100.0` in `parse_baby_data` if you need it.

---

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

- Protocol connection flow originally traced in
  [esphome-sense-u](https://github.com/esphome) community projects; byte-level
  details confirmed and extended via APK decompilation of the official Sense-U
  Android app.
- [bleak](https://github.com/hbldh/bleak) for cross-platform BLE.
