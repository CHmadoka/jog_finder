"""浏览器接管自测脚本。

用法:
    .venv\\Scripts\\python scripts\\check_browser.py

行为:确保浏览器以调试模式运行并连接,输出版本信息,
并逐个探测三个平台的登录态(未登录会打开登录页让用户手动登录)。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.browser import browser_manager  # noqa: E402
from app.config import config  # noqa: E402
from app.platforms import get_platform  # noqa: E402


async def main() -> None:
    browser = await browser_manager.ensure()
    version = browser.version
    print(f"[OK] 已连接浏览器,版本: {version}")
    ctx = browser_manager.primary_context()
    print(f"[OK] 上下文数: {len(browser.contexts)},已开标签页: {len(ctx.pages)}")

    for pid in ("job51", "boss", "liepin"):
        plat = get_platform(pid)
        try:
            page = await browser_manager.get_or_open_page(plat.home_prefix, plat.home_url)
            logged_in = await plat.is_logged_in(page)
            print(f"[{'OK' if logged_in else '!!'}] {plat.name}: {'已登录' if logged_in else '未登录(请在弹出的窗口中手动登录一次)'}")
        except Exception as exc:  # noqa: BLE001
            print(f"[ERR] {plat.name}: {exc}")

    print("\n浏览器保持运行以便手动登录。连接会话已断开(浏览器不关闭)。")
    await browser_manager.shutdown()


if __name__ == "__main__":
    print(f"CDP 端口: {config.CDP_PORT}")
    asyncio.run(main())