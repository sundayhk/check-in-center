"""调度器：串行执行 + 随机延迟 + 失败重试 + 当日去重 + 汇总。"""
from __future__ import annotations

import random
import time
import traceback
from datetime import datetime
from typing import Dict, List, Optional

from .base import BaseSite, SigninResult
from .config import Config
from .state import State


class Runner:
    def __init__(self, cfg: Config, state: State, logger):
        self.cfg = cfg
        self.state = state
        self.log = logger

    def _sleep_gap(self, gap: List[int], what: str, skip: bool = False) -> None:
        lo, hi = int(gap[0]), int(gap[-1])
        if hi <= 0 or lo < 0 or skip:
            return
        sec = random.randint(lo, max(lo, hi))
        self.log.info("%s：随机等待 %s 秒", what, sec)
        time.sleep(sec)

    def _attempt(self, site: BaseSite, account) -> SigninResult:
        retries = max(0, int(self.cfg.runtime.retries))
        last: Optional[SigninResult] = None
        for i in range(retries + 1):
            try:
                last = site.signin(account)
            except Exception as e:
                self.log.error("%s/%s 异常: %s\n%s", site.display, account.name, e,
                               traceback.format_exc(limit=3))
                last = site.result(account, "failed", f"异常：{type(e).__name__}: {e}"[:200])
            if last.ok:
                return last
            if getattr(last, "fatal", False):
                self.log.error("%s/%s 环境级失败，跳过重试", site.display, account.name)
                return last
            if i < retries:
                wait = 20 * (i + 1) + random.randint(5, 25)
                self.log.info("%s/%s 第 %s 次失败，%s 秒后重试", site.display, account.name, i + 1, wait)
                time.sleep(wait)
        return last  # type: ignore[return-value]

    def run(self, sites: Dict[str, BaseSite], force: bool = False,
            dry_run: bool = False) -> List[SigninResult]:
        results: List[SigninResult] = []
        rt = self.cfg.runtime
        for idx, (key, site) in enumerate(sites.items()):
            if idx:
                self._sleep_gap(rt.site_gap, f"{site.display} 站前间隔", skip=dry_run)
            for ai, account in enumerate(site.cfg.accounts):
                if ai:
                    self._sleep_gap(rt.account_gap, f"{site.display} 账号间隔", skip=dry_run)
                if not account.cookie and not (account.username and account.password):
                    results.append(site.result(account, "skipped", "未配置 cookie 或账号密码"))
                    continue
                if not force and self.state.done_today(key, account.name):
                    self.log.info("%s/%s 今日已完成，跳过", site.display, account.name)
                    results.append(site.result(account, "skipped", "今日已完成"))
                    continue
                if dry_run:
                    results.append(site.result(account, "skipped", "dry-run 未实际执行"))
                    continue

                self.log.info("开始签到：%s / %s", site.display, account.name)
                r = self._attempt(site, account)
                self.state.record(r)
                self.log.info("结果：%s / %s -> %s %s", site.display, account.name, r.status, r.message)
                results.append(r)
        return results


def format_summary(results: List[SigninResult]) -> str:
    now = datetime.now().strftime("%m-%d %H:%M")
    head = f"signin-hub 签到汇总 · {now}"
    lines = [head, "─" * 24]
    for r in results:
        lines.append(r.line())
    ok = sum(1 for r in results if r.status == "success")
    already = sum(1 for r in results if r.status == "already")
    fail = sum(1 for r in results if r.status == "failed")
    lines.append("─" * 24)
    lines.append(f"成功 {ok} · 已签 {already} · 失败 {fail}")
    return "\n".join(lines)
