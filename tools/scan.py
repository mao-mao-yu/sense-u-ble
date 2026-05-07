"""扫描周围 BLE 设备，列出 address / name。"""
import asyncio
from bleak import BleakScanner


async def main():
    devices = await BleakScanner.discover(timeout=10)
    for d in devices:
        print(d.address, d.name)


if __name__ == "__main__":
    asyncio.run(main())
