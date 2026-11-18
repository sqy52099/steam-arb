#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线自检：不联网，把各模块的连接处都跑一遍。

跑：python selftest.py
退出码 0 = 全部通过。

覆盖面按「出错了会静默给出错数字」来选：
  手续费阶梯、价格解析、盘口抠取、折算比标定、分片切分、快照覆盖、老库迁移。
这些地方错一位不会报错，只会让页面上的比例悄悄失真。
"""
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "collector"))

import fees                     # noqa: E402
import store                    # noqa: E402
import steam                    # noqa: E402
import thirdparty               # noqa: E402

FAIL = []
OK = []


def check(name, cond, detail=""):
    if cond:
        OK.append(name)
        print(f"  [OK] {name}")
    else:
        FAIL.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


# ---------------- 手续费 ----------------

def t_fees():
    print("\n手续费")
    n = fees.net_from_buyer(100.0)
    check("100 元净得 ≈ 86.97", abs(n - 86.97) < 0.01, f"得 {n}")

    # 回推：net 必须是「买家付得起」里最大的那个。两处 floor 让函数成阶梯，
    # 买家付 P 常落在两级之间，所以不能断言 buyer_pays(net) == P。
    bad = []
    for pay in range(3, 20000):
        net = fees.net_from_buyer_cents(pay)
        if not (fees.buyer_pays_cents(net) <= pay
                < fees.buyer_pays_cents(net + 1)):
            bad.append((pay, net, fees.buyer_pays_cents(net),
                        fees.buyer_pays_cents(net + 1)))
    check("净得是买家付得起的最大值", not bad, f"{len(bad)} 处不符 {bad[:3]}")

    gap = []
    for cents in range(3, 20000, 7):
        p = cents / 100.0
        nn = fees.net_from_buyer(p)
        if abs(fees.buyer_pays(nn) - p) > 0.031:
            gap.append((p, nn, fees.buyer_pays(nn)))
    check("元价位回推误差 < 3 分", not gap, f"{len(gap)} 处 {gap[:3]}")

    # 不能拿买家付 ÷1.15 近似：小额会差 2~3 分。这条是二分的存在理由。
    worst = max(abs(p / 1.15 - fees.net_from_buyer(p)) for p in (0.5, 1.0, 3.0))
    check("小额直接 ÷1.15 确实不准（证明必须二分）", worst > 0.01, f"最大差 {worst:.3f}")

    prev = 0
    mono = True
    for p in range(3, 5000):
        v = fees.net_from_buyer_cents(p)
        if v < prev:
            mono = False
            break
        prev = v
    check("净得随买家付出单调不减", mono)

    r_max = fees.fees_from_net(1000)["effective_rate"]
    check("大额有效费率 ≈ 86.96%", abs(r_max - 0.86957) < 0.0005, f"得 {r_max}")


# ---------------- 价格解析 ----------------

def t_price():
    print("\n价格解析")
    cases = [("¥ 1.23", 1.23), ("¥1,234.56", 1234.56), ("1.234,56€", 1234.56),
             ("1,23€", 1.23), ("¥ 12 500,00", 12500.0), ("12,345", 12345.0),
             ("12.345", 12345.0), ("1.234.567,89", 1234567.89), ("0,50", 0.5),
             ("$3.50", 3.50), ("abc", None), ("", None), (None, None)]
    bad = []
    for raw, want in cases:
        got = steam._parse_price(raw)
        if want is None:
            if got is not None:
                bad.append((raw, got, want))
        elif got is None or abs(got - want) > 0.001:
            bad.append((raw, got, want))
    check("_parse_price 各格式（含千分位）", not bad, str(bad))


# ---------------- 盘口解析 ----------------

def _fake_page(hash_name, bid=117, sell=119, buys=(108, 311, 107, 277),
               sells=(119, 348, 120, 712), decoy=False):
    """拼一段和 Steam SSR 页面同构的 HTML（转义两层的 JSON 字符串）。"""
    ob = {
        "amtMaxBuyOrder": bid, "amtMinSellOrder": sell,
        "eCurrency": 1, "cBuyOrders": 1799237, "cSellOrders": 41827,
        "rgCompactBuyOrders": list(buys),
        "rgCompactSellOrders": list(sells),
    }
    inner = '{"state":{"data":%s,"queryKey":["market","orderbook",730,%s]}}' % (
        json.dumps(ob, separators=(",", ":")),
        json.dumps(hash_name))
    if decoy:
        # 前面放一个别的 data 对象：老版本解析器有兜底分支会抓错它
        inner = ('{"other":{"data":{"amtMaxBuyOrder":9999,"amtMinSellOrder":1}},'
                 '"state":{"data":%s,"queryKey":["market","orderbook",730,%s]}}'
                 % (json.dumps(ob, separators=(",", ":")),
                    json.dumps(hash_name)))
    esc = inner.replace("\\", "\\\\").replace('"', '\\"')
    return f"<html><body><div id=app data-x=\"{esc}\"></div></body></html>"


def t_orderbook():
    print("\n盘口解析")
    html = _fake_page("Prisma Case")
    ob = steam.parse_orderbook(html, "Prisma Case")
    check("抠出求购价与挂单价", ob["usd_bid"] == 117 and ob["usd_sell"] == 119,
          str((ob.get("usd_bid"), ob.get("usd_sell"))))
    check("买盘深度 = 前 10 档件数和", ob["bid_depth"] == 311 + 277,
          f"得 {ob['bid_depth']}")
    check("档数与单数", ob["bid_levels"] == 2 and ob["sell_orders"] == 41827,
          f"{ob['bid_levels']}/{ob['sell_orders']}")

    # 老 bug：页面里有别的 data 对象时不能抓错
    ob2 = steam.parse_orderbook(_fake_page("Prisma Case", decoy=True), "Prisma Case")
    check("有干扰对象时仍抓对（老版本会抓成 9999）", ob2["usd_bid"] == 117,
          f"得 {ob2['usd_bid']}")

    def raises(fn, *a):
        try:
            fn(*a)
            return False
        except steam.ParseError:
            return True

    check("物品名对不上就报错，不张冠李戴",
          raises(steam.parse_orderbook, html, "AK-47 | Redline"))
    check("求购 > 挂单判为不合理",
          raises(steam.parse_orderbook, _fake_page("X", bid=200, sell=119), "X"))
    check("没有 orderbook 键就报错",
          raises(steam.parse_orderbook, "<html>Rate limited</html>", "X"))
    # 没有 "data":{"amtMaxBuyOrder" 锚点时也必须报错，不能退回模糊匹配
    broken = '{"state":{"data2":{"x":1},"queryKey":["market","orderbook",730,"X"]}}'
    esc = broken.replace("\\", "\\\\").replace('"', '\\"')
    check("缺锚点就报错，不退回模糊匹配",
          raises(steam.parse_orderbook, esc, "X"))


# ---------------- 折算比 ----------------

def t_factor():
    print("\n折算比标定")
    real = steam.price_overview
    table = {"A": (1.19, 7.95), "B": (2.88, 19.28), "C": (0.10, 0.67)}

    def fake(hn, currency=steam.CURRENCY_CNY, appid=steam.APPID, retries=1):
        if hn not in table:
            return None
        usd, cny = table[hn]
        v = usd if currency == steam.CURRENCY_USD else cny
        return {"lowest": v, "median": v, "volume": 100}

    steam.price_overview = fake
    try:
        r = steam.market_factor(["A", "B", "C"])
        # A→6.68 B→6.69 C→6.70，中位数 6.69；C 低于 0.5 美元应被剔掉
        check("折算比取中位数", r["factor"] is not None
              and abs(r["factor"] - 6.69) < 0.02, f"得 {r}")
        check("低价物品被剔除，不进样本", r["n"] == 2, f"n={r['n']}")
        r2 = steam.market_factor(["missing"])
        check("没样本时返回 None 而不是瞎猜", r2["factor"] is None)
    finally:
        steam.price_overview = real

    # 全拿不到时必须早早放弃，不能把合并 job 拖到十几分钟（真事故）
    calls = []

    def dead(hn, currency=steam.CURRENCY_CNY, appid=steam.APPID, retries=1):
        calls.append(hn)
        return None

    steam.price_overview = dead
    try:
        t0 = time.monotonic()
        r3 = steam.market_factor(["A", "B", "C", "D", "E", "F"], max_seconds=90)
        cost = time.monotonic() - t0
        check("全拿不到时返回 None", r3["factor"] is None)
        check("连续空手 3 次就收工，不把 6 个样本全试完",
              len(calls) == 3, f"实际试了 {len(calls)} 次")
        check("早退不需要等满超时", cost < 5, f"耗时 {cost:.1f}s")
    finally:
        steam.price_overview = real


# ---------------- 存储层 ----------------

def t_store():
    print("\n存储层")
    tmp = tempfile.mkdtemp(prefix="arbstore_")
    con = store.connect(os.path.join(tmp, "t.db"))
    check("建表", con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] >= 3)
    check("schema 版本写进 meta",
          store.meta_get(con, "schema_version") == store.SCHEMA_VERSION,
          str(store.meta_get(con, "schema_version")))

    store.upsert_items(con, [{"hash_name": "A"}, {"hash_name": "B"}])
    store.upsert_items(con, [{"hash_name": "A"}])
    check("items 幂等", store.stats(con)["items"] == 2,
          str(store.stats(con)["items"]))

    now = int(time.time())
    store.upsert_quotes(con, [{
        "hash_name": "A", "ts": now - 500, "first_ts": now - 500,
        "sell_cny": 10.0, "bid_cny": 9.0, "net_cny": 8.7,
        "src_buy_cny": 7.0, "src_name": "x", "ratio": 1.24}])
    store.upsert_quotes(con, [{
        "hash_name": "A", "ts": now, "first_ts": now,
        "sell_cny": 11.0, "bid_cny": 10.0, "net_cny": 9.5,
        "src_buy_cny": 7.0, "src_name": "x", "ratio": 1.36}])
    check("同一物品只留一条快照，不攒历史", store.stats(con)["quotes"] == 1,
          str(store.stats(con)["quotes"]))
    row = con.execute("SELECT * FROM quotes WHERE hash_name='A'").fetchone()
    check("快照被最新一次覆盖", abs(row["ratio"] - 1.36) < 1e-9, str(row["ratio"]))
    check("first_ts 保留最早观测", row["first_ts"] == now - 500,
          f"{row['first_ts']} vs {now - 500}")

    store.upsert_quotes(con, [{
        "hash_name": "B", "ts": now - 86400 * 10,
        "net_cny": 1.0, "src_buy_cny": 1.0, "ratio": 1.0}])
    n = store.purge_old(con, keep_days=7)
    check("清理久未更新的物品", n == 1 and store.stats(con)["quotes"] == 1, f"清了 {n}")

    # 晚到的旧数据不许盖掉新数据。反过来的取舍（「留高价」）会让行情真的
    # 跌了也写不进去，页面一直挂着旧高价，比重复覆盖危险得多。
    store.upsert_quotes(con, [{
        "hash_name": "A", "ts": now - 900, "bid_cny": 99.0, "net_cny": 85.0,
        "src_buy_cny": 7.0, "ratio": 12.1}])
    row = con.execute("SELECT * FROM quotes WHERE hash_name='A'").fetchone()
    check("迟到的旧数据不覆盖新快照", abs(row["ratio"] - 1.36) < 1e-9,
          str(row["ratio"]))
    # 时间相同（同一轮里撞上）时，留求购价高的那条
    store.upsert_quotes(con, [{
        "hash_name": "A", "ts": now, "bid_cny": 12.0, "net_cny": 11.0,
        "src_buy_cny": 7.0, "ratio": 1.57}])
    row = con.execute("SELECT * FROM quotes WHERE hash_name='A'").fetchone()
    check("同一时刻撞上时留求购价高的", abs(row["ratio"] - 1.57) < 1e-9,
          str(row["ratio"]))

    got = store.latest_quotes(con, within_hours=48, limit=10)
    check("最新报价按面值比降序", [r["hash_name"] for r in got] == ["A"], str(got))
    con.close()


def t_migrate():
    """CI 的 data 分支里躺着一个 v1 库（quotes 按 ts 攒历史），必须能自动重建。"""
    print("\n老库迁移")
    tmp = tempfile.mkdtemp(prefix="arbmig_")
    db = os.path.join(tmp, "old.db")
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE quotes(
            ts INTEGER NOT NULL, appid INTEGER NOT NULL,
            hash_name TEXT NOT NULL, sell_cny REAL, bid_cny REAL,
            net_cny REAL, volume INTEGER, src_buy_cny REAL,
            src_name TEXT, ratio REAL,
            PRIMARY KEY (ts, appid, hash_name));
        INSERT INTO quotes VALUES(123,730,'old',1,1,1,1,1,'x',1.0);
    """)
    con.commit()
    con.close()

    con = store.connect(db)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(quotes)")}
    check("v1 的 quotes 被重建成 v2", "first_ts" in cols and "volume" not in cols,
          str(sorted(cols)))
    check("v1 的历史行被丢弃，不阻塞新写入",
          con.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0)
    store.upsert_quotes(con, [{"hash_name": "new", "net_cny": 5.0,
                               "src_buy_cny": 4.0, "ratio": 1.25}])
    check("迁移后能正常写入", store.stats(con)["quotes"] == 1)
    con.close()


