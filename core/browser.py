"""L2/L3 浏览器层：Camoufox（反检测 Firefox）优先，Playwright Chromium 兜底。

浏览器层只在 L1（curl_cffi）被 Cloudflare 拦下时才启用：
- L2：真实浏览器指纹直接过 CF 的 JS/托管挑战
- L3：自动点击 Turnstile 小部件并等待 cf_clearance
"""
from __future__ import annotations

import os
import platform
import subprocess
import time
from importlib import util as _importlib_util
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


def camoufox_kwargs(headless: bool, proxy: str = "", exclude_addons: bool = True) -> Dict[str, Any]:
    """组装 Camoufox 启动参数，按版本能力降级兼容。"""
    kw: Dict[str, Any] = {"headless": headless}
    if proxy:
        kw["proxy"] = {"server": proxy}
    if exclude_addons:
        try:
            from camoufox.addons import DefaultAddons  # type: ignore
            kw["exclude_addons"] = [DefaultAddons.UBO]
        except Exception:
            pass  # 老版本没有该参数，忽略即可
    return kw


class BrowserUnavailable(RuntimeError):
    pass


def _has(module: str) -> bool:
    try:
        return _importlib_util.find_spec(module) is not None
    except Exception:
        return False


CAMOUFOX_BIN_NAMES = ("camoufox", "camoufox-bin", "camoufox.exe")


def _camoufox_binary_ready() -> Optional[bool]:
    """camoufox 浏览器内核是否已下载。True=OK，False=缺内核，None=判断不了。

    注意：pkgman.launch_path() 在内核缺失时会去 GitHub 查版本号，
    断网环境下抛 HTTPError 而非 CamoufoxNotInstalled，所以先用本地目录判定。
    """
    if not _has("camoufox"):
        return None
    try:
        from camoufox import pkgman  # type: ignore
    except Exception:
        return None

    inst = Path(str(getattr(pkgman, "INSTALL_DIR", "") or ""))
    if inst and inst.exists():
        for p in inst.rglob("*"):
            if p.is_file() and p.name in CAMOUFOX_BIN_NAMES:
                return True
    # 本地目录没有，再让 camoufox 自己判一次（兼容 install 到自定义路径的情况）
    try:
        p = pkgman.launch_path()
        return bool(p) and Path(str(p)).exists()
    except Exception as e:
        if type(e).__name__ == "CamoufoxNotInstalled":
            return False
        # 其余异常（无网络 / GitHub 404）多半也是因为本地压根没装
        return False if (inst and not inst.exists()) else None


def _camoufox_binary_path() -> Optional[Path]:
    """定位 camoufox 浏览器内核可执行文件，找不到返回 None。"""
    if not _has("camoufox"):
        return None
    try:
        from camoufox import pkgman  # type: ignore
    except Exception:
        return None
    inst = Path(str(getattr(pkgman, "INSTALL_DIR", "") or ""))
    if inst and inst.exists():
        for p in inst.rglob("*"):
            if p.is_file() and p.name in CAMOUFOX_BIN_NAMES:
                return p
    try:
        p = pkgman.launch_path()
        if p and Path(str(p)).exists():
            return Path(str(p))
    except Exception:
        pass
    return None


def _ldd_missing(bin_path: Path) -> List[str]:
    """列出动态链接里 'not found' 的共享库，用于诊断内核启动失败。"""
    try:
        out = subprocess.run(["ldd", str(bin_path)], capture_output=True,
                             text=True, timeout=15)
    except Exception:
        return []
    missing: List[str] = []
    for line in out.stdout.splitlines():
        if "not found" in line:
            # 形如 "libnss3.so => not found"
            missing.append(line.strip())
    return missing


def _playwright_chromium_ready() -> bool:
    """playwright 的 chromium 是否已 install。"""
    if not _has("playwright"):
        return False
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(Path.home() / ".cache" / "ms-playwright")
    try:
        return any(Path(root).glob("chromium-*"))
    except Exception:
        return False


def diagnose(backend: str = "auto") -> Tuple[str, str, List[str]]:
    """体检浏览器通道。

    返回 (实际后端或 None, 结论文案, 修复建议列表)。
    """
    fixes: List[str] = []
    if backend in ("auto", ""):
        backend = "camoufox" if _has("camoufox") else ("playwright" if _has("playwright") else "")

    if backend == "none":
        return "none", "已通过 browser_backend=none 显式禁用浏览器通道", []

    if backend == "camoufox":
        ready = _camoufox_binary_ready()
        if not _has("camoufox"):
            return "", "未安装 camoufox（Python 包）", ["pip install -U camoufox", "python -m camoufox fetch"]
        if ready is False:
            fixes.append("python -m camoufox fetch          # 下载 Camoufox 浏览器内核")
            return "", "camoufox 已安装，但浏览器内核未下载", fixes
        return "camoufox", "camoufox 可用", []

    if backend == "playwright":
        if not _has("playwright"):
            return "", "未安装 playwright（Python 包）", ["pip install playwright", "playwright install chromium"]
        if not _playwright_chromium_ready():
            fixes.append("playwright install chromium       # 下载 Chromium 内核")
            return "", "playwright 已安装，但 chromium 内核未下载", fixes
        return "playwright", "playwright 可用", []

    return "", "未找到任何浏览器后端", [
        "pip install -U camoufox && python -m camoufox fetch      # 推荐，反检测更强",
        "或：pip install playwright && playwright install chromium",
    ]


