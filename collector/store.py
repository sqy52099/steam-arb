#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SQLite 存储层：items 物品字典 / quotes 报价历史 / meta 采集进度。

GitHub Actions 的机器每次都是新的，所以 db 文件每轮从 data 分支 checkout
下来，跑完再 push 回去；30 天以上的报价自动清掉，文件不会无限长大。

设计要点：
  * items 表存 item_nameid —— 这个值终生不变，存下来就再也不用抓物品页，
    能把每轮请求量砍掉一半（见方案「关键点」）。
  * quotes 用 (ts, appid, hash_name) 做主键，重复写入用 INSERT OR REPLACE。
  * meta 表放轮转游标 rotation_cursor，配合 item_nameid 做分批采集。
"""
from __future__ import annotations

import os
import sqlite3
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("ARB_DB", os.path.join(BASE, "..", "data", "arb.db"))
KEEP_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    appid      INTEGER NOT NULL,
    hash_name  TEXT    NOT NULL,
    nameid     INTEGER,                 -- Steam item_nameid，终生不变
    tradable   INTEGER DEFAULT 1,
    volume     INTEGER DEFAULT 0,       -- 最近一次看到的 24h 成交量
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    PRIMARY KEY (appid, hash_name)
);
CREATE INDEX IF NOT EXISTS idx_items_volume ON items(appid, volume DESC);

CREATE TABLE IF NOT EXISTS quotes (
    ts           INTEGER NOT NULL,
    appid        INTEGER NOT NULL,
    hash_name    TEXT    NOT NULL,
    sell_cny     REAL,        -- Steam 最低挂单价（买家付）
    bid_cny      REAL,        -- Steam 最高求购价（买家付），可能为空
    net_cny      REAL,        -- 扣完 Steam+发行商费后卖家净得
    volume       INTEGER,
    src_buy_cny  REAL,        -- 第三方买入价
    src_name     TEXT,
    ratio        REAL,        -- net_cny / src_buy_cny
    PRIMARY KEY (ts, appid, hash_name)
);
CREATE INDEX IF NOT EXISTS idx_quotes_item ON quotes(appid, hash_name, ts DESC);
CREATE INDEX IF NOT EXISTS idx_quotes_ts   ON quotes(ts);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    p = os.path.abspath(path or DB_PATH)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    con = sqlite3.connect(p, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


# ---------------- meta ----------------

def meta_get(con, k: str, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def meta_set(con, k: str, v):
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))
    con.commit()


# ---------------- items ----------------

def upsert_items(con, rows: list[dict], appid: int = 730) -> int:
    """rows: [{name/hash_name, volume?, tradable?, nameid?}]"""
    now = int(time.time())
    n = 0
    for r in rows:
        hn = r.get("hash_name") or r.get("name")
        if not hn:
            continue
        con.execute(
            """INSERT INTO items(appid,hash_name,nameid,tradable,volume,first_seen,last_seen)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(appid,hash_name) DO UPDATE SET
                 volume   = COALESCE(excluded.volume, items.volume),
                 tradable = COALESCE(excluded.tradable, items.tradable),
                 nameid   = COALESCE(excluded.nameid, items.nameid),
                 last_seen= excluded.last_seen""",
            (appid, hn, r.get("nameid"), r.get("tradable", 1),
             r.get("volume", 0), now, now),
        )
        n += 1
    con.commit()
    return n


def set_nameid(con, hash_name: str, nameid: int, appid: int = 730) -> None:
    con.execute("UPDATE items SET nameid=? WHERE appid=? AND hash_name=?",
                (int(nameid), appid, hash_name))
    con.commit()


def need_nameid(con, hash_name: str, appid: int = 730) -> bool:
    r = con.execute("SELECT nameid FROM items WHERE appid=? AND hash_name=?",
                    (appid, hash_name)).fetchone()
    return not r or not r["nameid"]


def candidates(con, appid: int = 730, min_volume: int = 30,
               limit: int = 2000) -> list[dict]:
    """候选池：成交量够、有 nameid 的物品。按成交量降序。"""
    cur = con.execute(
        """SELECT hash_name, nameid, volume FROM items
           WHERE appid=? AND nameid IS NOT NULL AND volume>=?
           ORDER BY volume DESC LIMIT ?""",
        (appid, min_volume, limit))
    return [dict(r) for r in cur.fetchall()]


def rotation_slice(rows: list[dict], batch: int) -> list[dict]:
    """按 nameid 轮转取一批，保证每轮打不同的物品，几轮覆盖全池。"""
    if not rows:
        return []
    return rows[:batch] if len(rows) <= batch else rows[:batch]


# ---------------- quotes ----------------

def insert_quotes(con, rows: list[dict], appid: int = 730) -> int:
    n = 0
    for r in rows:
        con.execute(
            """INSERT OR REPLACE INTO quotes
               (ts,appid,hash_name,sell_cny,bid_cny,net_cny,volume,
                src_buy_cny,src_name,ratio)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (int(r["ts"]), appid, r["hash_name"], r.get("sell_cny"),
             r.get("bid_cny"), r.get("net_cny"), r.get("volume"),
             r.get("src_buy_cny"), r.get("src_name"), r.get("ratio")),
        )
        n += 1
    con.commit()
    return n


def latest_quotes(con, appid: int = 730, within_hours: int = 24,
                  limit: int = 500) -> list[dict]:
    """每个物品取最新一条报价，只保留 within_hours 内的。"""
    since = int(time.time()) - within_hours * 3600
    cur = con.execute(
        """SELECT q.* FROM quotes q
           JOIN (SELECT hash_name, MAX(ts) AS mx FROM quotes
                 WHERE appid=? AND ts>=? GROUP BY hash_name) t
             ON q.hash_name=t.hash_name AND q.ts=t.mx
           WHERE q.appid=? AND q.ratio IS NOT NULL
           ORDER BY (q.ratio * COALESCE(q.volume,0)) DESC
           LIMIT ?""",
        (appid, since, appid, limit))
    return [dict(r) for r in cur.fetchall()]


def history(con, hash_name: str, appid: int = 730, days: int = 7) -> list[dict]:
    since = int(time.time()) - days * 86400
    cur = con.execute(
        """SELECT ts,sell_cny,bid_cny,net_cny,src_buy_cny,ratio FROM quotes
           WHERE appid=? AND hash_name=? AND ts>=? ORDER BY ts""",
        (appid, hash_name, since))
    return [dict(r) for r in cur.fetchall()]


# ---------------- 维护 ----------------

def purge_old(con, keep_days: int = KEEP_DAYS) -> int:
    cutoff = int(time.time()) - keep_days * 86400
    n = con.execute("DELETE FROM quotes WHERE ts < ?", (cutoff,)).rowcount
    con.commit()
    if n:
        con.execute("VACUUM")
        con.commit()
    return n


def stats(con) -> dict:
    q = lambda s: con.execute(s).fetchone()[0]
    return {
        "items": q("SELECT COUNT(*) FROM items"),
        "items_with_nameid": q("SELECT COUNT(*) FROM items WHERE nameid IS NOT NULL"),
        "quotes": q("SELECT COUNT(*) FROM quotes"),
        "latest_ts": q("SELECT MAX(ts) FROM quotes") or 0,
    }


if __name__ == "__main__":
    con = connect()
    print("db:", os.path.abspath(DB_PATH))
    print(stats(con))
    if con.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]:
        print("\n最新 5 条：")
        for r in latest_quotes(con, limit=5):
            print(f"  {r['ratio']:.3f}  {r['hash_name']}")
