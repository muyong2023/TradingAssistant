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

CREATE TABLE IF NOT EXISTS chat_history (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    role    TEXT NOT NULL,     -- user / assistant
    content TEXT NOT NULL,
    ts      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_history ON chat_history(chat_id, id);

CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

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


# --------------------------------------------------------------------------
# 对话历史与 bot 状态
# --------------------------------------------------------------------------

def append_chat(chat_id: str, role: str, content: str, path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO chat_history (chat_id, role, content, ts) VALUES (?,?,?,?)",
            (str(chat_id), role, content, datetime.now(timezone.utc).isoformat()),
        )


def load_chat(chat_id: str, limit: int = 12, path: Path | None = None) -> list[dict]:
    """取最近若干轮对话。

    只存纯文本，不存 thinking 与 tool_use 块 —— 那些跨轮重放会让历史
    迅速膨胀，且模型下一轮本来就会按需重新调用工具。
    """
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT role, content FROM chat_history WHERE chat_id=?"
            " ORDER BY id DESC LIMIT ?",
            (str(chat_id), limit),
        ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    #  首条必须是 user，否则接口报错
    while history and history[0]["role"] != "user":
        history.pop(0)
    return history


def clear_chat(chat_id: str, path: Path | None = None) -> int:
    with connect(path) as conn:
        cur = conn.execute("DELETE FROM chat_history WHERE chat_id=?", (str(chat_id),))
        return cur.rowcount


def get_state(key: str, default: str = "", path: Path | None = None) -> str:
    with connect(path) as conn:
        row = conn.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(key: str, value: str, path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO bot_state (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
