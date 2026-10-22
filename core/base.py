"""站点适配器的公共抽象：统一入参、统一返回。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .config import Account, RuntimeCfg, SiteCfg


@dataclass
class SigninResult:
    site: str
    display: str
    account: str
    status: str                 # success | already | skipped | failed
    message: str = ""
    reward: str = ""
    balance: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("success", "already", "skipped")

    @property
    def marker(self) -> str:
        return {
            "success": "[OK]  ",
            "already": "[OK]  ",
            "skipped": "[SKIP]",
            "failed": "[FAIL]",
        }.get(self.status, "[??]  ")

    def line(self) -> str:
        bits = [f"{self.marker} {self.display} / {self.account}"]
        detail = " · ".join(x for x in (self.reward, self.balance, self.message) if x)
        if detail:
            bits.append("  " + detail)
        return "".join(bits)


@dataclass
class Context:
    """注入给每个站点的运行上下文。"""
    runtime: RuntimeCfg
    logger: Any

    def http(self, cookie: str = "", proxy: Optional[str] = None) -> Any:
        from .http import HttpSession
        return HttpSession(
            cookie=cookie,
            proxy=proxy if proxy is not None else self.runtime.proxy,
            timeout=self.runtime.timeout,
        )

    def browser(self, **kw: Any) -> Any:
        from .browser import BrowserSession
        opts = dict(
            backend=self.runtime.browser_backend,
            proxy=self.runtime.proxy,
            headless=self.runtime.headless,
            cf_timeout=self.runtime.cf_wait_timeout,
            logger=self.logger,
        )
        opts.update(kw)
        return BrowserSession(**opts)


class BaseSite(ABC):
    """所有站点的基类：实现一个 signin(account) 即可接入。"""

    name: str = ""
    display: str = ""

    def __init__(self, ctx: Context, cfg: SiteCfg):
        self.ctx = ctx
        self.cfg = cfg
        self.log = ctx.logger

    @abstractmethod
    def signin(self, account: Account) -> SigninResult:
        ...

    # ---- 便利方法 ----
    def opt(self, key: str, default: Any = None) -> Any:
        return self.cfg.opt(key, default)

    def result(self, account: Account, status: str, message: str = "",
               reward: str = "", balance: str = "", **extra: Any) -> SigninResult:
        return SigninResult(
            site=self.name, display=self.display, account=account.name,
            status=status, message=message, reward=reward, balance=balance, extra=extra,
        )
