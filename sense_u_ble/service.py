"""sense-u-ble HTTP 服务 — 独立进程入口。

启动后做四件事：
  1. 拉起 BLE client loop（持续连接 / polling / 重连）
  2. 把 sensor / alert 事件 POST 到 consumer_url（baby-sentinel 或其它消费方）
  3. 暴露 GET  /api/sensor          —— 当前传感器快照
  4. 暴露 POST /api/sensor/refresh  —— 强制设备立即推送一份完整数据

CLI:
    python -m sense_u_ble.service
    sense-u-ble                      # 经 pyproject [project.scripts] 暴露
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from sense_u_ble import client, state
from sense_u_ble.config import Config, init_logging


def _build_app(cfg: Config, log: logging.Logger) -> FastAPI:
    """构造 FastAPI app；cfg / log 提前注入避免模块级单例。"""

    # 推送到 consumer 的 HTTP 客户端（连接复用）
    push_client = httpx.AsyncClient(
        timeout=httpx.Timeout(2.0, connect=1.0),
        headers={"Content-Type": "application/json",
                 **({"X-API-Key": cfg.consumer_api_key} if cfg.consumer_api_key else {})},
    )

    async def _push(data: dict) -> None:
        if not cfg.consumer_url:
            return
        try:
            await push_client.post(cfg.consumer_url, json=data)
        except Exception as e:
            # 消费方下线时静默忽略；BLE 进程仍正常工作
            log.debug(f"consumer push failed ({type(e).__name__}): {e}")

    state.set_broadcast(_push)

    async def _on_alert(message: str, level: str) -> None:
        # alert event 跟 sensor event 走同一推送通道，consumer 端通过 type 字段分流
        await _push({
            "type":      "alert",
            "level":     level,
            "message":   message,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        })

    @asynccontextmanager
    async def _lifespan(_: FastAPI):
        loop_task = asyncio.create_task(client.run_loop(cfg, _on_alert))
        try:
            yield
        finally:
            loop_task.cancel()
            await push_client.aclose()

    app = FastAPI(title="sense-u-ble", lifespan=_lifespan)

    @app.get("/api/sensor")
    async def get_sensor():
        """当前传感器状态快照。"""
        return JSONResponse(state.sensor_state)

    @app.post("/api/sensor/refresh")
    async def refresh_sensor():
        """向设备发送 0xBA，触发立即推送一份完整快照。"""
        ok = await client.request_refresh(cfg)
        return JSONResponse({
            "ok": ok,
            "ble_connected": state.sensor_state.get("ble_ok", False),
        })

    @app.get("/health")
    async def health():
        return JSONResponse({"ok": True, "ble_ok": state.sensor_state.get("ble_ok", False)})

    return app


def main() -> None:
    cfg = Config.load()
    log = init_logging(cfg.log_level)
    log.info(f"sense-u-ble starting on http://{cfg.host}:{cfg.port}")
    log.info(f"  ble_address     {cfg.ble_address or '(empty — set in config.json)'}")
    log.info(f"  consumer_url    {cfg.consumer_url or '(empty — events not pushed)'}")
    log.info(f"  language        {cfg.language}")

    app = _build_app(cfg, log)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()
