"""Web UI：Cookie 导入 / 签到记录 / 积分概览 / 手动签到。

用 FastAPI 提供一个轻量控制台（单页应用，前端在 web/index.html）：
  GET  /                    控制台页面
  GET  /api/overview        今日各站状态 + 最新积分 + 连续天数
  GET  /api/records         历史签到记录（含 reward / balance）
  GET  /api/cookies         各站 Cookie（脱敏展示）
  POST /api/cookies/{site}  导入 Cookie / 账号密码（写回 .env 并热更新内存配置）
  POST /api/run             手动触发签到（后台线程执行）
  GET  /api/run/status      查询执行状态与汇总

安全：设置 WEB_PASSWORD 后所有接口需登录（HMAC 会话 Cookie）；
不设密码时建议只在内网使用。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from .config import ROOT, Config

try:
    from fastapi import Request  # noqa: F401  模块级导入，供运行时类型解析
except Exception:
    Request = None

# 各站点对应的 .env 变量名（Cookie 导入写回用）
SITE_ENV = {
    "nodeseek": "NS_COOKIE",
    "v2ex": "V2EX_COOKIE",
    "linuxsb": "LINUXSB_COOKIE",
    "sbsb": "SBSB_COOKIE",
}
SITE_LABEL = {
    "nodeseek": "NodeSeek",
    "v2ex": "V2EX",
    "linuxsb": "烧饼社区",
    "sbsb": "烧饼论坛",
}

STATUS_LABEL = {"success": "成功", "already": "已签", "failed": "失败", "skip": "跳过"}


def _mask(secret: str, keep_head: int = 14, keep_tail: int = 6) -> str:
    s = (secret or "").strip()
    if not s:
        return ""
    if len(s) <= keep_head + keep_tail + 4:
        return s[:4] + "…" + s[-2:]
    return f"{s[:keep_head]}…{s[-keep_tail:]}（长度 {len(s)}）"


def _session_token(password: str) -> str:
    return hmac.new(password.encode(), b"signin-hub-web-session", hashlib.sha256).hexdigest()


class WebApp:
    """FastAPI 应用包装：持有配置/状态库引用，Cookie 导入可热更新。"""

    def __init__(self, cfg: Config, log, config_path: Optional[str] = None):
        self.cfg = cfg
        self.log = log
        self.config_path = config_path
        self.password = os.environ.get("WEB_PASSWORD", "")
        self.job: Dict[str, Any] = {"running": False, "started": "", "finished": "",
                                    "ok": None, "summary": ""}
        self._lock = threading.Lock()
        try:
            from fastapi import FastAPI
            from fastapi.responses import FileResponse, JSONResponse  # noqa: F401
        except Exception as e:
            raise RuntimeError("Web UI 需要 fastapi / uvicorn：pip install fastapi uvicorn") from e

        app = FastAPI(title="signin-hub", docs_url=None, redoc_url=None, openapi_url=None)
        self._app = app
        index_path = ROOT / "web" / "index.html"

        def _authed(req: Request) -> bool:
            if not self.password:
                return True
            return req.cookies.get("sph_session", "") == _session_token(self.password)

        # ---------- 页面 ----------
        @app.get("/")
        def index():
            return FileResponse(index_path)

        # ---------- 登录 ----------
        @app.post("/api/login")
        async def login(request: Request):
            body = await request.json()
            if not self.password:
                return {"ok": True, "msg": "未设置 WEB_PASSWORD，无需登录"}
            if str(body.get("password") or "") == self.password:
                resp = JSONResponse({"ok": True})
                resp.set_cookie("sph_session", _session_token(self.password),
                                max_age=30 * 86400, httponly=True, samesite="lax")
                return resp
            return JSONResponse({"ok": False, "msg": "密码错误"}, status_code=401)

        @app.get("/api/auth")
        def auth(request: Request):
            return {"ok": _authed(request), "required": bool(self.password)}

        # ---------- 概览 ----------
        @app.get("/api/overview")
        def overview(request: Request):
            if not _authed(request):
                return JSONResponse({"ok": False, "msg": "未登录"}, status_code=401)
            return {"ok": True, "sites": self._overview()}

        # ---------- 记录 ----------
        @app.get("/api/records")
        def records(request: Request, days: int = 30, site: str = ""):
            if not _authed(request):
                return JSONResponse({"ok": False, "msg": "未登录"}, status_code=401)
            return {"ok": True, "records": self._records(days=max(1, min(days, 365)), site=site)}

        # ---------- Cookie ----------
        @app.get("/api/cookies")
        def cookies(request: Request):
            if not _authed(request):
                return JSONResponse({"ok": False, "msg": "未登录"}, status_code=401)
            return {"ok": True, "sites": self._cookie_view()}

        @app.post("/api/cookies/{site}")
        async def save_cookies(site: str, request: Request):
            if not _authed(request):
                return JSONResponse({"ok": False, "msg": "未登录"}, status_code=401)
            if site not in SITE_ENV:
                return JSONResponse({"ok": False, "msg": f"未知站点 {site}"}, status_code=404)
            body = await request.json()
            cookie = str(body.get("cookie") or "").strip()
            username = str(body.get("username") or "").strip()
            password = str(body.get("password") or "").strip()
            if not cookie and not (username and password):
                return JSONResponse({"ok": False, "msg": "Cookie 与账号密码至少填一项"}, status_code=400)
            self._write_env(site, cookie, username, password)
            self._apply_to_memory(site, cookie, username, password)
            self.log.info("Web UI：已更新 %s 的 Cookie/账号配置", site)
            return {"ok": True, "msg": f"{SITE_LABEL.get(site, site)} 配置已保存并即时生效"}

        # ---------- 手动签到 ----------
        @app.post("/api/run")
        async def run_signin(request: Request):
            if not _authed(request):
                return JSONResponse({"ok": False, "msg": "未登录"}, status_code=401)
            with self._lock:
                if self.job.get("running"):
                    return JSONResponse({"ok": False, "msg": "已有签到任务在执行"}, status_code=409)
                try:
                    body = await request.json()
                except Exception:
                    body = {}
                body = body or {}
                self.job = {"running": True, "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "finished": "", "ok": None, "summary": "",
                            "sites": body.get("sites") or [], "force": bool(body.get("force"))}
            t = threading.Thread(target=self._run_job, daemon=True)
            t.start()
            return {"ok": True, "msg": "已开始"}

        @app.get("/api/run/status")
        def run_status(request: Request):
            if not _authed(request):
                return JSONResponse({"ok": False, "msg": "未登录"}, status_code=401)
            return {"ok": True, **{k: self.job.get(k) for k in
                                   ("running", "started", "finished", "ok", "summary", "sites")}}

    # ---------------- 内部 ----------------
    def _overview(self) -> list:
        from .state import State
        db = ROOT / self.cfg.runtime.state_db
        state = State(db)
        today = state.today()
        out = []
        try:
            for key, site in self.cfg.sites.items():
                accs = []
                for acc in site.accounts:
                    rows = state.history(key, acc.name, limit=2)
                    latest = rows[0] if rows else None
                    latest_today = latest[0] == today and latest[1] in ("success", "already")
                    rec = {}
                    if latest:
                        cur = state.conn.execute(
                            "SELECT ts, status, message, reward, balance FROM signin_log "
                            "WHERE day=? AND site=? AND account=?",
                            (latest[0], key, acc.name))
                        r = cur.fetchone()
                        if r:
                            rec = {"day": r[0] and latest[0], "ts": r[0], "status": r[1],
                                   "message": r[2] or "", "reward": r[3] or "", "balance": r[4] or ""}
                    accs.append({
                        "name": acc.name,
                        "streak": state.streak(key, acc.name),
                        "done_today": latest_today,
                        "latest": rec,
                    })
                out.append({
                    "site": key, "label": SITE_LABEL.get(key, key),
                    "enabled": site.enabled, "strategy": site.strategy,
                    "accounts": accs,
                })
        finally:
            state.close()
        return out

    def _records(self, days: int, site: str) -> list:
        from .state import State
        db = ROOT / self.cfg.runtime.state_db
        state = State(db)
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        out = []
        try:
            sql = ("SELECT day, ts, site, account, status, message, reward, balance "
                   "FROM signin_log WHERE day >= ?")
            args: list = [since]
            if site:
                sql += " AND site=?"
                args.append(site)
            sql += " ORDER BY day DESC, ts DESC LIMIT 500"
            for day, ts, s, acc, status, msg, reward, balance in state.conn.execute(sql, args):
                out.append({"day": day, "ts": ts, "site": s, "site_label": SITE_LABEL.get(s, s),
                            "account": acc, "status": status,
                            "status_label": STATUS_LABEL.get(status, status),
                            "message": msg or "", "reward": reward or "", "balance": balance or ""})
        finally:
            state.close()
        return out

    def _cookie_view(self) -> list:
        out = []
        for key, site in self.cfg.sites.items():
            accs = []
            for acc in site.accounts:
                accs.append({
                    "name": acc.name,
                    "cookie_masked": _mask(acc.cookie),
                    "has_cookie": bool(acc.cookie),
                    "username": acc.username or "",
                    "has_password": bool(acc.password),
                })
            out.append({"site": key, "label": SITE_LABEL.get(key, key),
                        "enabled": site.enabled, "env": SITE_ENV.get(key, ""),
                        "separator": site.cookie_separator, "accounts": accs})
        return out

    def _write_env(self, site: str, cookie: str, username: str, password: str) -> None:
        """把导入的配置写回 .env（保留其余行；注释行不动）。"""
        updates: Dict[str, str] = {}
        if cookie:
            updates[SITE_ENV[site]] = cookie
        if username:
            updates[f"{SITE_ENV[site].rsplit('_COOKIE', 1)[0]}_USERNAME"] = username
        if password:
            updates[f"{SITE_ENV[site].rsplit('_COOKIE', 1)[0]}_PASSWORD"] = password
        env_path = ROOT / ".env"
        lines: list = []
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines()
        new_lines = []
        seen = set()
        for line in lines:
            m = re.match(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if m and m.group(2) in updates:
                key = m.group(2)
                val = updates[key].replace('"', '\\"')
                new_lines.append(f'{key}="{val}"')
                seen.add(key)
            else:
                new_lines.append(line)
        missing = [k for k in updates if k not in seen]
        if missing:
            new_lines.append("")
            new_lines.append(f"# Web UI 导入 · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
            for k in missing:
                new_lines.append(f'{k}="{updates[k]}"')
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        for k, v in updates.items():
            os.environ[k] = v

    def _apply_to_memory(self, site: str, cookie: str, username: str, password: str) -> None:
        """热更新内存配置，下一次签到立即使用新 Cookie。"""
        site_cfg = self.cfg.sites.get(site)
        if not site_cfg or not site_cfg.accounts:
            return
        acc = site_cfg.accounts[0]
        if cookie:
            acc.cookie = cookie
        if username:
            acc.username = username
        if password:
            acc.password = password

    # ---------------- 手动签到任务 ----------------
    def _run_job(self) -> None:
        try:
            import main as entry
            log = self.log
            class _Args:
                site = list(self.job.get("sites") or []) or None
                account = None
                force = bool(self.job.get("force"))
                dry_run = False
                no_notify = False
            from .notify import Notifier
            notifier = Notifier(self.cfg.notify, logger=log)
            code, summary = entry.run_once(self.cfg, log, _Args(), notifier)
            self.job.update(running=False, finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            ok=(code == 0), summary=summary)
        except Exception as e:
            self.log.exception("Web UI 手动签到失败")
            self.job.update(running=False, finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            ok=False, summary=f"执行异常：{type(e).__name__}: {e}")

    @property
    def app(self):
        return self._app
