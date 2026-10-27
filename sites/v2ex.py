"""V2EX 每日登录奖励领取。

流程：GET /mission/daily 抓出 /mission/daily/redeem?once=NNNN 再 GET 一次。
V2EX 基本不在 Cloudflare 后面，L1 直连即可；Cookie 只需 A2 字段。
注意：北京时间 08:00 前通常无法领取。
"""
from __future__ import annotations

import re
import time
from typing import Tuple

from core.base import BaseSite, SigninResult
from core.config import Account
from core.http import is_cf_challenge, parse_cookie

BASE = "https://www.v2ex.com"
MISSION_URL = BASE + "/mission/daily"
BALANCE_URL = BASE + "/balance"
REDEEM_RE = re.compile(r"/mission/daily/redeem\?once=(\d+)")
# /balance 流水页最新一行：td(positive strong 今日所得) + td(变动后余额)
LEDGER_RE = re.compile(
    r'<strong>([\d,\.]+)</strong></span></td>\s*<td class="d" style="text-align: right;">([\d,\.]+)</td>')
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
            # 2026-10 起 V2EX 仅凭 A2 无法维持登录态，需要 PB3_SESSION 等会话字段
            if "PB3_SESSION" not in parse_cookie(account.cookie):
                return self.result(
                    account, "failed",
                    "Cookie 缺少 PB3_SESSION 字段（现在仅 A2 不够），请从浏览器重新复制完整 Cookie"
                    "（至少含 A2、PB3_SESSION、A2O、V2EX_TAB）")
            return self.result(account, "failed", "Cookie 已失效（A2/PB3_SESSION），请重新抓取")

        balance = _balance(r.text)
        m = REDEEM_RE.search(r.text)
        if not m:
            if "已领取" in r.text or "每日登录奖励" in r.text:
                # 北京时间 08:00 前当天奖励未刷新，页面显示的「已领取」是昨天的状态；
                # 此时不能记「今日已完成」，否则去重库会把今天真正的领取窗口跳过。
                if _before_daily_reset():
                    return self.result(
                        account, "skipped",
                        "未到领取时间（北京时间 08:00 前奖励未刷新，页面仍是昨日状态），稍后再签")
                reward, coins = _ledger(account, http)
                return self.result(account, "already", "今日已领取", reward, coins or balance)
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
        reward, coins = _ledger(account, http)
        if not reward:
            reward = "已领取每日登录奖励"
        return self.result(
            account, "success",
            f"连续 {days} 天" if days else "领取成功",
            reward,
            coins or _balance(r2.text) or balance,
        )


def _ledger(account, http) -> Tuple[str, str]:
    """从 /balance 流水最新一行取（今日所得, 变动后余额）；失败返回空对。"""
    try:
        r = http.get(BALANCE_URL, headers={"Referer": BASE})
        if r.status_code != 200:
            return "", ""
        m = LEDGER_RE.search(r.text)
        if not m:
            return "", ""
        reward = f"今日 +{m.group(1).rstrip('0').rstrip('.')} 铜币"
        coins = f"余额 {m.group(2).rstrip('0').rstrip('.')}"
        return reward, coins
    except Exception:
        return "", ""


def _not_logged_in(r) -> bool:
    if "/signin" in (r.url or ""):
        return True
    head = r.text[:3000]
    return ('name="password"' in head) or ("登录以继续" in head) or ("请先登录" in head)


def _before_daily_reset() -> bool:
    """北京时间 08:00 前当天奖励未刷新（不管容器/机器时区是什么）。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:
        now = datetime.now()
    return now.hour < 8


def _balance(html: str) -> str:
    m = BALANCE_RE.search(html)
    return f"铜币 {m.group(1)}" if m else ""


def _days(html: str) -> str:
    for rx in DAY_RE:
        m = rx.search(html)
        if m:
            return m.group(1)
    return ""
