"""环境体检：依赖、浏览器通道、配置、通知、网络连通性。

用法：python main.py --doctor [--probe]
"""
from __future__ import annotations

import sys
from importlib import util as _importlib_util
from typing import Any, List, Tuple

from .browser import diagnose
from .http import HttpSession, is_cf_challenge

# (import 名, pip 名, 用途)
DEPS: List[Tuple[str, str, str]] = [
    ("yaml", "PyYAML", "读取 config.yaml"),
    ("requests", "requests", "HTTP 兜底请求"),
    ("curl_cffi", "curl_cffi", "TLS 指纹伪装（L1 过 Cloudflare）"),
    ("bs4", "beautifulsoup4", "HTML 解析"),
]

# 各站点 cookie 里必须有关键字段，缺了基本等于没登录
REQUIRED_COOKIE = {
    "nodeseek": "session",
    "v2ex": "A2",
    "linuxsb": "bbs_auth",
}


def _mark(ok: bool) -> str:
    return "[OK]  " if ok else "[FAIL]"


def _line(ok: bool, label: str, detail: str = "") -> str:
    base = f"  {_mark(ok)} {label}"
    return f"{base}  {detail}" if detail else base


def check_deps() -> List[str]:
    lines = ["== 依赖 =="]
    for mod, pip, why in DEPS:
        try:
            ok = _importlib_util.find_spec(mod) is not None
        except Exception:
            ok = False
        lines.append(_line(ok, f"{pip:<16}", "" if ok else f"缺失 → pip install {pip}（{why}）"))
    return lines


def check_browser(backend: str) -> Tuple[List[str], bool]:
    """返回 (报告行, 浏览器通道是否可用)。"""
    lines = ["", "== 浏览器通道（NodeSeek 必需） =="]
    actual, verdict, fixes = diagnose(backend)
    lines.append(_line(bool(actual), f"backend={backend or 'auto'}", verdict))
    for f in fixes:
        lines.append(f"         → {f}")
    if not actual:
        lines.append("         NodeSeek 只能走浏览器通道，此项不可用会导致 NodeSeek 签到失败。")
    return lines, bool(actual)


def check_config(cfg: Any, cfg_path: str) -> List[str]:
    lines = ["", "== 配置 =="]
    lines.append(f"  [INFO] 配置来源：{cfg_path}")
    rt = cfg.runtime
    lines.append(f"  [INFO] 代理={rt.proxy or '无'}  超时={rt.timeout}s  重试={rt.retries}  "
                 f"无头={rt.headless}  CF等待={rt.cf_wait_timeout}s")
    if not cfg.sites:
        lines.append("  [FAIL] 没有解析到任何站点配置")
        return lines
    for key, s in cfg.sites.items():
        n = len([a for a in s.accounts if a.cookie or (a.username and a.password)])
        if not s.enabled:
            lines.append(_line(True, f"{key:<10}", "已禁用"))
        elif n == 0:
            lines.append(_line(False, f"{key:<10}", "已启用但无可用账号（缺 cookie / 账号密码）"))
        else:
            detail = f"{n} 个账号"
            # 关键字段检查比长度更准：NodeSeek 必须含 session，V2EX 只要 A2
            need = REQUIRED_COOKIE.get(key)
            if need:
                bad = [a.name for a in s.accounts
                       if a.cookie and need.lower() not in a.cookie.lower()]
                if bad:
                    lines.append(_line(False, f"{key:<10}",
                                       f"{detail}，但「{'、'.join(bad)}」的 cookie 缺少关键字段 "
                                       f"{need}（需从 F12 Network 请求头整段复制）"))
                    continue
            shortest = min((len(a.cookie) for a in s.accounts if a.cookie), default=0)
            if 0 < shortest < 20:
                detail += f"，注意：有账号 cookie 仅 {shortest} 字符，可能没复制完整"
            lines.append(_line(True, f"{key:<10}", detail))
    return lines


def check_notify(cfg: Any) -> List[str]:
    lines = ["", "== 通知 =="]
    any_ok = False
    for channel in ("telegram", "wecom", "webhook"):
        c = getattr(cfg.notify, channel, {}) or {}
        wanted = bool(c.get("enabled"))
        filled = any(bool(v) for v in c.values() if isinstance(v, str) and v)
        if wanted and filled:
            any_ok = True
            lines.append(_line(True, f"{channel:<10}", "已启用"))
        elif wanted and not filled:
            lines.append(_line(False, f"{channel:<10}", "已标记 enabled 但缺少令牌/地址"))
        else:
            lines.append(f"  [--]  {channel:<10} 未配置（可选）")
    if not any_ok:
        lines.append("         → 未配置任何通知渠道，签到结果只会打印到终端（--test-notify 可验证）")
    return lines


def check_network(cfg: Any, timeout: int = 15) -> List[str]:
    lines = ["", "== 网络探测 =="]
    try:
        import logging

        from core.base import Context
        from sites import build_sites

        ctx = Context(runtime=cfg.runtime, logger=logging.getLogger("signin-hub"))
        sites = build_sites(cfg, ctx)
    except Exception as e:
        lines.append(f"  [FAIL] 构建站点失败：{type(e).__name__}: {e}")
        return lines

    for key, site in sites.items():
        base = sys.modules.get(site.__class__.__module__)
        url = getattr(base, "BASE", "") if base else ""
        if not url:
            lines.append(_line(False, f"{key:<10}", "未定义 BASE，跳过"))
            continue
        try:
            resp = HttpSession(proxy=cfg.runtime.proxy, timeout=timeout).get(url)
        except Exception as e:
            reason = str(e)
            hint = ""
            if "tunnel" in reason.lower() or "proxy" in reason.lower():
                hint = "（代理不可用？检查 runtime.proxy）"
            lines.append(_line(False, f"{key:<10}", f"请求异常：{reason[:80]}{hint}"))
            continue
        verdict = f"HTTP {resp.status_code}"
        if is_cf_challenge(resp):
            verdict += " · 被 Cloudflare 拦（需要浏览器通道或代理）"
            lines.append(_line(False, f"{key:<10}", verdict))
        else:
            lines.append(_line(resp.status_code < 400, f"{key:<10}", verdict))
    return lines


def run(cfg: Any, cfg_path: str, probe: bool = False) -> List[str]:
    lines = [f"signin-hub 环境体检 · Python {sys.version.split()[0]}", ""]
    lines += check_deps()
    bl, browser_ok = check_browser(cfg.runtime.browser_backend)
    lines += bl
    lines += check_config(cfg, cfg_path)
    lines += check_notify(cfg)
    if probe:
        lines += check_network(cfg)

    lines.append("")
    if browser_ok:
        lines.append("结论：可以直接运行 python main.py")
    else:
        lines.append("结论：浏览器通道不可用，NodeSeek 会失败；v2ex / linuxsb / sbsb 不受影响。")
    return lines
