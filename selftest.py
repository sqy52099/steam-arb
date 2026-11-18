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

    def fake(hn, currency=steam.CURRENCY_CNY, appid=steam.APPID):
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


# ---------------- 存储层 ----------------

def t_store():
    print("\n存储层")
    tmp = tempfile.mkdtemp(prefix="arbstore_")
    con = store.connect(os.path.join(tmp, "t.db"))
    check("建表", con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] >= 3)
    check("schema 版本写进 meta", store.meta_get(con, "schema_version") == "2")

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


def main():
    t_fees()
    t_price()
    t_orderbook()
    t_factor()
    t_store()
    t_migrate()
    t_merge()
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