class BrowserSession:
    """Playwright 风格 API 的浏览器会话（Camoufox 与 Playwright 通用）。"""

    def __init__(self, backend: str = "auto", proxy: str = "", headless: bool = True,
                 cf_timeout: int = 90, logger: Optional[Any] = None,
                 exclude_addons: bool = True):
        self.backend = backend
        self.proxy = proxy
        self.headless = headless
        self.cf_timeout = cf_timeout
        self.log = logger
        # Camoufox 默认会联网拉 uBlock Origin；对签到无用，且 addons.mozilla.org
        # 在国内经常 451/超时导致启动失败，默认排除。
        self.exclude_addons = exclude_addons
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
        requested = self.backend
        actual, verdict, fixes = diagnose(requested)
        if not actual:
            msg = f"浏览器通道不可用：{verdict}"
            if fixes:
                msg += "。修复：" + "；".join(fixes)
            raise BrowserUnavailable(msg)
        backend = actual

        if backend == "camoufox":
            try:
                self._start_camoufox()
            except BrowserUnavailable as e:
                # auto 模式下若 camoufox 不可用（如 ARM 架构缺 x86 内核 / 缺系统库），
                # 自动降级到 playwright（其 chromium 原生支持 aarch64）。
                if requested in ("auto", "camoufox") and self._playwright_usable():
                    self.log.warning("camoufox 启动失败，自动降级到 playwright：%s", e)
                    backend = "playwright"
                else:
                    raise
        if backend == "playwright":
            self._start_playwright()

        if self.context is None:
            self.context = self.page.context
        self._real_backend = backend
        if self.log:
            self.log.info("浏览器通道启动：backend=%s headless=%s", backend, self.headless)
        return self

    def _start_camoufox(self) -> None:
        from camoufox.sync_api import Camoufox
        self._cm = Camoufox(**camoufox_kwargs(headless=self.headless, proxy=self.proxy,
                                             exclude_addons=self.exclude_addons))
        try:
            self._browser = self._cm.__enter__()
            self.page = self._browser.new_page()
        except Exception as e:
            bp = _camoufox_binary_path()
            if bp and _ldd_missing(bp):
                miss = _ldd_missing(bp)
                hint = (f"camoufox 启动失败：缺少系统运行库（{len(miss)} 个）。\n"
                        f"在容器内执行 `ldd {bp}` 查看缺失项并安装对应依赖，\n"
                        f"或改用 playwright：browser_backend 设为 playwright，"
                        f"再 `playwright install chromium`。\n缺失样例：{miss[:4]}")
            elif platform.machine().lower() in ("aarch64", "arm64"):
                hint = (f"camoufox 启动失败：当前为 ARM 架构（{platform.machine()}），"
                        f"camoufox 官方内核仅 x86_64。请改用 playwright：\n"
                        f"  pip install playwright && playwright install chromium\n"
                        f"再把 config 的 runtime.browser_backend 设为 playwright。")
            else:
                hint = f"camoufox 启动失败：{type(e).__name__}: {str(e)[:160]}"
            raise BrowserUnavailable(hint) from e

    def _start_playwright(self) -> None:
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(
                headless=self.headless,
                proxy={"server": self.proxy} if self.proxy else None,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            raise BrowserUnavailable(
                f"playwright 启动失败：{type(e).__name__}: {str(e)[:160]}"
                f"（可尝试 `playwright install chromium` 与安装系统依赖）"
            ) from e
        self.context = self._browser.new_context(
            locale="zh-CN",
            viewport={"width": 1440, "height": 900},
            user_agent=UA,
        )
        self.page = self.context.new_page()

    def _playwright_usable(self) -> bool:
        return _has("playwright") and _playwright_chromium_ready()

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

    def click_selector(self, selector: str, timeout: int = 8000) -> bool:
        """按 CSS 选择器点击（签到页按钮常有固定的 class）。"""
        try:
            loc = self.page.locator(selector).first
            if loc.count() > 0:
                loc.click(timeout=timeout)
                return True
        except Exception:
            pass
        return False

    def screenshot(self, path: str) -> None:
        try:
            self.page.screenshot(path=path, full_page=False)
        except Exception:
            pass
