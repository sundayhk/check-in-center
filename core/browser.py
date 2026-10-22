"""L2/L3 浏览器层：Camoufox（反检测 Firefox）优先，Playwright Chromium 兜底。

浏览器层只在 L1（curl_cffi）被 Cloudflare 拦下时才启用：
- L2：真实浏览器指纹直接过 CF 的 JS/托管挑战
- L3：自动点击 Turnstile 小部件并等待 cf_clearance
"""
from __future__ import annotations

import time
from importlib import util as _importlib_util
from typing import Any, Dict, List, Optional

from .http import UA, parse_cookie

CF_TITLE_MARKERS = ("just a moment", "attention required", "checking your browser")
CF_BODY_MARKERS = (
    "just a moment",
    "challenges.cloudflare.com",
    "cf-browser-verification",
    "verifies you are not a bot",
    "performing security verification",
)

# 与出口 IP / UA 绑定的 CF 票据，注入旧值反而更容易被判异常
SKIP_COOKIE_PREFIXES = ("cf_clearance", "__cf_bm", "__cflb", "_ga", "_gid", "_gat")


class BrowserUnavailable(RuntimeError):
    pass


def _has(module: str) -> bool:
    try:
        return _importlib_util.find_spec(module) is not None
    except Exception:
        return False


class BrowserSession:
    """Playwright 风格 API 的浏览器会话（Camoufox 与 Playwright 通用）。"""

    def __init__(self, backend: str = "auto", proxy: str = "", headless: bool = True,
                 cf_timeout: int = 90, logger: Optional[Any] = None):
        self.backend = backend
        self.proxy = proxy
        self.headless = headless
        self.cf_timeout = cf_timeout
        self.log = logger
        self.page: Any = None
        self.context: Any = None
        self._browser: Any = None
        self._cm: Any = None
        self._pw: Any = None

    # ---- 生命周期 ----
    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> "BrowserSession":
        backend = self.backend
        if backend in ("auto", ""):
            backend = "camoufox" if _has("camoufox") else ("playwright" if _has("playwright") else "")
        if backend == "none":
            raise BrowserUnavailable("browser_backend=none，已禁用浏览器通道")
        if backend == "camoufox":
            from camoufox.sync_api import Camoufox
            kw: Dict[str, Any] = {"headless": self.headless}
            if self.proxy:
                kw["proxy"] = {"server": self.proxy}
            self._cm = Camoufox(**kw)
            self._browser = self._cm.__enter__()
            self.page = self._browser.new_page()
        elif backend == "playwright":
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=self.headless,
                proxy={"server": self.proxy} if self.proxy else None,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            self.context = self._browser.new_context(
                locale="zh-CN",
                viewport={"width": 1440, "height": 900},
                user_agent=UA,
            )
            self.page = self.context.new_page()
        else:
            raise BrowserUnavailable("未找到可用的浏览器后端，请安装 camoufox 或 playwright")
        if self.context is None:
            self.context = self.page.context
        self._real_backend = backend
        if self.log:
            self.log.info("浏览器通道启动：backend=%s headless=%s", backend, self.headless)
        return self

    def close(self) -> None:
        for closer in (
            lambda: self._browser.close() if self._browser else None,
            lambda: self._cm.__exit__(None, None, None) if self._cm else None,
            lambda: self._pw.stop() if self._pw else None,
        ):
            try:
                closer()
            except Exception:
                pass

    # ---- 基础操作 ----
    def goto(self, url: str, timeout: int = 60000, wait_until: str = "domcontentloaded") -> str:
        self.page.goto(url, timeout=timeout, wait_until=wait_until)
        return self.content

    @property
    def content(self) -> str:
        try:
            return self.page.content() or ""
        except Exception:
            return ""

    @property
    def title(self) -> str:
        try:
            return self.page.title() or ""
        except Exception:
            return ""

    @property
    def url(self) -> str:
        try:
            return self.page.url or ""
        except Exception:
            return ""

    def text(self, selector: str = "body") -> str:
        try:
            return self.page.inner_text(selector) or ""
        except Exception:
            return ""

    def eval(self, script: str, arg: Any = None) -> Any:
        return self.page.evaluate(script, arg)

    def wait(self, ms: int = 2000) -> None:
        self.page.wait_for_timeout(ms)

    def add_cookies(self, cookie_str: str, site_url: str) -> None:
        """注入 cookie；用 url 让浏览器自动推导 domain，避免显式 domain 报错。"""
        cookies = []
        for name, value in parse_cookie(cookie_str).items():
            if name.lower().startswith(SKIP_COOKIE_PREFIXES):
                continue
            cookies.append({"name": name, "value": value, "url": site_url})
        if cookies:
            self.context.add_cookies(cookies)

    # ---- Cloudflare ----
    def is_cf_page(self) -> bool:
        t = self.title.lower()
        if any(m in t for m in CF_TITLE_MARKERS):
            return True
        try:
            body = self.text()[:4000].lower()
        except Exception:
            body = ""
        return any(m in body for m in CF_BODY_MARKERS)

    def try_click_turnstile(self) -> bool:
        """尝试点击 Turnstile 复选框（L3）。"""
        try:
            frames = self.page.frames
        except Exception:
            return False
        for frame in frames:
            if "challenges.cloudflare.com" not in (frame.url or ""):
                continue
            for sel in ("input[type=checkbox]", "label.cb-lb", "body"):
                try:
                    frame.locator(sel).first.click(timeout=2500)
                    return True
                except Exception:
                    continue
        return False

    def wait_for_cf(self, timeout: Optional[int] = None) -> bool:
        """等待 Cloudflare 挑战自动通过；期间周期性尝试点击 Turnstile。"""
        deadline = time.time() + (timeout or self.cf_timeout)
        clicked_once = False
        while time.time() < deadline:
            if not self.is_cf_page():
                return True
            if not clicked_once:
                clicked_once = self.try_click_turnstile()
            time.sleep(3)
        return not self.is_cf_page()

    # ---- 交互 ----
    def click_button(self, texts: List[str], container: Optional[str] = None,
                     timeout: int = 8000) -> bool:
        """按文本关键词点击按钮；container 为限定容器选择器。"""
        base = f"{container} " if container else ""
        for t in texts:
            xpath = f"xpath={base}//button[contains(normalize-space(.), '{t}')]"
            try:
                loc = self.page.locator(xpath).first
                if loc.count() > 0:
                    loc.click(timeout=timeout)
                    return True
            except Exception:
                continue
        return False

    def screenshot(self, path: str) -> None:
        try:
            self.page.screenshot(path=path, full_page=False)
        except Exception:
            pass
