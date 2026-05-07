"""GATT 完整服务发现脚本。
连接设备后列出所有 Service / Characteristic / Property，可读字段尝试读一次。
设备地址从 repo 根的 config.json 读取（ble_address 字段）。
"""
import asyncio
import json
import os
from datetime import datetime

from bleak import BleakClient


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CFG_PATH = os.path.join(_ROOT, "config.json")
_EXP_PATH = os.path.join(_ROOT, "config.example.json")
with open(_CFG_PATH if os.path.exists(_CFG_PATH) else _EXP_PATH, encoding="utf-8") as _f:
    ADDRESS = json.load(_f)["ble_address"]


async def discover():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 连接 {ADDRESS} ...")
    async with BleakClient(ADDRESS, timeout=15) as client:
        print(f"已连接: {client.is_connected}\n")

        for svc in client.services:
            print(f"{'='*70}")
            print(f"SERVICE  {svc.uuid}")
            print(f"         描述: {svc.description}")
            for char in svc.characteristics:
                props = ", ".join(char.properties)
                print(f"  CHAR   {char.uuid}")
                print(f"         handle={char.handle}  描述={char.description}")
                print(f"         properties=[{props}]")

                if "read" in char.properties:
                    try:
                        val = await client.read_gatt_char(char.uuid)
                        try:
                            text = val.decode("utf-8").strip()
                        except Exception:
                            text = ""
                        suffix = f"  ({text})" if text else ""
                        print(f"         值: {val.hex()}{suffix}")
                    except Exception as e:
                        print(f"         读取失败: {e}")

                for desc in char.descriptors:
                    print(f"    DESC {desc.uuid}  handle={desc.handle}")
            print()


if __name__ == "__main__":
    asyncio.run(discover())
