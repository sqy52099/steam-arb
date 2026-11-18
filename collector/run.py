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

# 本轮采集的硬截止时间点（monotonic）。CI 上超时就整轮白跑，
# 所以宁可少采一点也要自己先收工，把已经拿到的写回去。
_DEADLINE = None

# 本轮各环节的成败计数，跟着 latest.json 一起导出。
# 用途：CI 上「采集失败也 exit 0」是为了保住旧数据，但那样从外面看不出
# 是「Steam 连不上」还是「今天本来就没变化」。有了 health，汇总步骤就能
# 直接判断并标红，不用人去翻日志。
_HEALTH = {}


def set_deadline(minutes: float) -> None:
    global _DEADLINE
    _DEADLINE = time.monotonic() + minutes * 60 if minutes > 0 else None


def out_of_time() -> bool:
    return _DEADLINE is not None and time.monotonic() >= _DEADLINE


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def mark(key: str, ok: bool, detail: str = "") -> None:
    """记一个环节的成败，最后进 latest.json 的 health 字段。"""
    _HEALTH[key] = {"ok": bool(ok), "detail": str(detail)[:200]}


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
    """按 meta 里的游标轮转取一批，保证每轮打不同的物品。

    注意：游标不在这里推进 —— 提前收工时可能只采了一半，这时推进整批
    会让没采到的那部分被跳过。真正的推进在 advance_cursor。
    """
    rows = candidates(con, appid=APPID, min_volume=30, limit=pool_size)
    if not rows:
        return []
    cursor = int(meta_get(con, "rotation_cursor", 0) or 0)
    cursor %= len(rows)
    return (rows[cursor:] + rows[:cursor])[:batch]


def advance_cursor(con, pool_size: int, done: int) -> None:
    """只推进实际采到的件数，保证一轮被打断也不会漏物品。"""
    if done <= 0:
        return
    rows = candidates(con, appid=APPID, min_volume=30, limit=pool_size)
    if not rows:
        return
    cursor = int(meta_get(con, "rotation_cursor", 0) or 0)
    meta_set(con, "rotation_cursor", (cursor + done) % len(rows))


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
    # 假数据也要填 health，否则测汇总步骤时永远是空的，测不出标红逻辑。
    mark("thirdparty", True, "fake")
    mark("steam_list", True, "fake")
    mark("nameid", True, "fake")
    mark("steam_orders", True, "fake")
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
        mark("thirdparty", False, "Skinport 和 Waxpeer 都失败")
        raise SystemExit("两家第三方都拿不到数据，本轮放弃（保留旧数据）")
    buy_map = thirdparty.merge(wax, skin)
    buy_by_name = {r["name"]: r for r in buy_map}
    mark("thirdparty", True, f"waxpeer {len(wax)} / skinport {len(skin)} / 合并 {len(buy_by_name)}")
    log(f"合并后 {len(buy_by_name)} 条有买入价")

    # 2. 物品字典（Steam 列表接口）
    import steam as st
    try:
        listed = st.iter_all(APPID, count=100, max_pages=args.pages,
                             on_page=lambda p, n, t: log(f"  列表第 {p} 页 {n}/{t}"),
                             should_stop=out_of_time)
        upsert_items(con, [{"hash_name": r["hash_name"],
                            "volume": r["sell_listings"],
                            "tradable": 1} for r in listed])
        if listed:
            mark("steam_list", True, f"{len(listed)} 件")
        else:
            mark("steam_list", False, "一件都没拿到（被拦或全空）")
        log(f"物品字典更新 {len(listed)} 条")
    except Exception as e:
        mark("steam_list", False, str(e))
        log(f"Steam 列表接口不可用（{e}），跳过字典更新")
        listed = []

    # 3a. 先补 nameid：没有它连候选池都进不了，会死锁
    todo = pick_need_nameid(con, args.nameid_budget)
    if todo:
        log(f"补 nameid {len(todo)} 件（每件要抓一次物品页，很贵）")
        ok = 0
        for hn in todo:
            if out_of_time():
                log("  时间预算用完，nameid 补到一半停下")
                break
            try:
                nid = st.fetch_nameid(hn, APPID)
            except Exception as e:
                log(f"  nameid 失败 {hn}: {e}")
                continue
            if nid:
                set_nameid(con, hn, nid, APPID)
                ok += 1
        log(f"  补到 {ok}/{len(todo)}")
        mark("nameid", ok > 0, f"{ok}/{len(todo)}")
    else:
        mark("nameid", True, "本轮不需要补")

    # 3b. 从已有 nameid 的池子里轮转取一批查求购价
    batch = pick_batch(con, args.pool, args.batch)
    log(f"本轮采集 {len(batch)} 件")
    got = 0
    scanned = 0        # 真正发过请求的件数，游标按它推进
    err = ""
    for it in batch:
        if out_of_time():
            log("  时间预算用完，求购价采到一半停下")
            break
        hn = it["hash_name"]
        nameid = it.get("nameid")
        if not nameid:
            continue
        try:
            hist = st.orders_histogram(nameid)
        except Exception as e:
            log(f"  求购价失败 {hn}: {e}")
            err = f"{hn}: {e}"
            break          # 多半是被限流了，本轮收工
        scanned += 1
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
    log(f"成功写入 {got} 条（实际请求 {scanned} 件）")
    # 判断标准：只要真的发出去过请求并且有回音，就算这条链路通。
    # 一件都没成交（got=0）可能只是这批恰好没数据，不等于被拦。
    mark("steam_orders", scanned > 0,
         f"{got}/{scanned}" + (f" · 中断于 {err}" if err else ""))
    advance_cursor(con, args.pool, scanned)

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
        # 每轮各环节的成败。CI 采集失败也 exit 0（为了保住旧数据），
        # 所以「这一轮到底通不通」只能靠它判断，汇总步骤读这个字段标红绿。
        "health": _HEALTH,
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
    ap.add_argument("--deadline", type=float, default=0,
                    help="本轮最多干多少分钟，到点自己收工（CI 上传超时用）")
    ap.add_argument("--fake", action="store_true")
    a = ap.parse_args()
    if a.deadline > 0:
        set_deadline(a.deadline)
    if a.fake:
        run_fake(a)
    else:
        run_real(a)


if __name__ == "__main__":
    main()
