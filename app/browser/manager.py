"""通过 CDP 接管用户浏览器。

策略:
1. 若调试端口已有浏览器(用户手动启动或上次拉起仍在运行),直接连接;
2. 否则用专用 profile 目录拉起 Chrome/Edge(--remote-debugging-port),
   首次由用户在该窗口内登录各平台,登录态随 profile 持久化;
3. Chrome 136+ 禁止默认用户目录开启 CDP,因此固定使用独立 profile。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import httpx
import websockets
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from ..config import config

logger = logging.getLogger(__name__)

# 常见浏览器安装位置(Windows)
_BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
]


def find_browser_exe() -> str:
    """定位 Chrome/Edge 可执行文件。"""
    if config.BROWSER_PATH and Path(config.BROWSER_PATH).exists():
        return config.BROWSER_PATH
    for p in _BROWSER_CANDIDATES:
        if Path(p).exists():
            return p
    raise FileNotFoundError(
        "未找到 Chrome/Edge,请在 .env 中设置 BROWSER_PATH 为浏览器可执行文件路径"
    )


async def is_cdp_alive(port: int, timeout: float = 2.0) -> bool:
    """探测 CDP 调试端口是否可用。

    必须禁用系统代理(trust_env=False):系统代理(如 127.0.0.1:7897)会把
    对 127.0.0.1:9222 的探测也截走并返回 502,导致"端口活着被误判已死",
    进而反复重复拉起浏览器(往现有实例塞 about:blank 标签页)。
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/json/version")
            return resp.status_code == 200
    except Exception:
        return False


