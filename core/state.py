"""SQLite 状态库：当天去重、历史留痕，避免重复签到与静默失败。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .base import SigninResult


class State:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
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

    def today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def done_today(self, site: str, account: str, day: Optional[str] = None) -> bool:
        day = day or self.today()
        cur = self.conn.execute(
            "SELECT status FROM signin_log WHERE day=? AND site=? AND account=?",
            (day, site, account),
        )
        row = cur.fetchone()
        return bool(row and row[0] in ("success", "already"))

    def record(self, r: SigninResult, day: Optional[str] = None) -> None:
        day = day or self.today()
        self.conn.execute(
            """INSERT INTO signin_log (day, ts, site, account, status, message, reward, balance)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(day, site, account) DO UPDATE SET
                 ts=excluded.ts, status=excluded.status, message=excluded.message,
                 reward=excluded.reward, balance=excluded.balance""",
            (day, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), r.site, r.account,
             r.status, r.message[:300], r.reward, r.balance),
        )
        self.conn.commit()

    def history(self, site: str, account: str, limit: int = 7) -> List[tuple]:
        cur = self.conn.execute(
            "SELECT day, status, reward FROM signin_log WHERE site=? AND account=? "
            "ORDER BY day DESC LIMIT ?", (site, account, limit))
        return cur.fetchall()

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
