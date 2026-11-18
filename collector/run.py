#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次采集的编排：第三方买入价 + Steam 价格 → 算比例 → 入库 → 导出前端 JSON。

每轮做的事：
  1. 拉 Skinport / Waxpeer 的第三方买入价（国内可直连）
  2. 用 Steam 列表接口更新物品字典和成交量
  3. 从候选池里轮转取 N 件，补 nameid（没有的话）并查求购价
  4. 用 fees.py 精确算净得、比例，写 quotes
  5. 清 30 天前的旧数据，导出 data/latest.json

--fake 用于没有外网时验证整条链路（写合成数据，会标记 fake=1）。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fees
import thirdparty
from store import (connect, candidates, insert_quotes, latest_quotes,
                   meta_get, meta_set, purge_old, set_nameid, stats,
                   upsert_items, need_nameid)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ARB_DATA 让 CI 把产物直接写进 data 分支的克隆目录
DATA = os.environ.get("ARB_DATA") or os.path.join(BASE, "data")
APPID = 730


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def usd_cny() -> float:
    """汇率：优先读环境变量，其次读 data/rate.json，最后兜底 7.10。
    汇率错了整体比例会偏，所以允许手动覆盖。"""
    env = os.environ.get("USD_CNY")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    fp = os.path.join(DATA, "rate.json")
    if os.path.exists(fp):
        try:
            with open(fp, encoding="utf-8") as f:
                return float(json.load(f).get("usd_cny") or 7.10)
        except Exception:
            pass
    return 7.10


def pick_batch(con, pool_size: int, batch: int) -> list[dict]:
    """按 meta 里的游标轮转取一批，保证每轮打不同的物品。"""
    rows = candidates(con, appid=APPID, min_volume=30, limit=pool_size)
    if not rows:
        return []
    cursor = int(meta_get(con, "rotation_cursor", 0) or 0)
    cursor %= len(rows)
    picked = (rows[cursor:] + rows[:cursor])[:batch]
    meta_set(con, "rotation_cursor", (cursor + batch) % len(rows))
    return picked


def pick_need_nameid(con, budget: int) -> list[str]:
    """挑成交量最高、但还没有 nameid 的物品。

    没有 nameid 就查不了求购价，也就进不了候选池 —— 所以必须有一支
    专门的队伍去补，否则会死锁（池子只收有 nameid 的，而没有 nameid 的
    永远拿不到 nameid）。
    """
    if budget <= 0:
        return []
    cur = con.execute(
        """SELECT hash_name FROM items
           WHERE appid=? AND nameid IS NULL AND volume>0
           ORDER BY volume DESC LIMIT ?""", (APPID, budget))
    return [r["hash_name"] for r in cur.fetchall()]


def run_fake(args) -> None:
    """生成合成数据跑通全链路，不联网。"""
    import steam as st
    rnd = random.Random(42)
    rows = []
    now = int(time.time())
    for i in range(80):
        hn = f"Fake Item {i:03d}"
        buy = round(rnd.uniform(3, 400), 2)
        # 让一部分物品「看起来很赚」，一部分亏，便于检查排序
        sell = round(buy * rnd.uniform(1.0, 1.45), 2)
        bid = round(sell * rnd.uniform(0.75, 0.98), 2)
        net = fees.net_from_buyer(sell)
        rows.append({
            "ts": now, "appid": APPID, "hash_name": hn,
            "sell_cny": sell, "bid_cny": bid, "net_cny": net,
            "volume": rnd.randint(5, 900),
            "src_buy_cny": buy, "src_name": "fake",
            "ratio": round(net / buy, 4) if buy else None,
        })
    con = connect(args.db)
    insert_quotes(con, rows)
    upsert_items(con, [{"hash_name": r["hash_name"], "volume": r["volume"],
                        "nameid": 1000 + i} for i, r in enumerate(rows)])
    meta_set(con, "fake", 1)
    log(f"[fake] 写入 {len(rows)} 条合成报价")
    export(args, con)


