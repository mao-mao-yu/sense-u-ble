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


def char_settings(cfg: Config) -> str:
    """0xBA 数据 polling 特征。"""
    return char_uuid("01021925-9e06-a079-2e3f", cfg.ble_address, cfg.ble_mac)


def pkt_reconnect(code: bytes) -> bytes:
    """0x70 ReconnectionType: [0x70, baby_code(6), ts_be(4), 0×7 zeros] 共 18 字节。"""
    b = bytearray(18)
    b[0] = 0x70
    b[1:7] = code[:6]
    b[7:11] = _ts_be()
    return bytes(b)


def pkt_get_baby_data() -> bytes:
    """0xBA get_baby_data 请求（20 字节，仅首字节有效）。"""
    b = bytearray(20)
    b[0] = 0xBA
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

# 模块级状态：用于告警去抖。每实例化新进程即重置；同一进程跨多次 parse 调用持续追踪。
_last_prone_alert:  float = 0
_prone_since:       float = 0
_last_breath_alert: float = 0
_low_breath_since:  float = 0


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
