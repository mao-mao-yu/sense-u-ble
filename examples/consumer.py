"""Reference consumer — minimal HTTP receiver that prints sensor + alert events.

把 `consumer_url` 在 sense-u-ble 的 config.json 设成此进程地址，例如：

    "consumer_url": "http://127.0.0.1:9000/ingest"

然后：

    python examples/consumer.py

任何外部应用接 sense-u-ble 都可以参考这个最小实现。
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
            f"  @ {data.get('last_update')}"
        )
    elif data.get("type") == "alert":
        print(f"[alert {data.get('level')}] {data.get('message')}")
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000, log_level="warning")
