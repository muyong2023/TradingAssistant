"""SQLite 持久化。

存三类东西：
1. bars     —— 日线，避免每次都重新下载
2. scans    —— 每次扫描的指标快照，用于看板和历史对比
3. alerts   —— 已推送记录，实现"同股同档当天只推一次"的去重
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from ta.config import DB_PATH
from ta.data.base import Bar

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    day    TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, day)
);

CREATE TABLE IF NOT EXISTS scans (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    symbol   TEXT NOT NULL,
    price    REAL,
    change_pct REAL,
    rsi      REAL,
    payload  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scans_symbol_ts ON scans(symbol, ts);

CREATE TABLE IF NOT EXISTS alerts (
    day    TEXT NOT NULL,
    symbol TEXT NOT NULL,
    kind   TEXT NOT NULL,   -- pct_move / rsi_extreme / volume_spike
    tier   TEXT NOT NULL,   -- 档位，如 "7" / "oversold"
    ts     TEXT NOT NULL,
    detail TEXT,
    PRIMARY KEY (day, symbol, kind, tier)
);
"""


@contextmanager
def connect(path: Path | None = None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def save_bars(symbol: str, bars: list[Bar], path: Path | None = None) -> int:
    """幂等写入：重跑同一天不会产生重复行。"""
    with connect(path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO bars (symbol, day, open, high, low, close, volume)"
            " VALUES (?,?,?,?,?,?,?)",
            [(symbol, b.day.isoformat(), b.open, b.high, b.low, b.close, b.volume) for b in bars],
        )
    return len(bars)


def save_scan(symbol: str, price: float, change_pct: float, rsi_value: float | None,
              payload: dict, path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO scans (ts, symbol, price, change_pct, rsi, payload) VALUES (?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), symbol, price, change_pct,
             rsi_value, json.dumps(payload, default=str)),
        )


def already_alerted(symbol: str, kind: str, tier: str, day: date | None = None,
                    path: Path | None = None) -> bool:
    with connect(path) as conn:
        row = conn.execute(
            "SELECT 1 FROM alerts WHERE day=? AND symbol=? AND kind=? AND tier=?",
            ((day or date.today()).isoformat(), symbol, kind, str(tier)),
        ).fetchone()
    return row is not None


def record_alert(symbol: str, kind: str, tier: str, detail: str = "",
                 day: date | None = None, path: Path | None = None) -> bool:
    """登记一次推送。返回 False 表示今天这一档已经推过了（调用方应跳过）。"""
    with connect(path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO alerts (day, symbol, kind, tier, ts, detail)"
            " VALUES (?,?,?,?,?,?)",
            ((day or date.today()).isoformat(), symbol, kind, str(tier),
             datetime.now(timezone.utc).isoformat(), detail),
        )
        return cur.rowcount > 0
