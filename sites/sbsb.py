"""烧饼论坛（sb.sb）每日签到。

站点自建程序（Discourse 风格路由 /t/ /u/ /go/），签到页是 /signin/：
  GET  /signin/  读状态并取表单里的 _csrf
  POST /signin/  提交 _csrf=xxx 完成签到
页面上「已签到」的判定：按钮文案含「今日已签到」或存在 .signin-cal-day.done.today
奖励为随机 1~10 烧饼，按 UTC 日期计算，北京时间每天 08:00 刷新。

L1 走 curl_cffi；被 Cloudflare 拦或 cookie 失效时升级到浏览器通道（可配账号密码自动登录）。
"""
from __future__ import annotations

import re
from typing import Any, Dict

from bs4 import BeautifulSoup

from core.base import BaseSite, SigninResult
from core.browser import BrowserUnavailable
from core.config import Account
from core.http import is_cf_challenge

BASE = "https://sb.sb"
SIGNIN_URL = BASE + "/signin/"
LOGIN_URL = BASE + "/login/"
HOME_URL = BASE + "/"

# sb.sb 对部分地区直接封锁：所有路径都返回同一个占位页（伪装成 404），
# 里面带着这句 description。不识别的话只会看到一个莫名其妙的 404。
# 不同 CDN 节点返回的封锁文案略有差异，这里多收几条。
REGION_BLOCK_MARKERS = (
    "暂未对您所在地区开放",
    "未对您所在地区开放",
    "not available in your region",
    "地区未开放",
    "地区暂未开放",
    "所在地区暂未开放",
)

REWARD_PATTERNS = (
    r"获得\s*(\d+)\s*个?\s*烧饼",
    r"签到.*?(\d+)\s*烧饼",
    r"(\d+)\s*烧饼",
)
STREAK_RE = re.compile(r"连续签到\s*(\d+)\s*天")
BALANCE_PATTERNS = (r"财富[:：]?\s*([\d,\.]+[KM]?)", r"烧饼[:：]?\s*([\d,\.]+)")

LOGIN_FILL_JS = """
([user, pwd]) => {
  const pw = document.querySelector('input[type="password"]')
          || document.querySelector('input[name="password"]');
  const form = pw ? pw.closest('form') : document.querySelector('form');
  if (!form) return false;
  const set = (sel, val) => {
    const el = form.querySelector(sel);
    if (el) {
      el.value = val;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }
  };
  set('input[name="username"], input[name="email"], input[type="text"]', user);
  set('input[type="password"], input[name="password"]', pwd);
  return true;
}
"""