# ---------------- 第三方合并 ----------------

def t_merge():
    print("\n第三方合并")
    wax = {"M4A1 | Test": {"buy_cny": 100.0, "steam_mid_cny": 150.0, "qty": 12,
                           "type": "Rifle", "rarity": "Covert"}}
    skin = {"M4A1 | Test": {"buy_cny": 95.0, "suggested_cny": 160.0, "qty": 3}}
    rows = thirdparty.merge(wax, skin)
    check("取两家更低的买入价", rows and rows[0]["buy_cny"] == 95.0
          and rows[0]["src"] == "skinport", str(rows))
    check("库存量跟着取价的那家走", rows and rows[0]["src_qty"] == 3, str(rows))

    rows2 = thirdparty.merge(wax, {})
    check("只有一家时用那家", rows2 and rows2[0]["src"] == "waxpeer")

    junk = thirdparty.merge({"Sticker | Foo": {"buy_cny": 50, "steam_mid_cny": 60,
                                              "qty": 1, "type": "", "rarity": ""}}, {})
    check("剔除贴纸类", junk == [])
    cheap = thirdparty.merge({"X": {"buy_cny": 0.5, "steam_mid_cny": 1,
                                    "qty": 1, "type": "", "rarity": ""}}, {})
    check("剔除过低价格", cheap == [])
    check("is_junk 认 StatTrak", thirdparty.is_junk("StatTrak™ AK-47 | Redline"))


