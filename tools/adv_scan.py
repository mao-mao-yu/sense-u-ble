"""BLE 广播数据扫描脚本。
设备进入配对模式后运行，捕获 Manufacturer Specific Data（可能含配对码）。
设备地址从 repo 根的 config.json 读取（ble_address 字段）。
"""
import asyncio
import json
import os
from datetime import datetime

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CFG_PATH = os.path.join(_ROOT, "config.json")
_EXP_PATH = os.path.join(_ROOT, "config.example.json")
with open(_CFG_PATH if os.path.exists(_CFG_PATH) else _EXP_PATH, encoding="utf-8") as _f:
    TARGET = json.load(_f)["ble_address"]

seen_hashes: set = set()


def on_advertisement(device: BLEDevice, adv: AdvertisementData):
    if device.address.upper() != TARGET.upper():
        return

    # 只在数据变化时打印
    mfr_raw = b""
    for company_id, data in adv.manufacturer_data.items():
        mfr_raw += company_id.to_bytes(2, "little") + data

    svc_data_key = tuple(sorted((k, v) for k, v in adv.service_data.items())) if adv.service_data else ()
    key = (mfr_raw, svc_data_key)
    if key in seen_hashes:
        return
    seen_hashes.add(key)

    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"\n[{ts}] 发现 {device.address}  RSSI={adv.rssi} dBm")
    print(f"  名称: {device.name or adv.local_name or '(无)'}")

    if adv.manufacturer_data:
        for company_id, data in adv.manufacturer_data.items():
            print(f"  Manufacturer Data  company=0x{company_id:04X}  data={data.hex()}")
            if len(data) >= 6:
                candidate = " ".join(f"{b:02X}" for b in data[:6])
                print(f"    → 前6字节候选配对码: {candidate}")
            if len(data) >= 2:
                all_bytes = " ".join(f"{b:02X}" for b in data)
                print(f"    → 全部数据字节:     {all_bytes}")
    else:
        print("  Manufacturer Data: (无)")

    if adv.service_data:
        for uuid, data in adv.service_data.items():
            print(f"  Service Data  uuid={uuid}  data={data.hex()}")

    if adv.service_uuids:
        for u in adv.service_uuids:
            print(f"  Service UUID: {u}")


async def main():
    print(f"扫描 {TARGET} 的广播数据 ...")
    print("请确保设备已进入配对模式（长按两下，蓝灯快闪）\n")
    print("按 Ctrl+C 停止\n")

    scanner = BleakScanner(detection_callback=on_advertisement)
    await scanner.start()
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await scanner.stop()
        print("\n扫描结束")


if __name__ == "__main__":
    asyncio.run(main())