class SbSbSite(BaseSite):
    name = "sbsb"
    display = "烧饼论坛"

    def signin(self, account: Account) -> SigninResult:
        if str(self.cfg.strategy or "auto").lower() == "browser":
            return self._via_browser(account)
        try:
            return self._via_http(account)
        except _Escalate as e:
            self.log.info("烧饼论坛升级到浏览器通道：%s", e)
            return self._via_browser(account)

    # ---------------- L1 ----------------
    def _via_http(self, account: Account) -> SigninResult:
        http = self.ctx.http(account.cookie)
        r = http.get(SIGNIN_URL, headers={"Referer": HOME_URL})
        if is_blocked_region(r.text):
            return self.fatal_fail(account, "站点对当前出口 IP 所在地区不开放（" + BASE +
                                   " 地区封锁），需换到可访问的出口网络/代理")
        if is_cf_challenge(r):
            raise _Escalate(f"Cloudflare 挑战（HTTP {r.status_code}）")
        if r.status_code != 200:
            return self.result(account, "failed", f"HTTP {r.status_code}")

        state = parse_signin_state(r.text)
        if "/login" in (r.url or "") or state["login_required"]:
            if account.username and account.password:
                raise _Escalate("Cookie 失效，改用账号密码登录")
            return self.result(account, "failed", "Cookie 已失效，请重新抓取")
        if state["signed"]:
            return self.result(account, "already", "今日已签到", "", _meta(r.text))
        if not state["can_submit"] or not state["csrf"]:
            return self.result(account, "failed",
                               f"未找到可用签到入口（按钮文案：{state['label'] or '无'}）")

        r2 = http.post(
            SIGNIN_URL,
            data={"_csrf": state["csrf"]},
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": SIGNIN_URL,
            },
        )
        if is_cf_challenge(r2):
            raise _Escalate("签到请求被 Cloudflare 拦截")
        # 地区封锁时，POST 与确认 GET 都会落到同一个占位 404 页
        if is_blocked_region(r2.text):
            return self.fatal_fail(
                account,
                "sb.sb 对当前出口网络不可用（疑似地区封锁）。请在 config.yaml 配置 "
                "runtime.proxy 指向可用出口，或临时关闭该站点（sites.sbsb.enabled: false）。")

        payload: Any = None
        try:
            payload = r2.json()
        except Exception:
            payload = None
        if isinstance(payload, dict) and _truthy(payload.get("ok", payload.get("success"))):
            return self.result(account, "success", str(payload.get("message") or "")[:100],
                               _reward(str(payload)), _meta(r2.text))

        after = parse_signin_state(r2.text)
        if after["signed"] or "今日已签到" in (r2.text or ""):
            return self.result(account, "success", "签到成功", _reward(r2.text), _meta(r2.text))

        # POST 返回的可能是跳转页，再拉一次确认
        r3 = http.get(SIGNIN_URL, headers={"Referer": SIGNIN_URL})
        if is_blocked_region(r3.text):
            return self.fatal_fail(
                account,
                "sb.sb 对当前出口网络不可用（疑似地区封锁）。请在 config.yaml 配置 "
                "runtime.proxy 指向可用出口，或临时关闭该站点（sites.sbsb.enabled: false）。")
        final = parse_signin_state(r3.text)
        if final["signed"]:
            return self.result(account, "success", "签到成功", _reward(r3.text), _meta(r3.text))
        if final["login_required"]:
            return self.result(account, "failed", "登录态丢失，Cookie 已失效")
        return self.result(
            account, "failed",
            f"签到后未确认成功（HTTP {r2.status_code}）；"
            f"若当前出口网络被 sb.sb 地区封锁，请配置 runtime.proxy 或改用策略 "
            f"strategy: browser（在 config.yaml 的 sbsb 下加 strategy: browser）")

    # ---------------- L2/L3 ----------------
    def _via_browser(self, account: Account) -> SigninResult:
        try:
            browser = self.ctx.browser()
        except BrowserUnavailable as e:
            return self.fatal_fail(account, str(e))

        try:
            with browser as b:
                b.goto(HOME_URL, timeout=60000)
                if not b.wait_for_cf(self.ctx.runtime.cf_wait_timeout):
                    return self.result(account, "failed", "Cloudflare 挑战未通过，建议配置代理")

                if account.cookie:
                    b.add_cookies(account.cookie, BASE)
                    b.goto(SIGNIN_URL, timeout=60000)
                    b.wait_for_cf(self.ctx.runtime.cf_wait_timeout)
                    if "/login" not in b.url and not _looks_login(b.text()):
                        return self._checkin_in_browser(b, account)
                    if not (account.username and account.password):
                        return self.result(account, "failed", "Cookie 已失效，请重新抓取")
                    self.log.info("Cookie 失效，尝试账号密码登录")

                b.goto(LOGIN_URL, timeout=60000)
                b.wait_for_cf(self.ctx.runtime.cf_wait_timeout)
                try:
                    b.page.wait_for_selector('input[type="password"]', timeout=20000)
                    b.eval(LOGIN_FILL_JS, [account.username, account.password])
                    b.wait(1500)
                    b.eval("() => { const f = document.querySelector('form'); "
                           "f.requestSubmit ? f.requestSubmit() : f.submit(); }")
                    b.page.wait_for_function(
                        "() => !document.querySelector('input[type=\"password\"]')", timeout=30000)
                except Exception as e:
                    return self.result(account, "failed",
                                       f"自动登录失败：{type(e).__name__}: {e}"[:200])
                return self._checkin_in_browser(b, account)
        except BrowserUnavailable as e:
            return self.fatal_fail(account, str(e))
        except Exception as e:
            return self.result(account, "failed", f"浏览器通道异常：{type(e).__name__}: {e}"[:200])

    def _checkin_in_browser(self, b, account: Account) -> SigninResult:
        b.goto(SIGNIN_URL, timeout=60000)
        b.wait_for_cf(self.ctx.runtime.cf_wait_timeout)
        text = b.text()
        if "/login" in b.url or _looks_login(text):
            return self.result(account, "failed", "登录态未生效，被重定向到登录页")
        if "今日已签到" in text:
            return self.result(account, "already", "今日已签到", "", _meta(text))

        clicked = (b.click_selector(".signin-hero-action .btn-post")
                   or b.click_selector('form[action*="/signin"] button[type="submit"]')
                   or b.click_button(["签到"]))
        if not clicked:
            return self.result(account, "failed", "未找到签到按钮（页面结构可能已改版）")

        b.wait(3000)
        b.wait_for_cf(30)
        body = b.text()
        if "今日已签到" in body or "签到成功" in body:
            return self.result(account, "success", "签到成功", _reward(body), _meta(body))
        return self.result(account, "failed", "点击后未确认成功")