async def wait_page_target(port: int, url_frag: str, timeout_s: float = 20) -> bool:
    """等待 CDP /json/list 出现指定 URL 的页面 target 且稳定。

    Chrome 启动时自身导航(如打开 zhipin.com)会切换渲染进程 target,
    过早 connect 会拿到已废弃的 page(后续 evaluate 报 "Target ... has been closed")。
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                lst = (await client.get(f"http://127.0.0.1:{port}/json/list")).json()
            if any(
                t.get("type") == "page" and url_frag in (t.get("url") or "")
                for t in lst
            ):
                return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False


class BrowserManager:
    """管理浏览器进程生命周期与 Playwright 连接。"""

    def __init__(self) -> None:
        self._playwright = None
        self._browser: Browser | None = None
        self._proc: subprocess.Popen | None = None
        self._owns_process = False  # 进程是否由本程序拉起(决定退出时是否关闭)

    @property
    def connected(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def ensure(self, start_url: str | None = None) -> Browser:
        """确保已连接到浏览器;必要时自动拉起。

        start_url:冷启动时的首屏地址。抓取流程默认用平台首页(作为 cookie 载体),
        "打开原始页面"则直接传目标岗位页,避免先开一个多余的主页标签。
        """
        if self.connected:
            return self._browser  # type: ignore[return-value]

        if not await is_cdp_alive(config.CDP_PORT):
            await self._launch(start_url)
        await self._connect()
        return self._browser  # type: ignore[return-value]

    async def _launch(self, start_url: str | None = None, fast: bool = False) -> None:
        exe = find_browser_exe()
        profile = Path(config.BROWSER_PROFILE_DIR).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        # 冷启动首屏:抓取流程用平台首页(让浏览器自身导航管线完成首次加载,
        # 规避"CDP 连接初期导航未就绪"的 goto 假成功);打开页面时直接以目标页
        # 为首屏(Chrome 原生导航,无 blank 中转阶段,单标签直达)
        start_url = start_url or "https://www.zhipin.com/"
        # --no-proxy-server:专用抓取浏览器直连(系统代理的机房出口 IP 更易触发风控,
        # 且代理节点波动会直接导致导航失败)
        args = [
            exe,
            f"--remote-debugging-port={config.CDP_PORT}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            "--hide-crash-restore-bubble",
            "--start-maximized",
        ]
        if config.BROWSER_NO_PROXY:
            args.append("--no-proxy-server")
        args.append(start_url)
        logger.info("拉起浏览器: %s (profile=%s, 首屏=%s)", exe, profile, start_url[:60])
        # DETACHED_PROCESS:与父进程(console/stdio/生命周期)完全解耦,
        # 避免服务环境导致浏览器启动失败或随父进程连带退出
        self._proc = subprocess.Popen(
            args, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        self._owns_process = True
        # 等待调试端口就绪 + 首屏 target 出现;按首屏域名匹配
        # fast=True("打开页面"场景):只求 target 出现,大幅缩短冷启动耗时
        if start_url == "about:blank":
            frag = "about:blank"
        else:
            frag = urlparse(start_url).netloc or "zhipin.com"
        probe_gap = 0.25 if fast else 0.5
        for _ in range(120 if fast else 60):
            if await is_cdp_alive(config.CDP_PORT):
                break
            await asyncio.sleep(probe_gap)
        if fast:
            # 打开页面不需要 Playwright 级别的 target 稳定,见到域名即可
            await wait_page_target(config.CDP_PORT, frag, 8)
            await asyncio.sleep(0.8)
        else:
            await wait_page_target(config.CDP_PORT, frag, 20)
            await asyncio.sleep(2)
        if not await is_cdp_alive(config.CDP_PORT):
            raise TimeoutError("浏览器调试端口启动超时(30s)")

    async def _connect(self) -> None:
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(
                config.cdp_endpoint, timeout=15000
            )
        except Exception:
            await self._playwright.stop()
            self._playwright = None
            raise
        # 连接初期 CDP target 枚举/导航管线未就绪,直接 goto 会"假成功"(停留 about:blank)
        await asyncio.sleep(2.0)
        logger.info("已连接浏览器 CDP: %s", config.cdp_endpoint)

    def primary_context(self) -> BrowserContext:
        if not self._browser or not self._browser.contexts:
            raise RuntimeError("浏览器未连接")
        return self._browser.contexts[0]

    async def get_or_open_page(self, url_prefix: str, url: str | None = None) -> Page:
        """按 URL 前缀复用已有 Tab,否则新开 Tab。"""
        ctx = self.primary_context()
        for page in ctx.pages:
            if page.url.startswith(url_prefix):
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
                return page
        page = await ctx.new_page()
        await page.goto(url or url_prefix, wait_until="domcontentloaded")
        return page

    @staticmethod
    def _looks_like_verify(url: str, title: str) -> bool:
        """判断落点是否为平台安全验证页(Boss 验证页会引导用户滑块后自动回跳)。"""
        u = url or ""
        return (
            "security" in u
            or "_security_check" in u
            or "verify" in u
            or "安全验证" in (title or "")
            or "安全挑战" in (title or "")
        )

    @staticmethod
    def _switch_to_window_by_title(fragment: str) -> bool:
        """按标题片段把浏览器窗口切到前台(Windows;失败静默)。

        /json/activate 只能在 Chrome 窗口内部切换标签,无法把整个浏览器窗口
        从其它应用背后带到前台;这里用 user32.SwitchToThisWindow 完成置顶。
        """
        if os.name != "nt" or not fragment:
            return False
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            found: list[int] = []
            proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

            def _cb(hwnd, _lp):
                if user32.IsWindowVisible(hwnd):
                    buf = ctypes.create_unicode_buffer(512)
                    user32.GetWindowTextW(hwnd, buf, 512)
                    if buf.value and fragment in buf.value:
                        found.append(hwnd)
                        return False   # 已找到,停止枚举
                return True

            cb = proto(_cb)
            user32.EnumWindows(cb, 0)
            if found:
                user32.SwitchToThisWindow(wintypes.HWND(found[0]), True)
                return True
        except Exception:  # noqa: BLE001
            logger.debug("窗口置顶失败", exc_info=True)
        return False

    async def open_url_native(self, url: str, referrer: str | None = None) -> dict:
        """通过 CDP 原生 WebSocket 驱动新标签页导航(与 Playwright 会话完全解耦)。

        Playwright 的 CDP 会话有生命周期副作用(断开时会把它创建的标签页重置为
        about:blank,driver 退出甚至连带关闭浏览器);而原生 ws 客户端 detach 后
        页面照常存活 —— 这是"给用户看"的页面唯一稳定的打开方式。

        返回 {"url": 最终落点, "need_verify": 是否落在安全验证页}。
        落在验证页时不关闭标签:用户完成滑块后平台会按 callbackUrl 自动跳回岗位。
        """
        launched = await self.ensure_running(url, fast=True)
        base = f"http://127.0.0.1:{config.CDP_PORT}"
        base_url = url.split("?")[0]      # 岗位页基址(用于同页查重)
        tid: str | None = None
        ws_url: str | None = None
        need_navigate = True
        if launched:
            # 冷启动:Chrome 首屏就是目标页(原生导航,无 blank 阶段)。
            # fast 模式下 target 的 URL 可能尚未就位,轮询等待其出现
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                for _ in range(10):
                    lst = (await client.get(f"{base}/json/list")).json()
                    t = next(
                        (
                            x
                            for x in lst
                            if x.get("type") == "page"
                            and urlparse(url).netloc in (x.get("url") or "")
                        ),
                        None,
                    )
                    if t:
                        tid = t.get("id")
                        need_navigate = False   # Chrome 启动参数已带目标 URL
                        break
                    await asyncio.sleep(0.5)
        if tid is None:
            # 查重:同岗位标签已存在则直接聚焦,不重复开 tab
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                lst = (await client.get(f"{base}/json/list")).json()
            t = next(
                (
                    x
                    for x in lst
                    if x.get("type") == "page" and (x.get("url") or "").startswith(base_url)
                ),
                None,
            )
            if t and not launched:
                tid = t.get("id")
                need_navigate = False
        if tid is None:
            # 浏览器已在运行(或冷启动首屏未匹配):新开标签并用 CDP 导航
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                resp = await client.put(f"{base}/json/new")
                resp.raise_for_status()
                target = resp.json()
                tid = target.get("id")
                if not tid:
                    raise RuntimeError(f"浏览器未返回新标签信息: {target}")
                lst = (await client.get(f"{base}/json/list")).json()
                ws_url = next(
                    (x.get("webSocketDebuggerUrl") for x in lst if x.get("id") == tid), None
                )
        if need_navigate:
            if not ws_url:
                raise RuntimeError("未找到新标签页的调试地址")
            ws_url = ws_url.replace("localhost", "127.0.0.1")
            try:
                async with websockets.connect(
                    ws_url, max_size=8 * 1024 * 1024, open_timeout=10
                ) as ws:
                    await ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
                    params: dict = {"url": url}
                    if referrer:
                        # 带站内来路,更像真人点击,降低风控触发概率
                        params["referrer"] = referrer
                        params["transitionType"] = "link"
                    await ws.send(
                        json.dumps({"id": 2, "method": "Page.navigate", "params": params})
                    )
                    while True:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                        if msg.get("id") == 2:
                            if "error" in msg:
                                raise RuntimeError(f"导航失败: {msg['error'].get('message')}")
                            break
            except Exception:
                try:
                    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                        await client.get(f"{base}/json/close/{tid}")
                except Exception:  # noqa: BLE001
                    pass
                raise
        # 等页面/重定向链稳定后检查落点(验证页/正常详情页)
        await asyncio.sleep(1.5)
        final_url, title = url, ""
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                lst = (await client.get(f"{base}/json/list")).json()
                t = next((x for x in lst if x.get("id") == tid), {})
                final_url = t.get("url") or url
                title = t.get("title") or ""
        except Exception:  # noqa: BLE001
            pass
        # 把新标签带到前台(失败不影响打开)
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                await client.get(f"{base}/json/activate/{tid}")
        except Exception:  # noqa: BLE001
            pass
        # activate 只能切换 Chrome 窗口内的标签;把整个浏览器窗口置顶还需系统调用
        self._switch_to_window_by_title((title or "")[:24])
        # 收尾:清掉残留的空白标签(Chrome 启动初期偶发的多余 target),保持窗口整洁
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                lst = (await client.get(f"{base}/json/list")).json()
                for x in lst:
                    if (
                        x.get("type") == "page"
                        and (x.get("url") or "") == "about:blank"
                        and x.get("id") != tid
                    ):
                        await client.get(f"{base}/json/close/{x['id']}")
        except Exception:  # noqa: BLE001
            pass
        return {"url": final_url, "need_verify": self._looks_like_verify(final_url, title)}

    async def ensure_running(self, start_url: str | None = None, fast: bool = False) -> None:
        """仅确保浏览器进程在运行(不建立 Playwright 连接);冷启动以 start_url 为首屏。"""
        if not await is_cdp_alive(config.CDP_PORT):
            await self._launch(start_url, fast=fast)

    async def close_page_by_prefix(self, url_prefix: str) -> None:
        ctx = self.primary_context()
        for page in list(ctx.pages):
            if page.url.startswith(url_prefix):
                try:
                    await page.close()
                except Exception:
                    pass

    async def shutdown(self) -> None:
        """服务退出时断开 Playwright 连接。

        注意:不显式调用 browser.close()——实测 driver 退出本身可能连带关闭
        浏览器进程,显式 close 只会更早杀掉用户正在查看的页面窗口。
        浏览器进程与登录态窗口尽量留给用户(browser_profile 独立持久)。
        """
        self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None

    async def restart(self) -> None:
        """重启浏览器实例(自愈):Chrome 处于病态时(如全部 tab 拒绝导航),
        断开连接 → 杀掉占用本 profile 的浏览器进程 → 重新拉起并连接。

        登录态保存在 profile 目录中,重启不丢失。
        """
        await self.shutdown()
        profile = Path(config.BROWSER_PROFILE_DIR).resolve()
        frag = profile.name  # profile 目录名,用于精确匹配进程命令行

        def _kill() -> None:
            import subprocess

            ps = (
                "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or Name='msedge.exe'\" | "
                f"Where-Object {{ $_.CommandLine -like '*{frag}*' }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
            )
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps],
                    capture_output=True, timeout=30,
                )
            except Exception:  # noqa: BLE001
                pass

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _kill)
        # 等调试端口死透
        for _ in range(10):
            if not await is_cdp_alive(config.CDP_PORT):
                break
            await asyncio.sleep(1)
        await self.ensure()


browser_manager = BrowserManager()