# ---------------- 分片调度 ----------------

def t_shards():
    """分片错了会表现为「某些物品永远采不到」或「几个 job 采同一批」，都不报错。"""
    print("\n分片调度")
    import run

    real_round = run.round_index
    try:
        names = [f"I{i:02d}" for i in range(20)]
        run.round_index = lambda: 0
        seg0 = [run.shard_slice(names, s, 4, 3) for s in range(4)]
        flat = [x for seg in seg0 for x in seg]
        check("4 个 shard 各取 3 件", all(len(s) == 3 for s in seg0),
              str([len(s) for s in seg0]))
        check("shard 之间不重叠", len(set(flat)) == len(flat), str(flat))
        check("同一轮覆盖连续区间", flat == names[0:12], str(flat))

        again = [run.shard_slice(names, s, 4, 3) for s in range(4)]
        check("同一轮内结果稳定（跨 job 能对齐）", again == seg0)

        run.round_index = lambda: 1
        seg1 = [run.shard_slice(names, s, 4, 3) for s in range(4)]
        f1 = [x for seg in seg1 for x in seg]
        check("下一轮换到不同区段", f1 != flat and len(set(f1)) == 12, str(f1))
        check("轮次推进按 per_round 步进", f1 == names[12:20] + names[0:4], str(f1))

        run.round_index = lambda: 0
        small = [run.shard_slice(names[:10], s, 4, 3) for s in range(4)]
        fs = [x for seg in small for x in seg]
        check("一轮能扫完时按 shard 均分而不是硬轮转",
              len(set(fs)) == len(fs) and len(fs) == 10, str(fs))

        check("空候选返回空", run.shard_slice([], 0, 4, 3) == [])
        check("batch 为 0 不采", run.shard_slice(names, 0, 4, 0) == [])
    finally:
        run.round_index = real_round


