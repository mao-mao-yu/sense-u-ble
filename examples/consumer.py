"""Reference consumer — minimal HTTP receiver that prints sensor + alert events.

把 `consumer_url` 在 sense-u-ble 的 config.json 设成此进程地址，例如：

    "consumer_url": "http://127.0.0.1:9000/ingest"

然后：

    python examples/consumer.py

Alert ACK 协议
--------------
驱动收到设备告警后 POST type=alert 到此端点，然后**阻塞等待**本端响应。
响应体返回 {"ack": true} 时，驱动向设备发 0xF6 让 LED 停止闪烁；
返回 {"ack": false} 或无 ack 字段时，LED 继续闪烁（等待用户二次确认）。
用户重启设备时 BLE 断开，驱动自动取消挂起的告警任务。
"""
from fastapi import FastAPI, Request
import uvicorn


app = FastAPI()


@app.post("/ingest")
async def ingest(request: Request):
    data = await request.json()

    if data.get("type") == "sensor":
        print(
            f"[sensor]"
            f"  ble={data.get('ble_ok')}"
            f"  posture={data.get('posture')}"
            f"  breath={data.get('breath_rate')}"
            f"  temp={data.get('temperature')}"
            f"  battery={data.get('battery')}"
            f"  wearing={data.get('wearing')}"
            f"  activity={data.get('activity')}"
            f"  charge={data.get('charge')}"
            f"  @ {data.get('last_update')}"
        )
        return {"ok": True}

    if data.get("type") == "alert":
        mode = data.get("mode")
        msg  = data.get("message", "")
        print(f"[alert] mode={mode}  {msg}")

        # TODO: 在这里接入你的通知系统（推送、报警器等）并等待用户确认。
        # 确认后返回 {"ack": true}，驱动才会向设备发 0xF6 停止闪灯。
        # 本示例直接确认，实际场景应等用户操作。
        return {"ack": True}

    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000, log_level="warning")
