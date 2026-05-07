"""共享状态 + 可注入的事件 broadcast hook。

state 是简单的 dict（保持与 baby-sentinel 现有协议兼容）。broadcast hook
默认 no-op；service.py 启动时会注入 HTTP push 实现。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

sensor_state: dict[str, Any] = {
    "breath_rate": None,
    "temperature": None,
    "posture":     None,
    "battery":     None,
    "ble_ok":      False,
    "last_update": None,
}

# BLE 失联时清空的字段
_BLE_DATA_FIELDS = ("breath_rate", "temperature", "posture", "battery", "last_update")


def clear_ble_data() -> None:
    for k in _BLE_DATA_FIELDS:
        sensor_state[k] = None


_broadcast_hook: Callable[[dict], Awaitable[None]] | None = None


def set_broadcast(fn: Callable[[dict], Awaitable[None]] | None) -> None:
    global _broadcast_hook
    _broadcast_hook = fn


async def broadcast(data: dict) -> None:
    if _broadcast_hook is not None:
        await _broadcast_hook(data)