def t_deadline():
    print("\n时间预算")
    import run
    run.set_deadline(0.002)
    a = run.out_of_time()
    time.sleep(0.2)
    b = run.out_of_time()
    run.set_deadline(0)
    c = run.out_of_time()
    check("到点前不喊停、到点后喊停、关掉不喊停",
          a is False and b is True and c is False, f"{a},{b},{c}")


# ---------------- 端到端 ----------------

def t_fake():
    """两个阶段用同一个 shard 目录串起来跑一遍，检查导出的字段。"""
    print("\n离线全链路")
    import argparse
    import run

    tmp = tempfile.mkdtemp(prefix="arbfake_")
    old = run.DATA
    run.DATA = tmp
    try:
        args = argparse.Namespace(
            mode="fake", db=os.path.join(tmp, "f.db"),
            shard=0, shards=3, batch=6, give_up=25,
            out_dir=os.path.join(tmp, "shards"),
            in_dir=os.path.join(tmp, "shards"), fx_samples=0,
            export_limit=50, within_hours=48, deadline=0)
        run.run_fake(args)
        fp = os.path.join(tmp, "latest.json")
        check("导出 latest.json", os.path.isfile(fp))
        with open(fp, encoding="utf-8") as f:
            d = json.load(f)
        check("三个 shard 的 18 件全进来了", d["count"] == 18, f"得 {d['count']}")

        keys = ("thirdparty", "steam_orders", "fx", "merge")
        h = d.get("health") or {}
        check("health 覆盖两个阶段", all(k in h for k in keys), f"得 {sorted(h)}")
        bad = [k for k in keys if not (isinstance(h.get(k), dict)
                                       and isinstance(h[k].get("ok"), bool)
                                       and isinstance(h[k].get("detail"), str))]
        check("每项都是 {ok: bool, detail: str}", not bad, f"不合格 {bad}")
        check("detail 有长度上限", all(len(h[k]["detail"]) <= 200
                                       for k in keys if k in h))

        it = d["items"][0]
        need = ("n", "sell", "bid", "net", "net_sell", "buy", "src", "qty",
                "depth", "orders", "sell_orders", "ratio", "ratio_sell", "ts")
        miss = [k for k in need if k not in it]
        check("物品字段齐全", not miss, f"缺 {miss}")
        check("带了折算比，前端能解释价格怎么来的", d.get("fx") is not None,
              str(d.get("fx")))
        check("面值比按降序导出",
              all(d["items"][i]["ratio"] >= d["items"][i + 1]["ratio"]
                  for i in range(len(d["items"]) - 1)))
    finally:
        run.DATA = old


