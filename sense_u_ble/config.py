"""配置加载 — 从 config.json 读取，缺失字段从 config.example.json 兜底。

设计目标：完全不依赖任何外部项目（如 baby-sentinel），独立可运行。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 包目录 = repo 根目录 (sense-u-ble/)
_PKG_DIR  = Path(__file__).resolve().parent.parent
_CFG_PATH = _PKG_DIR / "config.json"
_EXP_PATH = _PKG_DIR / "config.example.json"


def _load_with_defaults(cfg_path: Path, example_path: Path) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    if example_path.exists():
        with example_path.open(encoding="utf-8") as f:
            defaults = json.load(f)
    if cfg_path.exists():
        with cfg_path.open(encoding="utf-8") as f:
            return {**defaults, **json.load(f)}
    if defaults:
        # 首启自动从 example 生成实际 config，方便用户编辑
        with cfg_path.open("w", encoding="utf-8") as f:
            json.dump(defaults, f, indent=2, ensure_ascii=False)
    return defaults.copy()


@dataclass(frozen=True)
class Config:
    # Sensor identity
    ble_address: str
    ble_mac: str
    ble_dump_raw: bool

    # BLE link timing
    ble_scan_timeout_s: float
    ble_connect_timeout_s: float
    ble_reconnect_delay_s: float
    ble_poll_interval_s: float

    # Consumer push
    consumer_url: str
    consumer_api_key: str

    # Misc
    code_file: Path
    language: str
    host: str
    port: int
    log_level: str

    @classmethod
    def load(cls, cfg_path: Path | None = None, example_path: Path | None = None) -> "Config":
        raw = _load_with_defaults(cfg_path or _CFG_PATH, example_path or _EXP_PATH)
        # _ 前缀字段是 example 里的 section header 注释，过滤掉
        raw = {k: v for k, v in raw.items() if not k.startswith("_")}

        code_file_str = str(raw.get("code_file", "./baby_code.json"))
        code_file = Path(code_file_str)
        if not code_file.is_absolute():
            code_file = (_PKG_DIR / code_file).resolve()

        return cls(
            ble_address                 = str(raw.get("ble_address", "")),
            ble_mac                     = str(raw.get("ble_mac", "")),
            ble_dump_raw                = bool(raw.get("ble_dump_raw", False)),
            ble_scan_timeout_s          = float(raw.get("ble_scan_timeout_s", 20)),
            ble_connect_timeout_s       = float(raw.get("ble_connect_timeout_s", 15)),
            ble_reconnect_delay_s       = float(raw.get("ble_reconnect_delay_s", 10)),
            ble_poll_interval_s         = float(raw.get("ble_poll_interval_s", 2)),
            consumer_url                = str(raw.get("consumer_url", "")),
            consumer_api_key            = str(raw.get("consumer_api_key", "")),
            code_file                   = code_file,
            language                    = str(raw.get("language", "zh")),
            host                        = str(raw.get("host", "0.0.0.0")),
            port                        = int(raw.get("port", 8082)),
            log_level                   = str(raw.get("log_level", "INFO")),
        )


def init_logging(level: str = "INFO") -> logging.Logger:
    """统一日志格式，返回 'sense_u_ble' logger 供模块内使用。"""
    logging.basicConfig(
        level  = getattr(logging, level.upper(), logging.INFO),
        format = "%(asctime)s [%(levelname)-5s] %(message)s",
        datefmt= "%H:%M:%S",
    )
    # bleak 自身的 INFO 太吵，调到 WARNING
    logging.getLogger("bleak").setLevel(logging.WARNING)
    return logging.getLogger("sense_u_ble")
