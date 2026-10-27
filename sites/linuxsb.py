"""烧饼社区（linux.sb）每日签到。

接口：GET /daily_checkin 读状态并取 _csrf -> POST /daily_checkin（_csrf=xxx）
登录态 cookie：bbs_auth；CSRF：表单 _csrf 或 cookie bbs_csrf
该站 2026-08 起间歇性开启 Cloudflare 托管挑战：
  L1 被拦（403 + cf-mitigated: challenge）-> L2 浏览器注入 cookie 后页面内 fetch 签到
  cookie 失效 -> 若配置了账号密码，浏览器自动登录（算术验证码 + PoW）后签到
"""
from __future__ import annotations

import re
import time
from typing import Optional, Tuple

from core.base import BaseSite, SigninResult
from core.browser import BrowserUnavailable
from core.config import Account
from core.http import is_cf_challenge, parse_cookie

BASE = "https://linux.sb"
CHECKIN_URL = BASE + "/daily_checkin"
LOGIN_URL = BASE + "/login"

CSRF_RE = re.compile(r'name="_csrf"\s+value="([^"]+)"')
CHECKED_IN_TEXT = "今日已签到"
LOGIN_MARKERS = ('name="password"', "user_review_login_email")
BONUS_KEYS = ("bonus", "points", "gain", "reward", "score", "earned")
META_PATTERNS = (
    # 页面头部 user-rank 里的「积分 N」（2026-10 页面实际展示位置）
    (r'user-rank">\s*积分\s*([\d,]+)', "积分 {}"),
    (r"当前积分[:：]?\s*([\d,]+)", "当前积分 {}"),
    (r"连续签到\s*[:：]?\s*(\d+)\s*天", "连续 {} 天"),
    (r'<strong>(\d+)</strong>\s*<span>\s*连续天数', "连续 {} 天"),  # HTML 版
    (r'(\d+)\s*\n?\s*连续天数', "连续 {} 天"),  # 浏览器纯文本版
    (r"积分\s*([\d,]+)", "积分 {}"),  # 兜底：纯文本页头
)

CHECKIN_JS = """
async (csrf) => {
  const r = await fetch('/daily_checkin', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      'X-Requested-With': 'XMLHttpRequest'
    },
    body: '_csrf=' + encodeURIComponent(csrf)
  });
  return await r.json();
}
"""

CAPTCHA_RE = re.compile(r"(\d+)\s*([+\-*x×÷/])\s*(\d+)")

LOGIN_FILL_JS = """
([user, pwd, ans]) => {
  const form = document.querySelector('input[name="password"]')?.closest('form')
             || document.querySelector('form');
  if (!form) return '';
  const set = (name, val) => {
    const el = form.querySelector(`[name="${name}"]`);
    if (el) {
      el.value = val;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }
  };
  set('username', user);
  set('password', pwd);
  set('native_captcha_answer', ans);
  const csrfEl = form.querySelector('[name="_csrf"]');
  return csrfEl ? csrfEl.value : '';
}
"""


