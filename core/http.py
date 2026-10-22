"""L1 请求层：curl_cffi 指纹伪装优先，requests 兜底。

Cloudflare 的很多拦截发生在 TLS 指纹层，用 curl_cffi  impersonate 真实 Chrome
的 JA3/HTTP2 指纹，绝大多数场景无需启动浏览器即可通过。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

try:
    from curl_cffi import requests as _cffi
except Exception:  # pragma: no cover
    _cffi = None
try:
    import requests as _requests
except Exception:  # pragma: no cover
    _requests = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")

CF_BODY_MARKERS = (
    "just a moment",
    "attention required",
    "challenges.cloudflare.com",
    "cf-browser-verification",
    "verifies you are not a bot",
    "checking your browser",
)


def parse_cookie(raw: str) -> Dict[str, str]:
    """把浏览器复制出来的 cookie 串解析成 dict，容忍各种误粘贴。"""
    if not raw:
        return {}
    s = raw.strip()
    s = re.sub(r"^\s*cookie\s*:\s*", "", s, flags=re.I)
    s = s.strip().strip('"').strip("'")
    s = s.replace("\ufeff", "").replace("\u200b", "")
    out: Dict[str, str] = {}
    for part in re.split(r"[;\n]+", s):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip().lstrip("﻿")
        if k:
            out[k] = v.strip().strip('"')
    return out


def is_cf_challenge(resp: "HttpResponse") -> bool:
    """判断是否被 Cloudflare 拦下：看 cf-mitigated 响应头 + 页面特征。"""
    headers = {str(k).lower(): str(v) for k, v in (resp.headers or {}).items()}
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if str(resp.status_code) in ("403", "503", "429"):
        return True
    head = (resp.text or "")[:4000].lower()
    return any(m in head for m in CF_BODY_MARKERS)


@dataclass
class HttpResponse:
    status_code: int
    text: str
    headers: Dict[str, str] = field(default_factory=dict)
    url: str = ""

    def json(self) -> Any:
        return json.loads(self.text)


class HttpSession:
    """带指纹伪装与 cookie 管理的轻量请求会话。"""

    def __init__(self, cookie: str = "", proxy: str = "", ua: str = UA,
                 timeout: int = 30, impersonate: str = "chrome"):
        if _cffi is None and _requests is None:
            raise RuntimeError("需要安装 curl_cffi 或 requests：pip install curl_cffi requests")
        self.cookies = parse_cookie(cookie)
        self.timeout = timeout
        self.proxy = proxy or ""
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.base_headers = {
            "User-Agent": ua,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if _cffi is not None:
            self._s = _cffi.Session(impersonate=impersonate)
            self.kind = "curl_cffi"
        else:
            self._s = _requests.Session()
            self.kind = "requests"
            self._s.headers.update({"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})

    def request(self, method: str, url: str, headers: Optional[Dict[str, str]] = None,
                data: Any = None, json_body: Any = None, params: Optional[Dict[str, Any]] = None,
                allow_redirects: bool = True, timeout: Optional[int] = None) -> HttpResponse:
        h = dict(self.base_headers)
        h.update(headers or {})
        kw: Dict[str, Any] = dict(
            headers=h, cookies=self.cookies, params=params,
            allow_redirects=allow_redirects, timeout=timeout or self.timeout,
        )
        if json_body is not None:
            kw["json"] = json_body
        if data is not None:
            kw["data"] = data
        if self.proxies:
            kw["proxies"] = self.proxies
        resp = self._s.request(method.upper(), url, **kw)
        return HttpResponse(
            status_code=int(resp.status_code),
            text=resp.text or "",
            headers={str(k): str(v) for k, v in dict(resp.headers).items()},
            url=str(getattr(resp, "url", url)),
        )

    def get(self, url: str, **kw) -> HttpResponse:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> HttpResponse:
        return self.request("POST", url, **kw)

    def update_cookies(self, pairs: Dict[str, str]) -> None:
        """合并服务端下发的新 cookie（烧饼会话会轮换）。"""
        for k, v in (pairs or {}).items():
            if v and v not in ("deleted", "archived"):
                self.cookies[k] = v
            elif k in self.cookies:
                self.cookies.pop(k)

    def close(self) -> None:
        try:
            self._s.close()
        except Exception:
            pass