class _Escalate(Exception):
    """内部信号：升级到浏览器通道。"""


def is_blocked_region(html: str) -> bool:
    """sb.sb 的地区封锁页：所有路径都返回同一个占位页，不会写明确的 403。"""
    head = (html or "")[:4000]
    return any(m in head for m in REGION_BLOCK_MARKERS)


def parse_signin_state(html: str) -> Dict[str, Any]:
    """解析 /signin/ 页面：是否已签到、是否未登录、csrf、按钮文案。"""
    soup = BeautifulSoup(html or "", "html.parser")
    text = soup.get_text(" ", strip=True)
    button = (soup.select_one(".signin-hero-action .btn-post")
              or soup.select_one('form[action*="/signin"] button[type="submit"]')
              or soup.select_one('form[action*="/signin"] input[type="submit"]'))
    label = ""
    if button is not None:
        label = (button.get_text(strip=True) or button.get("value") or "").strip()
    csrf_el = soup.select_one('input[name="_csrf"]')
    csrf = (csrf_el.get("value") or "").strip() if csrf_el else ""
    signed = ("今日已签到" in label or "今日已签到" in text
              or soup.select_one(".signin-cal-day.done.today") is not None)
    login_required = (soup.select_one('input[name="username"]') is not None
                      or soup.select_one('input[type="password"]') is not None) \
        and "登录" in text and "每日签到" not in text
    can_submit = bool(button) and "签到" in label and "已签到" not in label
    return {"signed": signed, "login_required": login_required,
            "csrf": csrf, "can_submit": can_submit, "label": label}


def _looks_login(text: str) -> bool:
    head = (text or "")[:1500]
    return "登录" in head and "每日签到" not in head and "签到" not in head[:400]


def _truthy(v: Any) -> bool:
    return v in (1, True, "1", "true")


def _reward(text: str) -> str:
    for p in REWARD_PATTERNS:
        m = re.search(p, text or "")
        if m:
            return f"获得 {m.group(1)} 烧饼"
    return ""


def _meta(text: str) -> str:
    bits = []
    m = STREAK_RE.search(text or "")
    if m:
        bits.append(f"连续 {m.group(1)} 天")
    for pattern in BALANCE_PATTERNS:
        mm = re.search(pattern, text or "")
        if mm:
            bits.append(f"财富 {mm.group(1)}")
            break
    return " · ".join(bits)