# ---------------- 真实 merge 路径 ----------------

def t_merge_real():
    """run_merge 是线上真正跑的那个函数，而 t_fake 走的是 merge_fake。

    这条路径出过一次线上事故：折算比抽样那行把 lambda 的参数 x 用到了
    列表推导的条件里，语法合法、只在运行到才 NameError，结果 4 个 shard
    采到的数据全丢了、页面上还是旧数字。所以必须真跑一遍，并且把
    priceoverview 换成假的（本机连不上 Steam）。
    """
    print("\\n真实 merge 路径")
    import argparse
    import run

    tmp = tempfile.mkdtemp(prefix="arbmerge_")
    old_data = run.DATA
    run.DATA = tmp
    shards = os.path.join(tmp, "shards")
    os.makedirs(shards)

    now = int(time.time())
    rows = [
        {"n": "Cheap Thing", "usd_bid": 50, "usd_sell": 60,
         "buy": 1.0, "src": "waxpeer", "qty": 10, "depth": 5,
         "blevels": 3, "buys": 100, "sells": 20, "ts": now},
        {"n": "Pricey Thing", "usd_bid": 1200, "usd_sell": 1500,
         "buy": 100.0, "src": "skinport", "qty": 4, "depth": 30,
         "blevels": 7, "buys": 900, "sells": 80, "ts": now},
        {"n": "No Bid", "usd_bid": 0, "usd_sell": 300,
         "buy": 20.0, "src": "waxpeer", "qty": 2, "depth": 0,
         "blevels": 0, "buys": 0, "sells": 5, "ts": now},
    ]
    for s in range(2):
        with open(os.path.join(shards, f"shard-{s:02d}.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"shard": s, "shards": 2, "round": 7, "ts": now,
                       "tried": len(rows) // 2, "err": "",
                       "health": {"steam_orders": {"ok": True,
                                                   "detail": "2/2"}},
                       "items": rows[s::2]}, f, ensure_ascii=False)

    real_factor = steam.market_factor
    asked = []

    def fake_factor(items, min_usd=0.5):
        asked.extend(items)
        return {"factor": 6.80, "n": len(items), "spread": 0.01, "detail": []}

    steam.market_factor = fake_factor
    try:
        args = argparse.Namespace(
            mode="merge", db=os.path.join(tmp, "m.db"), shard=0, shards=2,
            batch=2, give_up=25, out_dir=shards, in_dir=shards,
            fx_samples=2, export_limit=50, within_hours=48, deadline=0)
        run.run_merge(args)                       # 这里崩，线上就全丢

        fp = os.path.join(tmp, "latest.json")
        check("真跑 run_merge 不抛异常", os.path.isfile(fp), "没产出 latest.json")
        with open(fp, encoding="utf-8") as f:
            d = json.load(f)
        check("折算比用标定值而不是汇率", d.get("fx") == 6.80, str(d.get("fx")))
        check("只把够贵的送去标定（$0.5 以下放大误差）",
              asked == ["Pricey Thing"]
              or (len(asked) == 2 and "Cheap Thing" not in asked), str(asked))

        by = {it["n"]: it for it in d["items"]}
        check("没有求购价的物品被跳过", "No Bid" not in by, str(sorted(by)))
        p = by.get("Pricey Thing") or {}
        # $1200/100 × 6.80 = ¥81.60 → 扣 15% 手续费
        check("美元分按标定比折成人民币",
              abs((p.get("bid") or 0) - 81.60) < 0.01, str(p.get("bid")))
        check("净得走了阶梯手续费不是直接 ×0.85",
              abs((p.get("net") or 0) - 70.99) < 0.02, str(p.get("net")))
        check("面值比 = 净得 ÷ 买入价",
              abs((p.get("ratio") or 0) - 0.7099) < 0.001, str(p.get("ratio")))
        check("挂单价比求购价高，面值比也更大",
              (p.get("sell") or 0) > (p.get("bid") or 0)
              and (p.get("ratio_sell") or 0) > (p.get("ratio") or 0),
              f"{p.get('sell')} vs {p.get('bid')}")

        con = store.connect(args.db)
        n = con.execute("select count(*) from quotes").fetchone()[0]
        check("只入库有求购价的 2 件", n == 2, str(n))
        con.close()

        run.run_merge(args)                       # 再跑一次，验幂等
        con = store.connect(args.db)
        n2 = con.execute("select count(*) from quotes").fetchone()[0]
        check("重复 merge 不产生重复行（快照表按物品覆盖）", n2 == 2, str(n2))
        con.close()
    finally:
        steam.market_factor = real_factor
        run.DATA = old_data


