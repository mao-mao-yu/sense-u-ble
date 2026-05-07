"""
Sense-U Pro BLE 完整协议实现
参考: esphome-sense-u / protocol.md + senseu.cpp

两阶段流程:
  Phase 1 (配对): 设备进入配对模式 -> 获取 baby_code (运行一次)
  Phase 2 (连接): 用 baby_code 连接, 接收传感器数据 (持续运行)

baby_code 保存在 baby_code.json, 重启后无需再配对
"""
import asyncio
import json
import os
import sys
import time
from bleak import BleakClient
from datetime import datetime

# macOS 的 CoreBluetooth 在 write-without-response 上偶尔丢包，
# 必须用 write-with-response (ATT ACK)。Windows / Linux 上沿用原 False 行为。
_WRITE_RESP = sys.platform == "darwin"

_ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE_FILE = os.path.join(_ROOT, "baby_code.json")
LOG_FILE  = os.path.join(_ROOT, "logs", "ble_protocol.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# 从 repo 根 config.json 读取 ble_address / ble_mac
_CFG_PATH = os.path.join(_ROOT, "config.json")
_EXP_PATH = os.path.join(_ROOT, "config.example.json")
with open(_CFG_PATH if os.path.exists(_CFG_PATH) else _EXP_PATH, encoding="utf-8") as _f:
    _CFG = json.load(_f)
ADDRESS = _CFG["ble_address"]
_MAC_HEX = (_CFG.get("ble_mac") or _CFG.get("ble_address", "")) \
    .replace(":", "").replace("-", "").lower()[-12:]

# UUID 前缀 + MAC 后缀
def _uuid(prefix):
    return f"{prefix}-{_MAC_HEX}"

CHAR_REGISTER   = _uuid("01021921-9e06-a079-2e3f")  # DATA_CHAR_1
CHAR_REALTIME   = _uuid("01021922-9e06-a079-2e3f")  # DATA_CHAR_2
CHAR_DATA_GUIDE = _uuid("01021923-9e06-a079-2e3f")  # DATA_CHAR_3
CHAR_SETTINGS   = _uuid("01021925-9e06-a079-2e3f")  # DATA_CHAR_4

# UID 包: 0x69 + 固定 UID 字节 + 0x00  (来自 senseu.h SET_UID_DATA)
UID_PACKET = bytes([
    0x69,
    0x31,0x32,0x33,0x34,0x35,0x36,0x37,0x38,0x39,
    0x30,0x35,0x34,0x33,0x32,0x31,0x30,
    0x00
])  # 18 bytes


# ──────────────── 工具函数 ────────────────

def now():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

def log(text):
    print(text)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(text + "\n")

def u8(b):
    return b & 0xFF

def ts_be() -> bytes:
    """当前 Unix 秒时间戳, 大端 4 字节 (匹配 protocol.md 示例)"""
    t = int(time.time())
    return bytes([(t >> 24) & 0xFF, (t >> 16) & 0xFF, (t >> 8) & 0xFF, t & 0xFF])


# ──────────────── 包构造 ────────────────

def build_register_type() -> bytes:
    """0x68 RegisterType: [0x68, ts_be(4), 0x01, 0x00, 0x32, 0x03, 0x00, 0×8 zeros]"""
    buf = bytearray(18)
    buf[0] = 0x68
    buf[1:5] = ts_be()
    buf[5] = 0x01
    buf[6] = 0x00
    buf[7] = 0x32
    buf[8] = 0x03
    buf[9] = 0x00
    return bytes(buf)

def build_reconnect(baby_code: bytes) -> bytes:
    """0x70 ReconnectionType: [0x70, baby_code(6), ts_be(4), 0×7 zeros]"""
    buf = bytearray(18)
    buf[0] = 0x70
    buf[1:7] = baby_code[:6]
    buf[7:11] = ts_be()
    return bytes(buf)

def build_get_batch() -> bytes:
    """0xC0 0x01 + zeros (20 bytes) → DATA_CHAR_4"""
    buf = bytearray(20)
    buf[0] = 0xC0
    buf[1] = 0x01
    return bytes(buf)

def build_power_on() -> bytes:
    """0xF5 F2 32 03 00 → DATA_CHAR_4 (启用告警)"""
    return bytes([0xF5, 0xF2, 0x32, 0x03, 0x00])

def build_temp_alarm() -> bytes:
    """0xB2 体感温度告警阈值 → DATA_CHAR_4"""
    return bytes([0xB2, 0x00, 0x68, 0x01, 0xC8, 0x00])

def build_kicking_alarm() -> bytes:
    """0xB3 踢腿告警 → DATA_CHAR_4"""
    return bytes([0xB3, 0x00, 0x0F, 0x03, 0x00])

def build_breath_alarm() -> bytes:
    """0xB0 呼吸告警阈值 → DATA_CHAR_4"""
    return bytes([0xB0, 0x01, 0x19])


# ──────────────── 传感器数据解析 ────────────────

def parse_sensor(data: bytes):
    if len(data) < 2:
        return
    rt = (u8(data[0]) >> 3) & 0x1F
    st = (((u8(data[0]) << 8) | u8(data[1])) >> 6) & 0x1F
    raw = " ".join(f"{b:02x}" for b in data)

    if rt == 0x06:  # Battery
        if st == 0x01 and len(data) >= 7:
            log(f"  [电量] {u8(data[6])}%  告警标志={u8(data[5])}")
        elif st == 0x02:
            log(f"  [穿戴] 未佩戴")
        elif st == 0x04:
            log(f"  [穿戴] 已佩戴")
        else:
            log(f"  [电量] st={st} raw={raw}")

    elif rt == 0x08:  # Special Record
        if st == 0x01 and len(data) >= 8:
            temp = (u8(data[6]) << 8 | u8(data[5])) / 10.0
            humid = u8(data[7])
            log(f"  [温湿度] 体感温度={temp:.1f}°C  湿度={humid}%")

        elif st == 0x02 and len(data) >= 8:  # Alert
            mode = u8(data[5])
            notify = u8(data[6])
            if mode == 2:
                log(f"  [告警] 姿势告警  notify={notify}")
            elif mode in (3, 4, 7) and len(data) >= 10:
                temp = (u8(data[9]) << 8 | u8(data[8])) / 10.0
                label = {3: "温度过高", 4: "温度过低", 7: "温度下降"}.get(mode, f"mode={mode}")
                log(f"  [告警] {label}  {temp:.1f}°C  notify={notify}")
            elif mode in (8, 9):
                log(f"  [告警] 呼吸{'过快' if mode==8 else '过慢'}  notify={notify}")
            else:
                log(f"  [告警] mode={mode}  notify={notify}  raw={raw}")

        elif st == 0x04 and len(data) >= 6:
            postures = {0: "仰卧", 1: "俯卧", 2: "左侧卧", 3: "右侧卧"}
            log(f"  [姿势] {postures.get(u8(data[5]), f'未知({u8(data[5])})')}")

        elif st == 0x05 and len(data) >= 7:
            rate = u8(data[5])
            online = u8(data[6])
            if rate < 200:
                log(f"  [呼吸] {rate} 次/分钟  在线={'是' if online else '否'}")

        elif st == 0x07:
            log(f"  [活动] raw={raw}")
        elif st == 0x08:
            log(f"  [信号] raw={raw}")
        else:
            log(f"  [数据] rt={rt} st={st} raw={raw}")
    else:
        log(f"  [数据] rt={rt} raw={raw}")


# ──────────────── Phase 1: 配对 (获取 baby_code) ────────────────

async def phase1_pair() -> bytes | None:
    log(f"\n{'='*60}")
    log(f"[{now()}] Phase 1: 配对 — 获取 baby_code")
    log(f"         请确认设备已进入配对模式 (长按两下, 蓝灯慢闪)")

    baby_code = None
    ev_69 = asyncio.Event()
    ev_68 = asyncio.Event()

    async def on_register(sender, data: bytearray):
        nonlocal baby_code
        data = bytes(data)
        log(f"[{now()}] RX: {' '.join(f'{b:02x}' for b in data)}")

        if not data:
            return

        if data[0] == 0x69:
            if len(data) >= 2 and data[1] == 0x00:
                log(f"  0x69 UID 注册成功 → 发送 RegisterType (0x68)")
                ev_69.set()
            else:
                log(f"  0x69 UID 注册失败 (byte[1]={data[1] if len(data)>1 else '?'})")

        elif data[0] == 0x68:
            if len(data) >= 8 and data[1] == 0x00:
                baby_code = bytes(data[2:8])
                log(f"  *** baby_code: {baby_code.hex()} ***")
                ev_68.set()
            else:
                log(f"  0x68 RegisterType 失败 (byte[1]={data[1] if len(data)>1 else '?'})")

    async with BleakClient(ADDRESS, timeout=20) as client:
        log(f"[{now()}] 已连接")

        # ── 全 CHAR 监听：把所有从设备来的通知打印出来用于诊断 ────────────
        async def _spy(name: str, _sender, data: bytearray):
            d = bytes(data)
            log(f"[{now()}] RX {name}: {d.hex(' ')}")

        # CHAR_1 (REGISTER) 仍然走原 on_register 处理 0x69/0x68 协议
        # 同时其他 3 个 char 全部 spy，纯粹打印
        await client.start_notify(CHAR_REGISTER, on_register)
        for _ch, _name in [
            (CHAR_REALTIME,   "CHAR_2"),
            (CHAR_DATA_GUIDE, "CHAR_3"),
            (CHAR_SETTINGS,   "CHAR_4"),
        ]:
            try:
                await client.start_notify(
                    _ch, lambda s, d, n=_name: asyncio.create_task(_spy(n, s, d))
                )
                log(f"[{now()}] 已监听 {_name}")
            except Exception as e:
                log(f"[{now()}] 监听 {_name} 失败: {e}")

        # 列出 GATT 全部 char + 属性，确认 0x01021921... 是否真的存在且可写
        try:
            for svc in client.services:
                log(f"[{now()}] SVC {svc.uuid}")
                for ch in svc.characteristics:
                    log(f"           CHR {ch.uuid}  props={','.join(ch.properties)}")
        except Exception as e:
            log(f"[{now()}] 服务枚举失败: {e}")

        await asyncio.sleep(0.5)

        # Step 1: 发送 UID 包（macOS 用 ACK 写入修丢包；Windows 沿用原 response=False）
        log(f"[{now()}] TX UID (0x69): {UID_PACKET.hex()}")
        try:
            await client.write_gatt_char(CHAR_REGISTER, UID_PACKET, response=_WRITE_RESP)
            log(f"[{now()}] UID 写入完成{' (ACK)' if _WRITE_RESP else ''}")
        except Exception as e:
            log(f"[{now()}] UID 写入异常: {e}")

        # 等待 0x69 响应；5s 内没回则重发一次（macOS 偶发首包丢失）
        try:
            await asyncio.wait_for(ev_69.wait(), timeout=5)
        except asyncio.TimeoutError:
            log(f"[{now()}] 5s 未收到 0x69 响应，重发 UID 包...")
            try:
                await client.write_gatt_char(CHAR_REGISTER, UID_PACKET, response=_WRITE_RESP)
            except Exception as e:
                log(f"[{now()}] UID 重发异常: {e}")
            try:
                await asyncio.wait_for(ev_69.wait(), timeout=8)
            except asyncio.TimeoutError:
                log(f"[{now()}] 超时: 未收到 0x69 响应 (设备是否在配对模式?)")
                return None

        # Step 2: 发送 RegisterType
        pkt = build_register_type()
        log(f"[{now()}] TX RegisterType (0x68): {pkt.hex()}")
        try:
            await client.write_gatt_char(CHAR_REGISTER, pkt, response=_WRITE_RESP)
            log(f"[{now()}] RegisterType 写入完成{' (ACK)' if _WRITE_RESP else ''}")
        except Exception as e:
            log(f"[{now()}] RegisterType 写入异常: {e}")

        # 等待 0x68 响应 (含 baby_code)
        try:
            await asyncio.wait_for(ev_68.wait(), timeout=10)
        except asyncio.TimeoutError:
            log(f"[{now()}] 超时: 未收到 0x68 响应 (含 baby_code)")
            return None

        await asyncio.sleep(0.5)
        # 退出 with 块时自动断开连接

    return baby_code


# ──────────────── Phase 2: 连接并接收数据 ────────────────

async def phase2_connect(baby_code: bytes):
    log(f"\n{'='*60}")
    log(f"[{now()}] Phase 2: 连接  baby_code={baby_code.hex()}")

    async with BleakClient(ADDRESS, timeout=15) as client:
        log(f"[{now()}] 已连接")

        # ── 设置命令序列回调 (DATA_CHAR_4) ──
        async def on_settings(sender, data: bytearray):
            data = bytes(data)
            raw = " ".join(f"{b:02x}" for b in data)
            log(f"[{now()}] RX SETTINGS: {raw}")

            if not data:
                return

            if data[0] == 0xC0:
                log(f"  [0xC0] GET_BATCH 响应 → POWER_ON (0xF5)")
                await client.write_gatt_char(CHAR_SETTINGS, build_power_on(), response=False)

            elif data[0] == 0xF5:
                log(f"  [0xF5] LEANING 响应 → TEMP_ALARM (0xB2)")
                await client.write_gatt_char(CHAR_SETTINGS, build_temp_alarm(), response=False)

            elif data[0] == 0xB2:
                log(f"  [0xB2] TEMP_ALARM 响应 → KICKING_ALARM (0xB3)")
                await client.write_gatt_char(CHAR_SETTINGS, build_kicking_alarm(), response=False)

            elif data[0] == 0xB3:
                log(f"  [0xB3] KICKING 响应 → BREATH_ALARM (0xB0)")
                await client.write_gatt_char(CHAR_SETTINGS, build_breath_alarm(), response=False)

            elif data[0] == 0xB0:
                log(f"  [0xB0] 配置完成! 开始接收传感器数据...")

            else:
                parse_sensor(data)

        # ── 鉴权回调 (DATA_CHAR_1) ──
        async def on_register(sender, data: bytearray):
            data = bytes(data)
            raw = " ".join(f"{b:02x}" for b in data)
            log(f"[{now()}] RX REGISTER: {raw}")

            if not data:
                return

            if data[0] == 0x70:
                if len(data) >= 2 and data[1] == 0x00:
                    log(f"  *** 0x70 鉴权成功! 设备已连接 (绿灯闪烁) ***")
                    await client.write_gatt_char(CHAR_SETTINGS, build_get_batch(), response=False)
                elif len(data) >= 2 and data[1] == 0x01:
                    log(f"  *** 0x70 鉴权失败! baby_code 无效, 请删除 {CODE_FILE} 后重新配对 ***")
                else:
                    log(f"  [0x70] 未知响应")
            else:
                parse_sensor(data)

        # ── 传感器数据回调 (DATA_CHAR_2 / DATA_CHAR_3) ──
        def on_realtime(sender, data: bytearray):
            raw = " ".join(f"{b:02x}" for b in data)
            log(f"[{now()}] RX REALTIME: {raw}")
            parse_sensor(bytes(data))

        def on_data_guide(sender, data: bytearray):
            raw = " ".join(f"{b:02x}" for b in data)
            log(f"[{now()}] RX DATA_GUIDE: {raw}")
            parse_sensor(bytes(data))

        # Windows BLE 需要在连接后稍等再做 GATT 操作
        await asyncio.sleep(1.5)

        # 订阅全部 4 个 DATA_CHAR (带重试)
        for uuid, name, handler in [
            (CHAR_REGISTER,   "CHAR_1 (REGISTER)",   on_register),
            (CHAR_REALTIME,   "CHAR_2 (REALTIME)",   on_realtime),
            (CHAR_DATA_GUIDE, "CHAR_3 (DATA_GUIDE)", on_data_guide),
            (CHAR_SETTINGS,   "CHAR_4 (SETTINGS)",   on_settings),
        ]:
            for attempt in range(3):
                try:
                    await client.start_notify(uuid, handler)
                    log(f"[{now()}] 已订阅 {name}")
                    await asyncio.sleep(0.3)
                    break
                except Exception as e:
                    if attempt < 2:
                        log(f"[{now()}] 订阅 {name} 失败 (尝试 {attempt+1}/3): {e}, 重试中...")
                        await asyncio.sleep(1.0)
                    else:
                        log(f"[{now()}] 订阅 {name} 最终失败: {e}")

        # 发送 0x70 鉴权包
        pkt = build_reconnect(baby_code)
        log(f"[{now()}] TX 0x70 鉴权: {pkt.hex()}")
        await client.write_gatt_char(CHAR_REGISTER, pkt, response=False)

        # 持续监听
        log(f"[{now()}] 持续监听中 (Ctrl+C 停止)...\n")
        try:
            while True:
                await asyncio.sleep(10)
                log(f"[{now()}] --- 心跳 ---")
        except KeyboardInterrupt:
            pass


# ──────────────── baby_code 持久化 ────────────────

def load_code() -> bytes | None:
    if not os.path.exists(CODE_FILE):
        return None
    try:
        with open(CODE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        h = d.get("baby_code", "")
        if len(h) == 12:
            return bytes.fromhex(h)
    except Exception:
        pass
    return None

def save_code(code: bytes):
    with open(CODE_FILE, "w", encoding="utf-8") as f:
        json.dump({"baby_code": code.hex(), "address": ADDRESS}, f, indent=2)
    log(f"[{now()}] baby_code 已保存到 {CODE_FILE}")


# ──────────────── 主程序 ────────────────

async def main():
    baby_code = load_code()

    if baby_code is None:
        log(f"[{now()}] 未找到 baby_code, 需要先配对")
        log(f"[{now()}] 请长按设备两下进入配对模式 (蓝灯慢闪), 然后按 Enter")
        input()
        baby_code = await phase1_pair()
        if baby_code is None:
            log(f"[{now()}] 配对失败, 请重试")
            return
        save_code(baby_code)
        log(f"[{now()}] 配对成功! baby_code={baby_code.hex()}")
        log(f"[{now()}] 3 秒后自动连接...")
        await asyncio.sleep(3)
    else:
        log(f"[{now()}] 已加载 baby_code: {baby_code.hex()}")

    await phase2_connect(baby_code)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log(f"\n[{now()}] 已停止")
