"""告警消息模板（zh / ja / en）。

只放本服务自己会发出的告警字符串。consumer 收到后会原样转发给用户终端
（Discord / Bark / WebSocket / 浏览器 UI），所以这里直接渲染成最终文本。
"""

_TR: dict[str, dict[str, str]] = {
    "zh": {
        "alert_prone":  "🚨 俯卧警告\n持续 {seconds} 秒处于俯卧状态，请立即确认。",
        "alert_breath": "🫁 呼吸异常警告\n呼吸 {rate} 次/分持续 {seconds} 秒，请立即确认。",
    },
    "ja": {
        "alert_prone":  "🚨 うつ伏せ警告\n{seconds} 秒間うつ伏せの状態が続いています。すぐに確認してください。",
        "alert_breath": "🫁 呼吸異常警告\n呼吸数 {rate} 回/分の状態が {seconds} 秒続いています。すぐに確認してください。",
    },
    "en": {
        "alert_prone":  "🚨 Prone alert\nBaby has been prone for {seconds}s. Please check immediately.",
        "alert_breath": "🫁 Breathing alert\nBreath rate {rate}/min for {seconds}s. Please check immediately.",
    },
}

DEFAULT_LANG = "zh"


def t(key: str, lang: str, **kw) -> str:
    table = _TR.get(lang) or _TR[DEFAULT_LANG]
    s = table.get(key) or _TR[DEFAULT_LANG].get(key, key)
    if kw:
        try:
            return s.format(**kw)
        except (KeyError, IndexError):
            return s
    return s