def t_sanity():
    """合并阶段的盘口可信度过滤。

    这条是照着实测的脏数据写的：★ Talon Knife | Safari Mesh (Factory New)
    挂单只剩 1 件，物品页报的最低价是 $13.92，而 Waxpeer 自报中间价 $3.33，
    差 4 倍。不过滤的话页面上会出现 ratio 3.0 的「无风险机会」——
    真去下单只会亏，这比少几条严重得多。
    """
    print("\n盘口可信度过滤")
    import argparse
    import run

    tmp = tempfile.mkdtemp(prefix="arbsanity_")
    old_data = run.DATA
    run.DATA = tmp
    shards = os.path.join(tmp, "shards")
    os.makedirs(shards)

    now = int(time.time())
    # usd_sell 单位是美元分；mid 是第三方自报的 Steam 中间价（人民币）
    rows = [
        # 正常：挂单多、价格和参考价吻合
        {"n": "Good Skin", "usd_bid": 1000, "usd_sell": 1100, "buy": 10.0,
         "src": "waxpeer", "qty": 5, "depth": 3, "blevels": 2,
         "buys": 500, "sells": 200, "mid": 74.8, "ts": now},
        # 挂单只有 1 件 → 最低挂单价不可信
        {"n": "Thin Book", "usd_bid": 1000, "usd_sell": 1392, "buy": 10.0,
         "src": "waxpeer", "qty": 1, "depth": 3, "blevels": 2,
         "buys": 500, "sells": 1, "mid": 33.3, "ts": now},
        # 挂单不少但价格离谱 4 倍 → 只有参考价能拦住它
        {"n": "Insane Price", "usd_bid": 1000, "usd_sell": 1392, "buy": 10.0,
         "src": "waxpeer", "qty": 1, "depth": 3, "blevels": 2,
         "buys": 500, "sells": 500, "mid": 33.3, "ts": now},
    ]
    with open(os.path.join(shards, "shard-00.json"), "w",
              encoding="utf-8") as f:
        json.dump({"shard": 0, "shards": 1, "round": 9, "ts": now,
                   "tried": len(rows), "err": "",
                   "health": {"steam_orders": {"ok": True, "detail": "3/3"}},
                   "items": rows}, f, ensure_ascii=False)

    real_factor = steam.market_factor
    steam.market_factor = lambda items, **kw: {
        "factor": 6.80, "n": len(items), "spread": 0.01, "detail": []}
    try:
        args = argparse.Namespace(
            mode="merge", db=os.path.join(tmp, "s.db"), shard=0, shards=1,
            batch=3, give_up=25, out_dir=shards, in_dir=shards,
            fx_samples=3, export_limit=50, within_hours=48, deadline=0)
        run.run_merge(args)
        fp = os.path.join(tmp, "latest.json")
        with open(fp, encoding="utf-8") as f:
            d = json.load(f)
        names = sorted(it["n"] for it in d["items"])
        check("挂单太少的被丢掉", "Thin Book" not in names, str(names))
        check("偏离第三方参考价太多的被丢掉",
              "Insane Price" not in names, str(names))
        check("干净的那件留下", names == ["Good Skin"], str(names))
        m = d["health"].get("merge") or {}
        note = str(m.get("detail") or "")
        check("Summary 里说清丢了几件、为什么",
              "挂单太少" in note and "偏离参考价" in note, note)

        # 关键：往轮留下的脏行也要在导出时挡掉。一轮只采几十件，表里
        # 五千多件都是往轮的，只靠写入端过滤，旧脏行会一直挂在页面上。
        con = store.connect(args.db)
        store.upsert_quotes(con, [{
            "hash_name": "Old Junk", "ts": now - 3000,
            "first_ts": now - 3000,
            "sell_cny": 139.2, "bid_cny": 100.0, "net_cny": 87.0,
            "net_sell_cny": 121.0, "src_buy_cny": 10.0, "src_name": "x",
            "sell_orders": 1, "src_mid_cny": 33.3,
            "ratio": 8.7, "ratio_sell": 12.1}])
        con.close()
        run.export(args, store.connect(args.db))
        with open(fp, encoding="utf-8") as f:
            d2 = json.load(f)
        names2 = [it["n"] for it in d2["items"]]
        check("导出时挡掉往轮留下的脏盘口（ratio 再高也不放）",
              "Old Junk" not in names2, str(names2))
    finally:
        steam.market_factor = real_factor
        run.DATA = old_data


