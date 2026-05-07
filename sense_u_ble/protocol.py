"""Sense-U Baby Pro BLE 协议解析（pure functions）+ 告警检测状态机。

不做任何 IO；调用方负责 BLE 连接 / 推送。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from sense_u_ble.config import Config
from sense_u_ble import state
from sense_u_ble.i18n import t

log = logging.getLogger("sense_u_ble.protocol")

# ── 帧助手 ────────────────────────────────────────────────────────────

def _u8(b: int) -> int:
    return b & 0xFF


def _ts_be() -> bytes:
    s = int(time.time())
    return bytes([(s >> 24) & 0xFF, (s >> 16) & 0xFF, (s >> 8) & 0xFF, s & 0xFF])


def char_uuid(prefix: str, address: str, mac_override: str = "") -> str:
    """Sense-U 的 GATT 特征 UUID 末尾 12 hex = 设备 MAC。

    macOS 上 ble_address 是 CoreBluetooth UUID（无法反推 MAC），需通过 mac_override
    单独提供。Windows / Linux 上 address 本身就是 MAC，mac_override 可留空。
    """
    mac = mac_override or address
    addr = mac.replace(":", "").replace("-", "").lower()[-12:]
    return f"{prefix}-{addr}"


def char_register(cfg: Config) -> str:
    """0x70 鉴权特征。"""
    return char_uuid("01021921-9e06-a079-2e3f", cfg.ble_address, cfg.ble_mac)


def char_realtime(cfg: Config) -> str:
    """CHAR_2 — 设备主动推送实时数据（姿势/呼吸/温湿度/告警）。"""
    return char_uuid("01021922-9e06-a079-2e3f", cfg.ble_address, cfg.ble_mac)


def char_settings(cfg: Config) -> str:
    """CHAR_4 — 指令/响应通道（0xBA 轮询、初始化链、告警 ACK）。"""
    return char_uuid("01021925-9e06-a079-2e3f", cfg.ble_address, cfg.ble_mac)


def pkt_reconnect(code: bytes) -> bytes:
    """0x70 ReconnectionType: [0x70, baby_code(6), ts_be(4), 0×7 zeros] 共 18 字节。"""
    b = bytearray(18)
    b[0] = 0x70
    b[1:7] = code[:6]
    b[7:11] = _ts_be()
    return bytes(b)


def pkt_get_baby_data() -> bytes:
    """0xBA get_baby_data 请求（20 字节）→ 写 CHAR_4，响应从 CHAR_4 回。"""
    b = bytearray(20)
    b[0] = 0xBA
    return bytes(b)


def pkt_get_batch() -> bytes:
    """0xC0 01 GET_BATCH — 鉴权成功后初始化链第一步。"""
    b = bytearray(20)
    b[0] = 0xC0
    b[1] = 0x01
    return bytes(b)


def pkt_leaning() -> bytes:
    """0xF5 F2 LeaningType — 启用所有告警开关。"""
    b = bytearray(20)
    b[0] = 0xF5; b[1] = 0xF2; b[2] = 0x32; b[3] = 0x03
    return bytes(b)


def pkt_temp_alarm() -> bytes:
    """0xB2 体温告警：高温 36.0°C / 低温 20.0°C。"""
    b = bytearray(20)
    b[0] = 0xB2
    b[2] = 0x68; b[3] = 0x01  # 360 = 36.0°C×10, LE
    b[4] = 0xC8; b[5] = 0x00  # 200 = 20.0°C×10, LE
    return bytes(b)


def pkt_kicking_alarm() -> bytes:
    """0xB3 踢腿告警。"""
    b = bytearray(20)
    b[0] = 0xB3; b[2] = 0x0F; b[3] = 0x03
    return bytes(b)


def pkt_breath_alarm() -> bytes:
    """0xB0 呼吸告警：下限 1 / 上限 25 次/分钟。"""
    b = bytearray(20)
    b[0] = 0xB0; b[1] = 0x01; b[2] = 0x19
    return bytes(b)


def pkt_alert_ack(mode: int, delay_s: int = 300) -> bytes:
    """0xF6 BabyAlertAck — 告知设备 APP 已处理报警，停止闪灯。

    mode:    CHAR_2 告警包 data[5] 的报警模式值
    delay_s: 多少秒后设备可再次发同类告警（65535 = 永不重发）
    写入 CHAR_4。
    """
    b = bytearray(18)
    b[0] = 0xF6
    b[1] = 2  # recordType，APP 固定为 2
    _MASK = {1: 0x01, 2: 0x02, 3: 0x04, 4: 0x08, 5: 0x10, 6: 0x20, 7: 0x40}
    if mode in (8, 9, 10, 48, 51, 65):
        b[2] = 0x80
    elif mode == 11:
        b[3] = 0x01
    elif mode in _MASK:
        b[2] = _MASK[mode]
    else:
        b[2] = 0xFF; b[3] = 0xFF  # 未知 mode → 通用 ACK
    b[6] = delay_s & 0xFF
    b[7] = (delay_s >> 8) & 0xFF
    return bytes(b)


def load_baby_code(code_file: Path) -> Optional[bytes]:
    if not code_file.exists():
        return None
    try:
        with code_file.open(encoding="utf-8") as f:
            h = json.load(f).get("baby_code", "")
        return bytes.fromhex(h) if len(h) == 12 else None
    except Exception:
        return None


# ── 解析 + 告警状态机 ────────────────────────────────────────────────

_POSTURES = {0: "仰卧", 1: "俯卧", 2: "左侧卧", 3: "右侧卧", 4: "坐姿"}

_ALERT_MODES = {
    2: "俯卧告警", 3: "温度过高", 4: "温度过低",
    7: "降温提醒", 8: "呼吸过快", 9: "呼吸微弱",
    10: "俯卧+呼吸微弱", 11: "活动提醒", 65: "趴睡呼吸微弱",
}

# 模块级状态：用于告警去抖。每实例化新进程即重置；同一进程跨多次 parse 调用持续追踪。
_last_prone_alert:  float = 0
_prone_since:       float = 0
_last_breath_alert: float = 0
_low_breath_since:  float = 0


async def parse_realtime_data(
    data: bytes,
    send_ack,
) -> None:
    """解析 CHAR_2 实时推送包 → 更新 state.sensor_state。

    告警包 (rt=8, st=2, notify≠0) 时立即调用 send_ack(mode) 写 0xF6 到 CHAR_4，
    让设备停止闪灯。持续时间型告警（俯卧 / 呼吸过慢）仍由 parse_baby_data 的
    0xBA 轮询负责，此处只更新状态字段。

    send_ack: async callable(mode: int) — 由 client.py 提供的 CHAR_4 写入闭包。
    """
    if len(data) < 2:
        return

    rt = (data[0] >> 3) & 0x1F
    st = (((data[0] & 0xFF) << 8) | (data[1] & 0xFF)) >> 6 & 0x1F

    if rt == 6:  # STATUS_RUNNING_RECORD
        if st == 1 and len(data) >= 7:  # 电量
            bat = _u8(data[6])
            if 0 <= bat <= 100:
                state.sensor_state["battery"] = bat
                log.debug(f"CHAR_2 电量: {bat}%")

    elif rt == 8:  # SPECIAL_RECORD
        if st == 1 and len(data) >= 8:  # 温度包
            raw = (_u8(data[6]) << 8) | _u8(data[5])
            if raw >= 32768:
                raw = 32768 - raw
            temp = raw / 10.0
            if 10.0 < temp < 50.0:
                state.sensor_state["temperature"] = round(temp, 1)
                log.debug(f"CHAR_2 温度: {temp:.1f}°C")

        elif st == 4 and len(data) >= 6:  # 姿势包
            pid = _u8(data[5])
            if pid in _POSTURES:
                state.sensor_state["posture"] = _POSTURES[pid]
                log.debug(f"CHAR_2 姿势: {_POSTURES[pid]}")

        elif st == 5 and len(data) >= 7:  # 呼吸包
            rate = _u8(data[5])
            if rate < 200:
                state.sensor_state["breath_rate"] = rate
                log.debug(f"CHAR_2 呼吸: {rate} 次/min")

        elif st == 2 and len(data) >= 7:  # 设备告警包
            mode   = _u8(data[5])
            notify = _u8(data[6])
            label  = _ALERT_MODES.get(mode, f"mode={mode}")
            if notify != 0:
                log.warning(f"设备告警: {label}  → 发送 0xF6 ACK 停止闪灯")
                await send_ack(mode)
            else:
                log.debug(f"设备告警已解除: {label}")

    state.sensor_state["last_update"] = datetime.now().strftime("%H:%M:%S")
    await state.broadcast({"type": "sensor", **state.sensor_state})


async def parse_baby_data(cfg: Config, data: bytes,
                          on_alert) -> None:
    """解析 0xBA get_baby_data 响应包 → 更新 state.sensor_state → broadcast。

    on_alert: async callable(message: str, level: str) — 由调用方传入告警发射器。

    布局: [0]=0xBA [2]=姿势 [3-4]=衣内温度*10 LE [6]=呼吸 [9]=电量 [10]=佩戴
    """
    global _last_prone_alert, _prone_since, _last_breath_alert, _low_breath_since

    if len(data) < 11:
        return

    posture_id = _u8(data[2])
    if posture_id in _POSTURES:
        state.sensor_state["posture"] = _POSTURES[posture_id]
        log.debug(f"姿势: {_POSTURES[posture_id]}")
    else:
        log.warning(f"姿势 ID 未知: {posture_id} (0x{posture_id:02x})  data={data.hex(' ')}")

    # 俯卧告警：持续 ≥ threshold 才首次报警，之后每 cooldown 秒重复一次（如仍俯卧）
    now = time.time()
    if posture_id == 1:
        if _prone_since == 0:
            _prone_since = now
        elapsed = now - _prone_since
        if elapsed >= cfg.prone_alert_threshold_s and (now - _last_prone_alert) > cfg.prone_alert_cooldown_s:
            _last_prone_alert = now
            await on_alert(t("alert_prone", cfg.language, seconds=int(elapsed)), "danger")
    else:
        _prone_since = 0

    # 衣内温度：data[3..4] 16-bit LE，单位 0.1°C
    temp = (_u8(data[4]) << 8 | _u8(data[3])) / 10.0
    if 10.0 < temp < 50.0:
        state.sensor_state["temperature"] = round(temp, 1)
        log.debug(f"衣内温度: {temp:.1f}°C")
    elif temp != 0.0:
        log.warning(f"温度超范围: [3-4]={data[3]:02x} {data[4]:02x} → {temp:.1f}°C")

    rate = _u8(data[6])
    if rate < 200:
        state.sensor_state["breath_rate"] = rate
        log.debug(f"呼吸频率: {rate} 次/min")

        # 呼吸过低/停止告警
        if rate < cfg.breath_alert_threshold_rate:
            if _low_breath_since == 0:
                _low_breath_since = now
            elapsed_b = now - _low_breath_since
            if elapsed_b >= cfg.breath_alert_duration_s and (now - _last_breath_alert) > cfg.breath_alert_cooldown_s:
                _last_breath_alert = now
                await on_alert(
                    t("alert_breath", cfg.language, rate=rate, seconds=int(elapsed_b)),
                    "danger",
                )
        else:
            _low_breath_since = 0

    battery = _u8(data[9])
    if battery <= 100:
        state.sensor_state["battery"] = battery
        log.debug(f"电量: {battery}%")

    if not state.sensor_state.get("ble_ok"):
        state.sensor_state["ble_ok"] = True
        log.info("连接已活跃")

    state.sensor_state["last_update"] = datetime.now().strftime("%H:%M:%S")
    await state.broadcast({"type": "sensor", **state.sensor_state})
