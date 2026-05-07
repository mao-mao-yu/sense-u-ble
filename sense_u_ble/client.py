"""BLE 连接 + polling 主循环。

把 protocol.py 的解析函数和 BleakClient 黏起来；从外部看就是
`run_loop(cfg, on_alert)` 一个 coroutine，跑完就再起一轮重连，永不退出。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from bleak import BleakClient, BleakScanner

from sense_u_ble.config import Config
from sense_u_ble import protocol, state

log = logging.getLogger("sense_u_ble.client")

# 当前 BleakClient 引用，供外部 `request_refresh()` 使用
_current_client: BleakClient | None = None


async def request_refresh(cfg: Config) -> bool:
    """向设备发送 0xBA get_baby_data，触发立即推送一份完整快照。"""
    if _current_client and _current_client.is_connected:
        try:
            await _current_client.write_gatt_char(
                protocol.char_settings(cfg),
                protocol.pkt_get_baby_data(),
                response=True,
            )
            log.debug("主动刷新：已发送 0xBA")
            return True
        except Exception as e:
            log.debug(f"主动刷新失败: {e}")
    return False


async def run_loop(
    cfg: Config,
    on_alert: Callable[[str, str], Awaitable[None]],
) -> None:
    """永不退出的 BLE 连接 + polling 循环。

    on_alert: async callable(message, level) 触发 prone / breath 告警时调用。
    """
    addr     = cfg.ble_address
    char_reg = protocol.char_register(cfg)
    char_rt  = protocol.char_realtime(cfg)   # CHAR_2 实时推送
    char_set = protocol.char_settings(cfg)   # CHAR_4 指令/响应

    while True:
        code = protocol.load_baby_code(cfg.code_file)
        if code is None:
            log.warning(f"未找到 {cfg.code_file}，请先运行配对工具 (tools/pairing.py)")
            await asyncio.sleep(10)
            continue

        log.debug(f"扫描 {addr}...")
        state.sensor_state.update(ble_ok=False)
        state.clear_ble_data()
        await state.broadcast({"type": "sensor", **state.sensor_state})

        try:
            found_evt    = asyncio.Event()
            found_device = None

            def _detection_cb(dev, _):
                nonlocal found_device
                if dev.address.upper() == addr.upper() and not found_evt.is_set():
                    found_device = dev
                    found_evt.set()

            async with BleakScanner(detection_callback=_detection_cb):
                try:
                    await asyncio.wait_for(found_evt.wait(), timeout=cfg.ble_scan_timeout_s)
                except asyncio.TimeoutError:
                    pass

            device = found_device
            if device is None:
                log.warning("未扫描到设备，5 秒后重试...")
                await asyncio.sleep(5)
                continue

            log.info("找到设备，连接中...")
            disc_evt  = asyncio.Event()
            init_done = asyncio.Event()   # 初始化链 C0→F5→B2→B3→B0 完成标志
            connect_ts = time.time()

            async with BleakClient(
                device, timeout=cfg.ble_connect_timeout_s,
                disconnected_callback=lambda _: disc_evt.set(),
            ) as client:
                global _current_client
                _current_client = client
                log.info("已连接，等待 GATT 就绪...")
                await asyncio.sleep(2.5)

                # ── 初始化链：0x70 鉴权成功后按顺序发送，由 CHAR_4 回包驱动 ──
                # 每收到前一条的 ACK 才发下一条，最终 set init_done
                _INIT_CHAIN = [
                    (0xC0, protocol.pkt_leaning,       "F5 LeaningType"),
                    (0xF5, protocol.pkt_temp_alarm,    "B2 TempAlarm"),
                    (0xB2, protocol.pkt_kicking_alarm, "B3 KickingAlarm"),
                    (0xB3, protocol.pkt_breath_alarm,  "B0 BreathAlarm"),
                    (0xB0, None,                       "初始化完成"),
                ]

                async def _send_ack(mode: int) -> None:
                    """向 CHAR_4 发 0xF6，让设备停止闪灯。"""
                    try:
                        await client.write_gatt_char(
                            char_set, protocol.pkt_alert_ack(mode), response=False
                        )
                        log.debug(f"0xF6 ACK 已发送: mode={mode}")
                    except Exception as e:
                        log.debug(f"0xF6 ACK 失败: {e}")

                async def on_settings(_s, raw: bytearray):
                    d = bytes(raw)
                    if not d:
                        return
                    h = d[0]
                    if h == 0xBA:
                        await protocol.parse_baby_data(cfg, d, on_alert)
                        return
                    # 初始化链驱动
                    for resp_hdr, next_fn, label in _INIT_CHAIN:
                        if h == resp_hdr:
                            if next_fn is not None:
                                try:
                                    await client.write_gatt_char(
                                        char_set, next_fn(), response=False
                                    )
                                    log.debug(f"初始化链 → {label}")
                                except Exception as e:
                                    log.warning(f"初始化链发送失败 ({label}): {e}")
                            else:
                                log.info(label)
                                init_done.set()
                            break

                async def on_register(_s, raw: bytearray):
                    d = bytes(raw)
                    if not d or d[0] != 0x70:
                        return
                    if len(d) >= 2 and d[1] == 0x00:
                        log.info("鉴权成功！启动初始化链 (GET_BATCH)...")
                        state.sensor_state.update(ble_ok=True)
                        await state.broadcast({"type": "sensor", **state.sensor_state})
                        try:
                            await client.write_gatt_char(
                                char_set, protocol.pkt_get_batch(), response=False
                            )
                        except Exception as e:
                            log.warning(f"GET_BATCH 发送失败: {e}")
                    elif len(d) >= 2 and d[1] == 0x01:
                        log.error(
                            f"鉴权失败！baby_code 无效，请删除 {cfg.code_file} 后重新配对"
                        )

                async def on_realtime(_s, raw: bytearray):
                    await protocol.parse_realtime_data(bytes(raw), _send_ack)

                def _wrap(name: str, handler):
                    async def _aw(s, raw):
                        if cfg.ble_dump_raw:
                            log.info(f"RX {name}: {bytes(raw).hex(' ')}")
                        await handler(s, raw)
                    return _aw

                for uuid, name, handler in [
                    (char_reg, "CHAR_1", on_register),
                    (char_rt,  "CHAR_2", on_realtime),
                    (char_set, "CHAR_4", on_settings),
                ]:
                    log.info(f"订阅 {name}...")
                    for attempt in range(3):
                        try:
                            await asyncio.wait_for(
                                client.start_notify(uuid, _wrap(name, handler)), timeout=10
                            )
                            log.info(f"已订阅 {name}")
                            await asyncio.sleep(0.3)
                            break
                        except asyncio.TimeoutError:
                            log.warning(f"订阅 {name} 超时 (尝试 {attempt+1}/3)")
                            if attempt < 2:
                                await asyncio.sleep(1.0)
                        except Exception as e:
                            if attempt < 2:
                                await asyncio.sleep(1.0)
                            else:
                                log.warning(f"订阅 {name} 失败: {e}")

                pkt = protocol.pkt_reconnect(code)
                log.info(f"发送鉴权重连包 (0x70): {pkt.hex()}")
                try:
                    await asyncio.wait_for(
                        client.write_gatt_char(char_reg, pkt, response=False),
                        timeout=10,
                    )
                except asyncio.TimeoutError:
                    log.warning("鉴权写入超时")
                except Exception as e:
                    log.warning(f"鉴权写入失败: {e}")

                # 等待初始化链完成（最多 20 秒），超时则直接进轮询
                try:
                    await asyncio.wait_for(init_done.wait(), timeout=20)
                except asyncio.TimeoutError:
                    log.warning("初始化链超时，跳过直接进入轮询")

                # 立即拉一次完整快照
                try:
                    await client.write_gatt_char(
                        char_set, protocol.pkt_get_baby_data(), response=True
                    )
                except Exception:
                    pass

                # 主循环：每 N 秒 polling 一次 0xBA（补充 CHAR_2 推送）
                while not disc_evt.is_set():
                    try:
                        await asyncio.wait_for(disc_evt.wait(), timeout=cfg.ble_poll_interval_s)
                        break  # disc_evt fired → 正常断线
                    except asyncio.TimeoutError:
                        if not client.is_connected:
                            break
                        try:
                            await client.write_gatt_char(
                                char_set, protocol.pkt_get_baby_data(), response=True,
                            )
                            log.debug("0xBA polling")
                        except Exception as ke:
                            log.warning(f"0xBA 发送失败: {ke}")

                _current_client = None
                elapsed = int(time.time() - connect_ts)
                log.info(f"连接断开（持续 {elapsed}s）")

        except Exception as e:
            log.warning(f"错误: {type(e).__name__}: {e}")

        state.sensor_state.update(ble_ok=False)
        state.clear_ble_data()
        await state.broadcast({"type": "sensor", **state.sensor_state})
        log.debug(f"{cfg.ble_reconnect_delay_s} 秒后重连...")
        await asyncio.sleep(cfg.ble_reconnect_delay_s)
