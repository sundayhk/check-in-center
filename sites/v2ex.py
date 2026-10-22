"""V2EX 每日登录奖励领取。

流程：GET /mission/daily 抓出 /mission/daily/redeem?once=NNNN 再 GET 一次。
V2EX 基本不在 Cloudflare 后面，L1 直连即可；Cookie 只需 A2 字段。
注意：北京时间 08:00 前通常无法领取。
"""
from __future__ import annotations

import re
import time

from core.base import BaseSite, SigninResult
from core.config import Account
from core.http import is_cf_challenge

BASE = "https://www.v2ex.com"
MISSION_URL = BASE + "/mission/daily"
REDEEM_RE = re.compile(r"/mission/daily/redeem\?once=(\d+)")
DAY_RE = (
    re.compile(r"已连续登录\s*(\d+)\s*天"),
    re.compile(r"连续登录\s*(\d+)\s*天"),
    re.compile(r"(\d+)\s*天"),
)
BALANCE_RE = re.compile(r"(\d[\d,]*)\s*铜币")


class V2exSite(BaseSite):
    name = "v2ex"
    display = "V2EX"

    def signin(self, account: Account) -> SigninResult:
        http = self.ctx.http(account.cookie)
        try:
            r = http.get(MISSION_URL, headers={"Referer": BASE})
        except Exception as e:
            return self.result(account, "failed", f"请求异常：{type(e).__name__}: {e}"[:200])

        if is_cf_challenge(r):
            return self.result(account, "failed", "被 Cloudflare 拦截，建议配置代理后重试")
        if r.status_code != 200:
            return self.result(account, "failed", f"HTTP {r.status_code}")
        if _not_logged_in(r):
            return self.result(account, "failed", "Cookie 已失效（A2），请重新抓取")

        balance = _balance(r.text)
        m = REDEEM_RE.search(r.text)
        if not m:
            if "已领取" in r.text or "每日登录奖励" in r.text:
                return self.result(account, "already", "今日已领取", "", balance)
            return self.result(account, "failed", "未找到领取链接（未到时间或页面改版）")

        try:
            http.get(BASE + m.group(0), headers={"Referer": MISSION_URL})
        except Exception as e:
            return self.result(account, "failed", f"领取请求异常：{type(e).__name__}"[:200])

        time.sleep(2)
        try:
            r2 = http.get(MISSION_URL, headers={"Referer": BASE})
        except Exception as e:
            return self.result(account, "failed", f"复核请求异常：{type(e).__name__}"[:200])

        if REDEEM_RE.search(r2.text) and "已领取" not in r2.text:
            return self.result(account, "failed", "领取后仍未生效，请手动检查")
        days = _days(r2.text)
        return self.result(
            account, "success",
            f"连续 {days} 天" if days else "领取成功",
            "已领取每日登录奖励",
            _balance(r2.text) or balance,
        )


def _not_logged_in(r) -> bool:
    if "/signin" in (r.url or ""):
        return True
    head = r.text[:3000]
    return ('name="password"' in head) or ("登录以继续" in head) or ("请先登录" in head)


def _balance(html: str) -> str:
    m = BALANCE_RE.search(html)
    return f"铜币 {m.group(1)}" if m else ""


def _days(html: str) -> str:
    for rx in DAY_RE:
        m = rx.search(html)
        if m:
            return m.group(1)
    return ""
