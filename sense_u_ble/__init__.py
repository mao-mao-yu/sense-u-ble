"""Sense-U Baby Pro BLE driver.

Public API:
    from sense_u_ble import Config, run_service
    from sense_u_ble.client import BleClient
    from sense_u_ble.protocol import parse_baby_data, build_packet
"""

__version__ = "0.1.0"

from sense_u_ble.config import Config

__all__ = ["Config", "__version__"]
