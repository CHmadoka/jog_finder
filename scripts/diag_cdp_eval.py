"""临时诊断:原生 CDP websocket 直读指定 URL 片段标签的正文(绕过 Playwright 枚举)。

用法: python scripts/diag_cdp_eval.py <url片段> [js表达式]
"""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import websockets

CDP = "http://127.0.0.1:9222"

JS_DEFAULT = """(() => {
  const t = (document.body?.innerText || '').slice(0, 500);
  return JSON.stringify({url: location.href.slice(0, 140), title: document.title.slice(0, 80), body: t});
})()"""


async def main() -> None:
    frag = sys.argv[1] if len(sys.argv) > 1 else "zhipin"
    expr = sys.argv[2] if len(sys.argv) > 2 else JS_DEFAULT
    async with httpx.AsyncClient(trust_env=False, timeout=5) as c:
        targets = (await c.get(f"{CDP}/json/list")).json()
    tgt = next((t for t in targets if t.get("type") == "page" and frag in t.get("url", "")), None)
    if not tgt:
        print(f"未找到含 {frag} 的 page target")
        return
    print("target:", tgt["url"][:120], "|", tgt["title"][:60])
    async with websockets.connect(tgt["webSocketDebuggerUrl"], max_size=10 ** 7) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": expr, "returnByValue": True}}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                r = msg.get("result", {}).get("result", {})
                val = r.get("value")
                print("结果:", val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)[:1200])
                break


asyncio.run(main())