def t_shard_fx():
    """折算比要由采集阶段交上来，因为 merge 只有一个出口 IP。

    线上连着两轮 fx 都标定失败 —— merge 是单独一个 job，撞上 Steam 限流
    就整轮拿不到，只好退回汇率，所有人民币价偏高约 6%。而每个 collect
    job 有自己的 IP，让它们各标一份，挑中位数，成功率高得多。
    """
    print("\n折算比来自采集阶段")
    import argparse
    import run

    tmp = tempfile.mkdtemp(prefix="arbfx_")
    old_data = run.DATA
    run.DATA = tmp
    shards = os.path.join(tmp, "shards")
    os.makedirs(shards)

    now = int(time.time())
    items = [{"n": "Thing", "usd_bid": 1000, "usd_sell": 1400,
              "buy": 10.0, "src": "waxpeer", "qty": 5, "depth": 3,
              "blevels": 2, "buys": 400, "sells": 300, "mid": None,
              "ts": now}]
    # 四台各标一次，其中两台被限流没标出来
    fx_per_shard = [
        {"factor": 6.72, "n": 3, "spread": 0.01, "detail": []},
        {"factor": 6.74, "n": 2, "spread": 0.01, "detail": []},
        {},
        {},
    ]
    for i, fx in enumerate(fx_per_shard):
        with open(os.path.join(shards, f"shard-{i:02d}.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"shard": i, "shards": 4, "round": 11, "ts": now,
                       "tried": 1, "err": "", "fx": fx,
                       "health": {"steam_orders": {"ok": True, "detail": "1/1"}},
                       "items": items if i == 0 else []},
                      f, ensure_ascii=False)

    real_factor = steam.market_factor
    asked = []

    def spy(sample, **kw):
        asked.append(sample)
        return {"factor": 9.99, "n": 1, "spread": 0.0, "detail": []}

    steam.market_factor = spy
    try:
        args = argparse.Namespace(
            mode="merge", db=os.path.join(tmp, "f.db"), shard=0, shards=4,
            batch=1, give_up=25, out_dir=shards, in_dir=shards,
            fx_samples=6, export_limit=50, within_hours=48, deadline=0)
        run.run_merge(args)
        d = json.load(open(os.path.join(tmp, "latest.json"), encoding="utf-8"))
        check("用采集阶段交上来的折算比（两台各标一次取中位数）",
              d.get("fx") is not None and abs(d["fx"] - 6.73) < 0.02,
              str(d.get("fx")))
        check("有 shard 的折算比时就不再当场问 Steam",
              asked == [], f"问了 {len(asked)} 次")
        hfx = d["health"].get("fx") or {}
        check("health 里写明折算比是从哪来的",
              hfx.get("ok") is True and "shard" in str(hfx.get("detail")),
              str(hfx.get("detail")))
    finally:
        steam.market_factor = real_factor
        run.DATA = old_data


