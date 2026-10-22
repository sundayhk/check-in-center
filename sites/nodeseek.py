"""NodeSeek 签到。

NodeSeek 常年挂在 Cloudflare 后面，且签到没有稳定的公开 JSON 接口（社区脚本普遍
走浏览器 UI）。因此：
  strategy=auto|api   先尝试 L1 接口调用，失败或被 CF 拦则自动升级
  strategy=browser    直接走 L2/L3 浏览器通道（推荐，最稳）

签到模式（account.options.mode 或 site.options.mode）：
  random  试试手气，1~10 鸡腿（默认）
  fixed    稳拿 5 鸡腿
"""
from __future__ import annotations

import re
from typing import Optional

from core.base import BaseSite, SigninResult
from core.browser import BrowserUnavailable
from core.config import Account
from core.http import is_cf_challenge, parse_cookie

BASE = "https://www.nodeseek.com"
BOARD_URL = BASE + "/board"
API_ATTENDANCE = BASE + "/api/attendance"

REWARD_PATTERNS = (
    r"获得\s*(\d+)\s*鸡腿",
    r"鸡腿\s*(\d+)\s*个",
    r"踩到鸡腿\s*(\d+)\s*个",
    r"得鸡腿\s*(\d+)\s*个",
    r"(\d+)\s*(?:个?\s*鸡腿|鸡腿)",
)
ALREADY_KEYS = ("已签到", "今日已签到", "今日签到获得", "当前排名", "签到成功", "本次获得", "排名")
BALANCE_PATTERNS = (r"鸡腿总数[:：]?\s*([\d,]+)", r"当前鸡腿[:：]?\s*([\d,]+)", r"总鸡腿[:：]?\s*([\d,]+)")

LOGIN_JS = """
() => {
  const has = (s) => !!document.querySelector(s);
  let auth = 0;
  document.querySelectorAll('a,button,span').forEach((e) => {
    const t = (e.textContent || '').trim();
    const h = e.getAttribute('href') || '';
    if (t === '登录' || h.includes('/signIn') || h.includes('/login')) auth += 1;
  });
  const avatar = has('.avatar') || has('.nsk-user-avatar') || has('.user-avatar')
                 || has('.user-info') || has('[class*="avatar"]');
  return { avatar, auth, url: location.href };
}
"""


class NodeSeekSite(BaseSite):
    name = "nodeseek"
    display = "NodeSeek"

    # ---------------- 主流程 ----------------
    def signin(self, account: Account) -> SigninResult:
        mode = str(account.opt("mode", self.opt("mode", "random"))).lower()
        random_mode = mode != "fixed"
        strategy = str(self.cfg.strategy or "auto").lower()

        if strategy in ("auto", "api"):
            r = self._try_api(account, random_mode)
            if r is not None:
                return r
            self.log.info("NodeSeek 接口通道不可用，升级到浏览器通道")
        return self._via_browser(account, random_mode)

    # ---------------- L1：接口尝试 ----------------
    def _try_api(self, account: Account, random_mode: bool) -> Optional[SigninResult]:
        http = self.ctx.http(account.cookie)
        try:
            resp = http.post(
                API_ATTENDANCE,
                params={"random": "true" if random_mode else "false"},
                headers={
                    "Referer": BOARD_URL,
                    "Origin": BASE,
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/plain, */*",
                },
            )
        except Exception as e:
            self.log.info("NodeSeek 接口请求异常：%s", e)
            return None
        if is_cf_challenge(resp):
            return None
        try:
            payload = resp.json()
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        msg = str(payload.get("message") or data.get("message") or "")
        ok = payload.get("success", payload.get("ok"))
        reward = data.get("reward") or data.get("amount") or payload.get("reward") or ""
        balance = str(data.get("balance") or data.get("total") or "")

        if ok is True or str(ok).lower() == "true":
            status = "already" if any(k in msg for k in ("已签到", "重复", "already")) else "success"
            return self.result(account, status, msg[:120],
                               f"获得 {reward} 鸡腿" if reward else "",
                               f"总鸡腿 {balance}" if balance else "")
        if ok is False:
            if any(k in msg for k in ("已签到", "重复", "already")):
                return self.result(account, "already", msg[:120] or "今日已签到")
            if any(k in msg for k in ("登录", "未登录", "unauthorized", "signIn")):
                return self.result(account, "failed", "Cookie 已失效，请重新抓取")
            return self.result(account, "failed", msg[:120] or f"接口返回失败（HTTP {resp.status_code}）")
        return None

    # ---------------- L2/L3：浏览器通道 ----------------
    def _via_browser(self, account: Account, random_mode: bool) -> SigninResult:
        try:
            browser = self.ctx.browser()
        except BrowserUnavailable as e:
            return self.result(account, "failed", str(e))

        try:
            with browser as b:
                b.goto(BASE, timeout=60000)
                b.add_cookies(account.cookie, BASE)
                b.goto(BOARD_URL, timeout=60000)
                if not b.wait_for_cf(self.ctx.runtime.cf_wait_timeout):
                    return self.result(account, "failed", "Cloudflare 挑战未通过，建议配置代理")

                st = b.eval(LOGIN_JS) or {}
                if "/signIn" in str(st.get("url", "")) or (not st.get("avatar") and st.get("auth", 0) > 0):
                    return self.result(account, "failed", "Cookie 已失效，请重新抓取（需含 session 字段）")

                texts = ["手气", "鸡腿", "x 5", "签到"] if random_mode else ["鸡腿", "x 5", "手气", "签到"]
                clicked = b.click_button(texts, container=".board-intro") or b.click_button(texts)
                if not clicked:
                    body = b.text()
                    if any(k in body for k in ALREADY_KEYS):
                        return self.result(account, "already", "今日已签到", balance=_balance(body))
                    return self.result(account, "failed", "未找到签到按钮（页面结构可能已改版）")

                b.wait(3000)
                b.wait_for_cf(30)
                body = b.text()
                reward = _reward(body)
                already = any(k in body for k in ("今日已签到", "已签到", "当前排名")) and not reward
                status = "already" if already else "success"
                return self.result(account, status,
                                   "" if reward else "已提交签到",
                                   f"获得 {reward} 鸡腿" if reward else "",
                                   _balance(body))
        except BrowserUnavailable as e:
            return self.result(account, "failed", str(e))
        except Exception as e:
            return self.result(account, "failed", f"浏览器通道异常：{type(e).__name__}: {e}"[:200])


def _reward(text: str) -> str:
    for p in REWARD_PATTERNS:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return ""


def _balance(text: str) -> str:
    for p in BALANCE_PATTERNS:
        m = re.search(p, text)
        if m:
            return f"总鸡腿 {m.group(1)}"
    return ""


__all__ = ["NodeSeekSite", "SigninResult", "parse_cookie"]