def run_real(args) -> None:
    rate = usd_cny()
    log(f"汇率 USD→CNY = {rate}")
    con = connect(args.db)

    # 1. 第三方买入价
    wax, skin = {}, {}
    try:
        wax = thirdparty.fetch_waxpeer(rate)
        log(f"Waxpeer  {len(wax)} 条")
    except Exception as e:
        log(f"Waxpeer 失败：{e}")
    try:
        skin = thirdparty.fetch_skinport()
        log(f"Skinport {len(skin)} 条")
    except Exception as e:
        log(f"Skinport 失败：{e}")
    if not wax and not skin:
        raise SystemExit("两家第三方都拿不到数据，本轮放弃（保留旧数据）")
    buy_map = thirdparty.merge(wax, skin)
    buy_by_name = {r["name"]: r for r in buy_map}
    log(f"合并后 {len(buy_by_name)} 条有买入价")

    # 2. 物品字典（Steam 列表接口）
    import steam as st
    try:
        listed = st.iter_all(APPID, count=100, max_pages=args.pages,
                             on_page=lambda p, n, t: log(f"  列表第 {p} 页 {n}/{t}"))
        upsert_items(con, [{"hash_name": r["hash_name"],
                            "volume": r["sell_listings"],
                            "tradable": 1} for r in listed])
        log(f"物品字典更新 {len(listed)} 条")
    except Exception as e:
        log(f"Steam 列表接口不可用（{e}），跳过字典更新")
        listed = []

    # 3a. 先补 nameid：没有它连候选池都进不了，会死锁
    todo = pick_need_nameid(con, args.nameid_budget)
    if todo:
        log(f"补 nameid {len(todo)} 件（每件要抓一次物品页，很贵）")
        ok = 0
        for hn in todo:
            try:
                nid = st.fetch_nameid(hn, APPID)
            except Exception as e:
                log(f"  nameid 失败 {hn}: {e}")
                continue
            if nid:
                set_nameid(con, hn, nid, APPID)
                ok += 1
        log(f"  补到 {ok}/{len(todo)}")

    # 3b. 从已有 nameid 的池子里轮转取一批查求购价
    batch = pick_batch(con, args.pool, args.batch)
    log(f"本轮采集 {len(batch)} 件")
    got = 0
    for it in batch:
        hn = it["hash_name"]
        nameid = it.get("nameid")
        if not nameid:
            continue
        try:
            hist = st.orders_histogram(nameid)
        except Exception as e:
            log(f"  求购价失败 {hn}: {e}")
            break          # 多半是被限流了，本轮收工
        if not hist:
            continue
        b = buy_by_name.get(hn) or {}
        sell = hist.get("lowest_sell_cny")
        bid = hist.get("bid_cny")
        buy_cny = b.get("buy_cny")
        net = fees.net_from_buyer(sell) if sell else None      # 按最低挂单价卖
        net_bid = fees.net_from_buyer(bid) if bid else None    # 按求购价秒卖
        got += 1
        insert_quotes(con, [{
            "ts": int(time.time()), "hash_name": hn,
            "sell_cny": sell, "bid_cny": bid,
            "net_cny": net_bid if net_bid else net,
            "volume": it.get("volume"),
            "src_buy_cny": buy_cny, "src_name": b.get("src"),
            "ratio": round((net_bid if net_bid else net) / buy_cny, 4)
                     if (buy_cny and (net_bid or net)) else None,
        }])
    log(f"成功写入 {got} 条")

    # 4. 维护 + 导出
    n = purge_old(con, 30)
    if n:
        log(f"清理 30 天前报价 {n} 条")
    meta_set(con, "last_run", int(time.time()))
    log(f"库状态：{stats(con)}")
    export(args, con)


def export(args, con) -> None:
    """给前端用的静态 JSON：只放排序后的前 N 条，控制体积。"""
    rows = latest_quotes(con, appid=APPID, within_hours=args.within_hours,
                         limit=args.export_limit)
    out = {
        "generated": int(time.time()),
        "rate_usd_cny": usd_cny(),
        "currency": "CNY",
        "count": len(rows),
        "note": "ratio = Steam 扣费后净得 ÷ 第三方买入价。余额折现要再乘 0.75~0.85。",
        "items": [{
            "n": r["hash_name"],
            "sell": r["sell_cny"],
            "bid": r["bid_cny"],
            "net": r["net_cny"],
            "buy": r["src_buy_cny"],
            "src": r["src_name"],
            "vol": r["volume"],
            "ratio": r["ratio"],
            "ts": r["ts"],
        } for r in rows],
    }
    os.makedirs(DATA, exist_ok=True)
    fp = os.path.join(DATA, "latest.json")
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, fp)
    size = os.path.getsize(fp) / 1024
    log(f"导出 {fp}（{len(rows)} 条，{size:.1f} KB）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="")
    ap.add_argument("--batch", type=int, default=250, help="本轮查求购价的件数")
    ap.add_argument("--pool", type=int, default=2000, help="候选池大小")
    ap.add_argument("--pages", type=int, default=200, help="列表接口翻页上限")
    ap.add_argument("--nameid-budget", type=int, default=60,
                    help="本轮最多补多少个 nameid（每个要抓一次物品页，很贵）")
    ap.add_argument("--export-limit", type=int, default=500)
    ap.add_argument("--within-hours", type=int, default=48)
    ap.add_argument("--fake", action="store_true")
    a = ap.parse_args()
    if a.fake:
        run_fake(a)
    else:
        run_real(a)


if __name__ == "__main__":
    main()