def t_fx_reuse():
    """标定没成功时沿用上次测到的折算比，绝不轻易退回汇率。

    线上连跑三轮 fx 都失败（merge 一个 IP、采集阶段也只有 11/61 成功），
    退回汇率 7.1 会让所有人民币价偏高约 6%，方向正好是「让你以为更赚」。
    国区定价和汇率的差距变化很慢，昨天的折算比今天照样能用。
    """
    print("\n折算比沿用上次的值")
    import argparse
    import run

    tmp = tempfile.mkdtemp(prefix="arbfxreuse_")
    old_data = run.DATA
    run.DATA = tmp
    shards = os.path.join(tmp, "shards")
    os.makedirs(shards)

    now = int(time.time())
    items = [{"n": "Thing", "usd_bid": 1000, "usd_sell": 1400,
              "buy": 10.0, "src": "waxpeer", "qty": 5, "depth": 3,
              "blevels": 2, "buys": 400, "sells": 300, "mid": None,
              "ts": now}]
    args = argparse.Namespace(
        mode="merge", db=os.path.join(tmp, "f.db"), shard=0, shards=1,
        batch=1, give_up=25, out_dir=shards, in_dir=shards,
        fx_samples=0, export_limit=50, within_hours=48, deadline=0)

    def one_shard(fx):
        with open(os.path.join(shards, "shard-00.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"shard": 0, "shards": 1, "round": 12, "ts": now,
                       "tried": 1, "err": "", "fx": fx,
                       "health": {"steam_orders": {"ok": True, "detail": "1/1"}},
                       "items": items}, f, ensure_ascii=False)

    real_factor = steam.market_factor
    steam.market_factor = lambda s, **kw: {"factor": None, "n": 0}
    try:
        # 第一次：从没标定过 → 只能退回汇率，并且必须明说
        one_shard({})
        run.run_merge(args)
        d = json.load(open(os.path.join(tmp, "latest.json"), encoding="utf-8"))
        rate = d["rate_usd_cny"]
        check("从没标定过时退回汇率", d.get("fx") == rate, str(d.get("fx")))
        check("退回汇率时明说后果（涨约 6%）",
              "6%" in (d.get("fx_note") or ""), str(d.get("fx_note")))

        # 第二次：真标出来了 → 记下来
        one_shard({"factor": 6.73, "n": 4, "spread": 0.01,
                   "detail": [], "src": "shard"})
        run.run_merge(args)
        d = json.load(open(os.path.join(tmp, "latest.json"), encoding="utf-8"))
        check("标定成功时用标定值", abs((d.get("fx") or 0) - 6.73) < 0.01,
              str(d.get("fx")))
        check("标定成功时不带提示", not (d.get("fx_note") or ""),
              str(d.get("fx_note")))

        # 第三次：又没标出来 → 沿用 6.73，不是汇率
        one_shard({})
        run.run_merge(args)
        d = json.load(open(os.path.join(tmp, "latest.json"), encoding="utf-8"))
        check("没标出来时沿用上次的值，不退回汇率",
              abs((d.get("fx") or 0) - 6.73) < 0.01, str(d.get("fx")))
        check("沿用旧值时说清是最近一次测的",
              "最近一次" in (d.get("fx_note") or "")
              or "小时前" in (d.get("fx_note") or ""),
              str(d.get("fx_note")))
    finally:
        steam.market_factor = real_factor
        run.DATA = old_data


def main():
    t_fees()
    t_price()
    t_orderbook()
    t_factor()
    t_store()
    t_migrate()
    t_merge()
    t_merge_real()
    t_sanity()
    t_shard_fx()
    t_fx_reuse()
    t_shards()
    t_deadline()
    t_fake()
    print("\n" + "=" * 46)
    print(f"通过 {len(OK)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  ✗", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
