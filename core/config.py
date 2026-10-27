"""配置加载：YAML 优先，缺文件时自动回退到纯环境变量模式（GitHub Actions 友好）。

配置值支持 ${ENV_VAR} 与 ${ENV_VAR:-默认值} 插值，方便把密钥留在环境变量里。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    """递归展开字符串中的 ${VAR} / ${VAR:-default}。"""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            val = os.environ.get(name, "")
            if not val and default is not None:
                val = default
            return val
        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def split_cookie(raw: str, separator: str = "|") -> List[str]:
    """把多账号 cookie 串切成列表，并清洗掉误粘贴的前缀/引号。"""
    if not raw:
        return []
    for junk in ("cookie:", "Cookie:", "-H", "--cookie"):
        raw = raw.replace(junk, "")
    parts = re.split(r"[|&\n]+" if separator in ("|", "&") else re.escape(separator), raw)
    out = []
    for p in parts:
        p = p.strip().strip('"').strip("'").replace("\ufeff", "").replace("\u200b", "")
        if p and "=" in p:
            out.append(p)
    return out


@dataclass
class Account:
    name: str = "default"
    cookie: str = ""
    username: str = ""
    password: str = ""
    options: Dict[str, Any] = field(default_factory=dict)

    def opt(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)


@dataclass
class SiteCfg:
    key: str
    enabled: bool = True
    strategy: str = "auto"
    cookie_separator: str = "|"
    options: Dict[str, Any] = field(default_factory=dict)
    accounts: List[Account] = field(default_factory=list)

    def opt(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)


@dataclass
class RuntimeCfg:
    timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"
    log_dir: str = "logs"
    state_db: str = "data/state.db"
    headless: bool = True
    browser_backend: str = "auto"      # auto | camoufox | playwright | none
    camoufox_exclude_addons: bool = True   # 不加载 uBlock Origin，避免联网下载拖慢/拖垮启动
    proxy: str = ""
    timeout: int = 30
    cf_wait_timeout: int = 90
    retries: int = 1
    site_gap: List[int] = field(default_factory=lambda: [30, 120])
    account_gap: List[int] = field(default_factory=lambda: [10, 60])


@dataclass
class NotifyCfg:
    telegram: Dict[str, Any] = field(default_factory=dict)
    wecom: Dict[str, Any] = field(default_factory=dict)
    webhook: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    runtime: RuntimeCfg
    notify: NotifyCfg
    sites: Dict[str, SiteCfg]


def _build(data: Dict[str, Any]) -> Config:
    data = expand_env(data or {})

    rt = dict(data.get("runtime") or {})
    gaps = rt.get("site_gap") or [30, 120]
    agaps = rt.get("account_gap") or [10, 60]
    runtime = RuntimeCfg(
        timezone=str(rt.get("timezone", "Asia/Shanghai")),
        log_level=str(rt.get("log_level", "INFO")),
        log_dir=str(rt.get("log_dir", "logs")),
        state_db=str(rt.get("state_db", "data/state.db")),
        headless=bool(rt.get("headless", True)),
        browser_backend=str(rt.get("browser_backend", "auto")),
        camoufox_exclude_addons=(
            str(rt.get("camoufox_exclude_addons",
                       os.environ.get("CAMOUFOX_EXCLUDE_ADDONS", "true"))).lower() != "false"),
        proxy=str(rt.get("proxy") or os.environ.get("PROXY") or os.environ.get("HTTPS_PROXY")
                  or os.environ.get("https_proxy") or ""),
        timeout=int(rt.get("timeout", 30)),
        cf_wait_timeout=int(rt.get("cf_wait_timeout", 90)),
        retries=int(rt.get("retries", 1)),
        site_gap=[int(gaps[0]), int(gaps[1])],
        account_gap=[int(agaps[0]), int(agaps[1])],
    )

    notify = NotifyCfg(
        telegram=dict(data.get("notify", {}).get("telegram") or {}),
        wecom=dict(data.get("notify", {}).get("wecom") or {}),
        webhook=dict(data.get("notify", {}).get("webhook") or {}),
    )

    sites: Dict[str, SiteCfg] = {}
    for key, raw in (data.get("sites") or {}).items():
        raw = raw or {}
        accounts: List[Account] = []
        for idx, acc in enumerate(raw.get("accounts") or []):
            accounts.append(Account(
                name=str(acc.get("name") or f"账号{idx + 1}"),
                cookie=str(acc.get("cookie") or ""),
                username=str(acc.get("username") or ""),
                password=str(acc.get("password") or ""),
                options=dict(acc.get("options") or {}),
            ))
        sites[key] = SiteCfg(
            key=key,
            enabled=bool(raw.get("enabled", True)),
            strategy=str(raw.get("strategy", "auto")),
            cookie_separator=str(raw.get("cookie_separator", "|")),
            options=dict(raw.get("options") or {}),
            accounts=accounts,
        )
        # 支持「一条环境变量塞多个账号」的写法：cookie 里用分隔符隔开
        raw_cookie = str(raw.get("cookie") or "")
        if not accounts and raw_cookie:
            for idx, ck in enumerate(split_cookie(raw_cookie, sites[key].cookie_separator)):
                sites[key].accounts.append(Account(name=f"账号{idx + 1}", cookie=ck))
        # 账号密码兜底：LINUXSB_ACCOUNT='[{"username":"a","password":"b"}]'
        raw_json = str(raw.get("account_json") or "")
        if raw_json:
            try:
                items = json.loads(raw_json)
            except Exception:
                items = []
            if isinstance(items, dict):
                items = [items]
            for idx, item in enumerate(items or []):
                if not isinstance(item, dict):
                    continue
                if idx < len(sites[key].accounts):
                    acc = sites[key].accounts[idx]
                    acc.username = str(item.get("username") or acc.username)
                    acc.password = str(item.get("password") or acc.password)
                else:
                    sites[key].accounts.append(Account(
                        name=str(item.get("name") or f"账号{idx + 1}"),
                        username=str(item.get("username") or ""),
                        password=str(item.get("password") or ""),
                    ))
    return _apply_site_enabled_env(Config(runtime=runtime, notify=notify, sites=sites))


_SITE_ENV_PREFIX = {
    "nodeseek": "NS",
    "v2ex": "V2EX",
    "linuxsb": "LINUXSB",
    "sbsb": "SBSB",
}


def _apply_site_enabled_env(cfg: "Config") -> "Config":
    """允许用环境变量覆盖站点启用状态（Web 控制台的开关写 .env 用）。"""
    for key, site in cfg.sites.items():
        prefix = _SITE_ENV_PREFIX.get(key)
        if not prefix:
            continue
        raw = os.environ.get(f"{prefix}_ENABLED")
        if raw is not None and raw != "":
            site.enabled = str(raw).strip().lower() not in ("0", "false", "no", "off")
    return cfg


def load_config(path: Optional[str] = None) -> Config:
    """读取配置；找不到 YAML 时回退到环境变量模式。

    会先加载项目根目录的 .env（不覆盖已有的真实环境变量），
    这样本地直跑 / VPS cron 也能用 .env，而不必只在 Docker 里生效。
    """
    load_dotenv()
    candidates = [Path(path)] if path else [ROOT / "config.yaml", ROOT / "config.yml"]
    for c in candidates:
        if c and c.exists():
            if yaml is None:
                raise RuntimeError("需要 PyYAML 才能读取 YAML 配置：pip install PyYAML")
            with c.open("r", encoding="utf-8") as f:
                return _build(yaml.safe_load(f) or {})
    return _build(_env_template())


def load_dotenv(path: Optional[Path] = None, override: bool = False) -> Path:
    """加载项目根目录的 .env 到 os.environ（真实环境变量优先）。

    不引入 python-dotenv，手写一个够用的解析器：
    支持注释、`export` 前缀、单双引号包裹、行尾行内注释。
    返回实际加载的文件路径，没找到返回 None。
    """
    p = Path(path) if path else ROOT / ".env"
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8-sig")
    except Exception:
        return None

    inline_comment = re.compile(r"\s+#.*$")
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        if not key:
            continue
        val = val.strip()
        # 只有成对且完整包裹的引号才去掉，避免误删值里的引号
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        else:
            val = inline_comment.sub("", val).strip()
        if override or key not in os.environ:
            os.environ[key] = val
    return p


def _env_template() -> Dict[str, Any]:
    """纯环境变量模式：不用配置文件也能跑（GitHub Actions / 容器 env 直接注入）。"""
    return {
        "runtime": {
            "headless": os.environ.get("HEADLESS", "true").lower() != "false",
            "browser_backend": os.environ.get("BROWSER_BACKEND", "auto"),
            "proxy": os.environ.get("PROXY", ""),
            "retries": os.environ.get("RETRIES", "1"),
            "log_level": os.environ.get("LOG_LEVEL", "INFO"),
        },
        "notify": {
            "telegram": {
                "enabled": bool(os.environ.get("TG_BOT_TOKEN") and os.environ.get("TG_CHAT_ID")),
                "bot_token": "${TG_BOT_TOKEN}",
                "chat_id": "${TG_CHAT_ID}",
                "thread_id": "${TG_THREAD_ID}",
            },
            "wecom": {"enabled": bool(os.environ.get("WECOM_WEBHOOK")), "webhook": "${WECOM_WEBHOOK}"},
        },
        "sites": {
            "nodeseek": {
                "enabled": bool(os.environ.get("NS_COOKIE")),
                "strategy": os.environ.get("NS_STRATEGY", "auto"),
                "cookie": "${NS_COOKIE}",
                "options": {"mode": os.environ.get("NS_MODE", "random")},
            },
            "v2ex": {
                "enabled": bool(os.environ.get("V2EX_COOKIE")),
                "cookie": "${V2EX_COOKIE}",
            },
            "linuxsb": {
                "enabled": bool(os.environ.get("LINUXSB_COOKIE") or os.environ.get("LINUXSB_ACCOUNT")),
                "cookie_separator": "&",
                "cookie": "${LINUXSB_COOKIE}",
                "account_json": "${LINUXSB_ACCOUNT}",
            },
            "sbsb": {
                "enabled": bool(os.environ.get("SBSB_COOKIE")),
                "cookie_separator": "|",
                "cookie": "${SBSB_COOKIE}",
            },
        },
    }
