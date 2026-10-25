"""SQLite 状态库：当天去重、历史留痕，避免重复签到与静默失败。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .base import SigninResult


class State:
    """当日去重 + 历史留痕。

    状态库是辅助设施：磁盘只读、权限不足、tmp 不可用等情况都应该只降级为
    「不去重」，绝不能让签到主流程整个崩掉。
    """

    def __init__(self, db_path: str | Path, logger=None):
        self.path = Path(db_path)
        self.log = logger
        self.conn = None
        self.degraded = False
        self._memory: set = set()          # 降级时用的内存去重表
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.path), timeout=10)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS signin_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    site TEXT NOT NULL,
                    account TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT,
                    reward TEXT,
                    balance TEXT,
                    UNIQUE(day, site, account)
                )
            """)
            self.conn.commit()
        except Exception as e:
            self._downgrade(f"状态库不可用（{type(e).__name__}: {e}）")

    def _downgrade(self, reason: str) -> None:
        if not self.degraded:
            self.degraded = True
            if self.log:
                self.log.warning("%s，本次运行的去重失效，改为内存去重", reason)
        self.conn = None

    def today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def done_today(self, site: str, account: str, day: Optional[str] = None) -> bool:
        day = day or self.today()
        key = (day, site, account)
        if self.conn is None:
            return key in self._memory
        try:
            cur = self.conn.execute(
                "SELECT status FROM signin_log WHERE day=? AND site=? AND account=?",
                (day, site, account),
            )
            row = cur.fetchone()
        except Exception as e:
            self._downgrade(f"读取状态失败（{e}）")
            return key in self._memory
        return bool(row and row[0] in ("success", "already"))

    def record(self, r: SigninResult, day: Optional[str] = None) -> None:
        day = day or self.today()
        key = (day, r.site, r.account)
        row = (day, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), r.site, r.account,
               r.status, r.message[:300], r.reward, r.balance)
        if r.ok:
            self._memory.add(key)
        if self.conn is None:
            return
        try:
            self.conn.execute(
                """INSERT INTO signin_log (day, ts, site, account, status, message, reward, balance)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(day, site, account) DO UPDATE SET
                     ts=excluded.ts, status=excluded.status, message=excluded.message,
                     reward=excluded.reward, balance=excluded.balance""",
                row,
            )
            self.conn.commit()
        except Exception as e:
            self._downgrade(f"写入状态失败（{e}）")

    def history(self, site: str, account: str, limit: int = 7) -> List[tuple]:
        if self.conn is None:
            return []
        try:
            cur = self.conn.execute(
                "SELECT day, status, reward FROM signin_log WHERE site=? AND account=? "
                "ORDER BY day DESC LIMIT ?", (site, account, limit))
            return cur.fetchall()
        except Exception:
            return []

    def streak(self, site: str, account: str) -> int:
        """连续成功天数（简单按日期倒序推算）。"""
        rows = self.history(site, account, limit=365)
        if not rows:
            return 0
        days = {r[0] for r in rows if r[1] in ("success", "already")}
        count, cursor = 0, datetime.now()
        while cursor.strftime("%Y-%m-%d") in days:
            count += 1
            cursor = cursor.fromordinal(cursor.toordinal() - 1)
        return count

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