class LinuxSbSite(BaseSite):
    name = "linuxsb"
    display = "烧饼社区"

    def signin(self, account: Account) -> SigninResult:
        try:
            return self._via_http(account)
        except _Escalate as e:
            self.log.info("烧饼社区升级到浏览器通道：%s", e)
            return self._via_browser(account)

    # ---------------- L1 ----------------
    def _via_http(self, account: Account) -> SigninResult:
        http = self.ctx.http(account.cookie)
        r = http.get(CHECKIN_URL, headers={"Referer": BASE})
        if is_cf_challenge(r):
            raise _Escalate(f"Cloudflare 挑战（HTTP {r.status_code}）")
        if r.status_code != 200:
            return self.result(account, "failed", f"HTTP {r.status_code}")
        if "/login" in (r.url or "") or any(m in r.text for m in LOGIN_MARKERS):
            if account.username and account.password:
                raise _Escalate("Cookie 失效，改用账号密码登录")
            return self.result(account, "failed", "Cookie 已失效（bbs_auth），请重新抓取")

        if CHECKED_IN_TEXT in r.text:
            return self.result(account, "already", "今日已签到", "", _meta(r.text))

        csrf = _csrf_from_html(r.text) or http.cookies.get("bbs_csrf", "")
        if not csrf:
            return self.result(account, "failed", "未取到 _csrf，页面结构可能已改版")

        r2 = http.post(
            CHECKIN_URL,
            data={"_csrf": csrf},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": CHECKIN_URL,
            },
        )
        if is_cf_challenge(r2):
            raise _Escalate("签到请求被 Cloudflare 拦截")
        try:
            payload = r2.json()
        except Exception:
            return self.result(account, "failed", f"返回非 JSON（HTTP {r2.status_code}）")

        if _truthy(payload.get("ok")):
            bonus = _bonus(payload)
            http.update_cookies(_cookie_dict(r2))
            return self.result(account, "success", str(payload.get("message") or "")[:100],
                               f"获得 {bonus} 积分" if bonus else "签到成功")
        msg = str(payload.get("message") or "")
        if any(k in msg for k in ("已签到", "已打卡", "重复签到")):
            return self.result(account, "already", msg[:100] or "今日已签到")
        return self.result(account, "failed", msg[:120] or "签到失败")

    # ---------------- L2/L3 ----------------
    def _via_browser(self, account: Account) -> SigninResult:
        try:
            browser = self.ctx.browser()
        except BrowserUnavailable as e:
            return self.fatal_fail(account, str(e))

        try:
            with browser as b:
                b.goto(BASE, timeout=60000)
                if not b.wait_for_cf(self.ctx.runtime.cf_wait_timeout):
                    return self.result(account, "failed", "Cloudflare 挑战未通过，建议配置代理")

                if account.cookie:
                    b.add_cookies(account.cookie, BASE)
                    b.goto(CHECKIN_URL, timeout=60000)
                    b.wait_for_cf(self.ctx.runtime.cf_wait_timeout)
                    if "/login" not in b.url:
                        return self._checkin_in_browser(b, account, parse_cookie(account.cookie).get("bbs_csrf", ""))
                    if not (account.username and account.password):
                        return self.result(account, "failed", "Cookie 已失效（bbs_auth），请重新抓取")
                    self.log.info("Cookie 失效，尝试账号密码登录")

                return self._login_and_checkin(b, account)
        except BrowserUnavailable as e:
            return self.fatal_fail(account, str(e))
        except Exception as e:
            return self.result(account, "failed", f"浏览器通道异常：{type(e).__name__}: {e}"[:200])

    def _login_and_checkin(self, b, account: Account) -> SigninResult:
        b.goto(LOGIN_URL, timeout=60000)
        b.wait_for_cf(self.ctx.runtime.cf_wait_timeout)
        try:
            b.page.wait_for_selector('input[name="username"]', timeout=20000)
            question = ""
            try:
                question = b.page.inner_text(".native-captcha-question")
            except Exception:
                pass
            answer = _solve_math(question)
            login_csrf = b.eval(LOGIN_FILL_JS, [account.username, account.password, answer])
            time.sleep(8)  # 模拟真人输入节奏，提交过快会被拒
            b.eval("() => { const f = document.querySelector('form'); f.requestSubmit ? f.requestSubmit() : f.submit(); }")
            b.page.wait_for_function("() => !document.querySelector('input[name=\"password\"]')", timeout=30000)
            if "user_review_login_email" in b.url:
                return self.result(account, "failed", "触发邮箱验证风控，请本地浏览器完成验证后更新 Cookie")
        except Exception as e:
            return self.result(account, "failed", f"自动登录失败：{type(e).__name__}: {e}"[:200])
        return self._checkin_in_browser(b, account, str(login_csrf or ""))

    def _checkin_in_browser(self, b, account: Account, fallback_csrf: str) -> SigninResult:
        b.goto(CHECKIN_URL, timeout=60000)
        b.wait_for_cf(self.ctx.runtime.cf_wait_timeout)
        if "/login" in b.url:
            return self.result(account, "failed", "登录态未生效，签到页被重定向到登录页")
        if CHECKED_IN_TEXT in b.content:
            return self.result(account, "already", "今日已签到", "", _meta(b.text()))

        csrf = ""
        try:
            for c in b.context.cookies():
                if c.get("name") == "bbs_csrf" and c.get("value") not in ("deleted", "archived"):
                    csrf = c.get("value", "")
        except Exception:
            csrf = ""
        csrf = csrf or fallback_csrf
        if not csrf:
            return self.result(account, "failed", "未取到 bbs_csrf，无法提交签到")

        try:
            payload = b.eval(CHECKIN_JS, csrf)
        except Exception as e:
            return self.result(account, "failed", f"页面内签到异常：{type(e).__name__}"[:200])

        if isinstance(payload, dict) and _truthy(payload.get("ok")):
            bonus = _bonus(payload)
            b.wait(1500)
            return self.result(account, "success", str(payload.get("message") or "")[:100],
                               f"获得 {bonus} 积分" if bonus else "签到成功", _meta(b.text()))
        msg = str((payload or {}).get("message") or "")
        if any(k in msg for k in ("已签到", "已打卡", "重复签到")):
            return self.result(account, "already", msg[:100] or "今日已签到")
        return self.result(account, "failed", msg[:120] or "签到失败")


class _Escalate(Exception):
    """内部信号：请求层被拦，升级到浏览器通道。"""


def _csrf_from_html(html: str) -> str:
    m = CSRF_RE.search(html or "")
    return m.group(1) if m else ""


def _cookie_dict(resp) -> dict:
    """从响应头里取服务端轮换下发的新 cookie。"""
    out = {}
    raw = ""
    for k, v in (getattr(resp, "headers", {}) or {}).items():
        if str(k).lower() == "set-cookie":
            raw = v
            break
    for part in re.split(r",(?=\s*[^;]+=)", raw):
        m = re.match(r"\s*([^=;]+)=([^;]*)", part)
        if m:
            out[m.group(1).strip()] = m.group(2).strip()
    return out


def _truthy(v) -> bool:
    return v in (1, True, "1", "true") or v == 1


def _bonus(payload: dict) -> str:
    for k in BONUS_KEYS:
        if payload.get(k) not in (None, ""):
            return str(payload[k])
    return ""


def _meta(html: str) -> str:
    bits = []
    for pattern, tpl in META_PATTERNS:
        m = re.search(pattern, html or "")
        if m:
            bit = tpl.format(m.group(1))
            if bit not in bits:
                bits.append(bit)
    return " · ".join(bits)


def _solve_math(question: str) -> str:
    """解析登录页算术验证码，如「9 × 4 = ?」"""
    m = CAPTCHA_RE.search(question or "")
    if not m:
        return ""
    a, op, bb = int(m.group(1)), m.group(2), int(m.group(3))
    try:
        if op in "+":
            return str(a + bb)
        if op in "-":
            return str(a - bb)
        if op in ("*", "x", "×"):
            return str(a * bb)
        if op in ("/", "÷"):
            return str(a // bb) if bb and a % bb == 0 else ""
    except Exception:
        return ""
    return ""
