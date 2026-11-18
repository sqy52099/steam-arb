#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SQLite 存储层：items 物品名册 / quotes 最新报价 / meta 杂项。

为什么 quotes 每个物品只留一条最新（而不是攒历史）：
  改成多 job 并行后，一轮能采 500+ 件，一天 48 轮 ≈ 2.7 万行/天，
  30 天就是 80 万行。这个 db 每轮要从 data 分支 checkout 下来、跑完再
  push 回去，体积一大光传输就要几十秒。前端只需要「当前快照」，
  历史趋势以后单独做聚合表，不该让采集链路背这个包袱。
  现在最坏也就 6580 行（第三方给的全部候选），文件几百 KB。

db 的所有权：只有 merge job 写它。采集 shard 只产出 JSON，不碰 db ——
这样多个 shard 并行时不会互相覆盖。
"""
from __future__ import annotations

import os
import sqlite3
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("ARB_DB", os.path.join(BASE, "..", "data", "arb.db"))
KEEP_DAYS = 7

# 表结构改过一次：v1 是 (ts,...) 主键攒历史，v2 是 (appid,hash_name) 主键存快照。
# 版本号写进 meta，对不上就整表重建（这库本来就是可再生的缓存）。
SCHEMA_VERSION = "3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    appid      INTEGER NOT NULL,
    hash_name  TEXT    NOT NULL,
    tradable   INTEGER DEFAULT 1,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    PRIMARY KEY (appid, hash_name)
);

CREATE TABLE IF NOT EXISTS quotes (
    appid        INTEGER NOT NULL,
    hash_name    TEXT    NOT NULL,
    ts           INTEGER NOT NULL,   -- 最近一次观测时间
    first_ts     INTEGER NOT NULL,   -- 第一次观测时间（看这物品跟踪了多久）
    sell_cny     REAL,               -- Steam 最低挂单价（买家付，人民币）
    bid_cny      REAL,               -- Steam 最高求购价（买家付，人民币）
    net_cny      REAL,               -- 按求购价秒卖，扣费后卖家净得
    net_sell_cny REAL,               -- 按最低挂单价卖掉，扣费后卖家净得
    usd_bid      INTEGER,            -- 盘口原始值，单位美元分（留作校验）
    usd_sell     INTEGER,
    fx           REAL,               -- 本轮用的「国区人民币÷美元」折算比
    bid_depth    INTEGER,            -- 买盘前 10 档可承接件数
    bid_levels   INTEGER,            -- 买盘档数
    sell_orders  INTEGER,            -- 在售挂单件数
    buy_orders   INTEGER,            -- 求购单件数
    src_buy_cny  REAL,               -- 第三方买入价
    src_name     TEXT,
    src_qty      INTEGER,            -- 第三方库存量
    ratio        REAL,               -- net_cny / src_buy_cny
    ratio_sell   REAL,               -- net_sell_cny / src_buy_cny
    src_mid_cny  REAL,               -- 第三方自报的 Steam 中间价（校验用）
    PRIMARY KEY (appid, hash_name)
);
CREATE INDEX IF NOT EXISTS idx_quotes_ratio ON quotes(appid, ratio DESC);

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
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """老库直接重建 quotes —— 里面的数据本来就是每轮重新采的行情，
    丢了下一轮就补回来，不值得写迁移脚本。

    判据是「缺列」而不是版本号：加一列就重建一次，比维护迁移链简单，
    而且这些数据全都能重新采回来，没有任何不可再生的东西。
    """
    needed = {"first_ts", "src_mid_cny"}
    exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='quotes'"
    ).fetchone()
    if exists:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(quotes)")}
        if needed - cols:
            con.execute("DROP TABLE quotes")
            con.commit()
    con.executescript(SCHEMA)
    con.execute(
        "INSERT OR REPLACE INTO meta(k,v) VALUES('schema_version',?)",
        (SCHEMA_VERSION,))


def meta_get(con, k: str, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def meta_set(con, k: str, v) -> None:
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))
    con.commit()


# ---------------- items ----------------

def upsert_items(con, rows: list[dict], appid: int = 730) -> int:
    """rows: [{name/hash_name, tradable?}]"""
    now = int(time.time())
    n = 0
    for r in rows:
        hn = r.get("hash_name") or r.get("name")
        if not hn:
            continue
        con.execute(
            """INSERT INTO items(appid,hash_name,tradable,first_seen,last_seen)
               VALUES(?,?,?,?,?)
               ON CONFLICT(appid,hash_name) DO UPDATE SET
                 last_seen = excluded.last_seen""",
            (appid, hn, r.get("tradable", 1), now, now))
        n += 1
    con.commit()
    return n


# ---------------- quotes ----------------

QUOTE_COLS = (
    "ts", "first_ts", "sell_cny", "bid_cny", "net_cny", "net_sell_cny",
    "usd_bid", "usd_sell", "fx", "bid_depth", "bid_levels",
    "sell_orders", "buy_orders", "src_buy_cny", "src_name", "src_qty",
    "ratio", "ratio_sell", "src_mid_cny",
)


def upsert_quotes(con, rows: list[dict], appid: int = 730) -> int:
    """每件只留最新一条。first_ts 保留最早那次观测，不被覆盖。

    同一轮里一件物品可能被采到两次（4 个 shard 的切片是按名字排序切的，
    正常不会重叠，但 artifact 重跑、手工补采都可能撞上）。后写的那条
    未必更好 —— 有一回就是同一件被采到两次，高价那条盖掉低价那条，
    页面上的求购价凭空高了 3 倍。所以：**谁观测得更晚谁说了算**，
    同一时刻再按求购价高的留下。轮内去重已经在 run_merge 里做过一次，
    这里只兜底跨轮的情况。
    """
    n = 0
    ph = ",".join("?" * (len(QUOTE_COLS) + 2))
    sets = ",".join(f"{c}=excluded.{c}" for c in QUOTE_COLS if c != "first_ts")
    # 只有当新数据不旧于旧数据时才覆盖。用 ts 而不是价格排序，是因为按
    # 价格留高会让「行情真的跌了」永远写不进去，页面一直挂着旧高价 ——
    # 那比重复覆盖更危险。
    guard = (" WHERE excluded.ts > quotes.ts"
             " OR (excluded.ts = quotes.ts"
             "     AND COALESCE(excluded.bid_cny,0) >= COALESCE(quotes.bid_cny,0))")
    sql = (f"INSERT INTO quotes(appid,hash_name,{','.join(QUOTE_COLS)}) "
           f"VALUES({ph}) "
           f"ON CONFLICT(appid,hash_name) DO UPDATE SET {sets},"
           f" first_ts=COALESCE(quotes.first_ts, excluded.first_ts)"
           + guard)
    now = int(time.time())
    for r in rows:
        hn = r.get("hash_name") or r.get("name") or r.get("n")
        if not hn:
            continue
        r = dict(r)
        r.setdefault("ts", now)
        r.setdefault("first_ts", r["ts"])
        con.execute(sql, [appid, hn] + [r.get(c) for c in QUOTE_COLS])
        n += 1
    con.commit()
    return n


def latest_quotes(con, appid: int = 730, within_hours: int = 48,
                  limit: int = 800) -> list[dict]:
    """按面值比降序取前 N 条。超过 within_hours 没更新的算消失（不导出）。"""
    since = int(time.time()) - within_hours * 3600
    cur = con.execute(
        """SELECT * FROM quotes
           WHERE appid=? AND ts>=? AND ratio IS NOT NULL
           ORDER BY ratio DESC LIMIT ?""",
        (appid, since, limit))
    return [dict(r) for r in cur.fetchall()]


def purge_old(con, keep_days: int = KEEP_DAYS) -> int:
    """久没更新的物品清掉（下架了、或第三方不再收录）。"""
    cutoff = int(time.time()) - keep_days * 86400
    n = con.execute("DELETE FROM quotes WHERE ts < ?", (cutoff,)).rowcount
    con.commit()
    return n


def stats(con) -> dict:
    q = lambda s: con.execute(s).fetchone()[0]
    return {
        "items": q("SELECT COUNT(*) FROM items"),
        "quotes": q("SELECT COUNT(*) FROM quotes"),
        "latest_ts": q("SELECT MAX(ts) FROM quotes") or 0,
    }


if __name__ == "__main__":
    con = connect()
    print("db:", os.path.abspath(DB_PATH))
    print(stats(con))
    rows = latest_quotes(con, limit=5)
    if rows:
        print("\n面值比最高的 5 条：")
        for r in rows:
            print(f"  {r['ratio']:.3f}  {r['hash_name']}")
