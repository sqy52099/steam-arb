#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线自检：不联网，把各模块的连接处都跑一遍。

跑：python selftest.py
退出码 0 = 全部通过。
"""
import os
import sqlite3
import sys
import tempfile

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


def t_fees():
    print("\n手续费")
    # 大额应接近 ÷1.15
    n = fees.net_from_buyer(100.0)
    check("100 元净得 ≈ 86.97", abs(n - 86.97) < 0.01, f"得 {n}")
    check("100 元净得 > 86", n > 86, f"得 {n}")
    # 回推：net 必须是「买家付得起」里最大的那个
    # 手续费两处 floor，函数是阶梯，买家家付 P 可能落在两级之间，
    # 所以不能断言 buyer_pays(net) == P，只能断言它是不超过 P 的最大值。
    bad = []
    for pay in range(3, 20000):
        net = fees.net_from_buyer_cents(pay)
        if not (fees.buyer_pays_cents(net) <= pay
                < fees.buyer_pays_cents(net + 1)):
            bad.append((pay, net, fees.buyer_pays_cents(net),
                        fees.buyer_pays_cents(net + 1)))
    check("净得是买家付得起的最大值", not bad,
          f"{len(bad)} 处不符 {bad[:3]}")
    # 元为单位时差额必须小于一个档位（最多 3 分）
    gap = []
    for cents in range(3, 20000, 7):
        p = cents / 100.0
        n = fees.net_from_buyer(p)
        if abs(fees.buyer_pays(n) - p) > 0.031:
            gap.append((p, n, fees.buyer_pays(n)))
    check("元价位回推误差 < 3 分", not gap, f"{len(gap)} 处 {gap[:3]}")
    # 单调性
    prev = 0
    mono = True
    for p in range(3, 5000):
        n = fees.net_from_buyer_cents(p)
        if n < prev:
            mono = False
            break
        prev = n
    check("净得随买家付出单调不减", mono)
    # 费率随金额升高趋近 0.8696
    r_max = fees.fees_from_net(1000)["effective_rate"]
    check("大额有效费率 ≈ 86.96%", abs(r_max - 0.86957) < 0.0005, f"得 {r_max}")


def t_steam_parse():
    print("\n价格解析")
    cases = [("¥ 1.23", 1.23), ("¥1,234.56", 1234.56), ("1.234,56€", 1234.56),
             ("1,23€", 1.23), ("¥ 12 500,00", 12500.0), ("12,345", 12345.0),
             ("$3.50", 3.50), ("abc", None), ("", None), (None, None)]
    bad = []
    for raw, want in cases:
        got = steam._parse_price(raw)
        if want is None:
            if got is not None:
                bad.append((raw, got, want))
        elif got is None or abs(got - want) > 0.001:
            bad.append((raw, got, want))
    check("_parse_price 各格式", not bad, str(bad))

    html = ('<script>Market_LoadOrderSpread( 1760231 );</script>')
    m = steam.NAMEID_RE.search(html)
    check("nameid 正则", bool(m) and m.group(1) == "1760231")

    print("\n限流器")
    lim = steam.Limiter(gap=0.0, max_429=3)
    lim.backoff()
    check("一次 429 只退避不抛", lim.strikes == 1)
    try:
        lim.backoff(); lim.backoff()
        check("连续三次 429 抛 RateLimited", False)
    except steam.RateLimited:
        check("连续三次 429 抛 RateLimited", True)
    lim.ok()
    check("成功后计数清零", lim.strikes == 0)


def t_store():
    print("\n存储层")
    tmp = tempfile.mkdtemp(prefix="arbtest_")
    db = os.path.join(tmp, "t.db")
    con = store.connect(db)
    check("建表", con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] >= 3)

    store.upsert_items(con, [
        {"hash_name": "A", "volume": 500, "nameid": 11},
        {"hash_name": "B", "volume": 300, "nameid": None},
        {"hash_name": "C", "volume": 10,  "nameid": 33},
    ])
    check("items 写入 3 条", store.stats(con)["items"] == 3)

    # 重复写入不应翻倍，且 volume 会被更新
    store.upsert_items(con, [{"hash_name": "A", "volume": 900}])
    check("items 幂等", store.stats(con)["items"] == 3)
    v = con.execute("SELECT volume FROM items WHERE hash_name='A'").fetchone()[0]
    check("volume 被更新", v == 900, f"得 {v}")
    # nameid 不被 None 覆盖
    nid = con.execute("SELECT nameid FROM items WHERE hash_name='A'").fetchone()[0]
    check("nameid 不被空值覆盖", nid == 11, f"得 {nid}")

    rows = store.candidates(con, min_volume=30, limit=10)
    names = [r["hash_name"] for r in rows]
    check("候选池只收有 nameid 且成交量够的", names == ["A"], str(names))

    import time
    now = int(time.time())
    store.insert_quotes(con, [
        {"ts": now, "hash_name": "A", "sell_cny": 10, "bid_cny": 9,
         "net_cny": 8.7, "volume": 900, "src_buy_cny": 7, "src_name": "x",
         "ratio": 1.24},
        {"ts": now - 86400 * 40, "hash_name": "A", "sell_cny": 10, "bid_cny": 9,
         "net_cny": 8.7, "volume": 1, "src_buy_cny": 7, "src_name": "x",
         "ratio": 1.24},
    ])
    check("quotes 写入", store.stats(con)["quotes"] == 2)
    store.insert_quotes(con, [
        {"ts": now, "hash_name": "A", "sell_cny": 11, "bid_cny": 10,
         "net_cny": 9.5, "volume": 900, "src_buy_cny": 7, "src_name": "x",
         "ratio": 1.36},
    ])
    check("同 ts 主键替换而非重复", store.stats(con)["quotes"] == 2)
    latest = store.latest_quotes(con, limit=5)
    check("最新报价取到更新后的值", latest and abs(latest[0]["ratio"] - 1.36) < 1e-9,
          str([r["ratio"] for r in latest]))

    n = store.purge_old(con, keep_days=30)
    check("清理 40 天前的报价", n == 1 and store.stats(con)["quotes"] == 1, f"清了 {n}")

    store.meta_set(con, "rotation_cursor", 5)
    check("meta 读写", store.meta_get(con, "rotation_cursor") == "5")
    con.close()


def t_merge():
    print("\n第三方合并")
    wax = {"M4A1 | Test": {"buy_cny": 100.0, "steam_mid_cny": 150.0, "qty": 12,
                           "type": "Rifle", "rarity": "Covert"}}
    skin = {"M4A1 | Test": {"buy_cny": 95.0, "suggested_cny": 160.0, "qty": 3}}
    rows = thirdparty.merge(wax, skin)
    check("取两家更低的买入价", rows and rows[0]["buy_cny"] == 95.0
          and rows[0]["src"] == "skinport", str(rows))

    rows2 = thirdparty.merge(wax, {})
    check("只有一家时用那家", rows2 and rows2[0]["src"] == "waxpeer")

    junk = thirdparty.merge({"Sticker | Foo": {"buy_cny": 50, "steam_mid_cny": 60,
                                               "qty": 1, "type": "", "rarity": ""}}, {})
    check("剔除贴纸类", junk == [])

    cheap = thirdparty.merge({"X": {"buy_cny": 0.5, "steam_mid_cny": 1,
                                    "qty": 1, "type": "", "rarity": ""}}, {})
    check("剔除过低价格", cheap == [])

    check("is_junk 认 StatTrak", thirdparty.is_junk("StatTrak™ AK-47 | Redline"))


def t_schedule():
    """CI 时间预算 + 轮转游标：这两处错了会静默漏采，必须离线验。"""
    print("\n调度与游标")
    import run
    import time as _t

    run.set_deadline(0.002)          # 0.12 秒
    a = run.out_of_time()
    _t.sleep(0.2)
    b = run.out_of_time()
    run.set_deadline(0)
    c = run.out_of_time()
    check("deadline 到点前不喊停、到点后喊停、关掉不喊停",
          a is False and b is True and c is False, f"{a},{b},{c}")

    tmp = tempfile.mkdtemp(prefix="arbcur_")
    con = store.connect(os.path.join(tmp, "c.db"))
    store.upsert_items(con, [{"hash_name": f"X{i}", "volume": 100,
                              "nameid": i + 1} for i in range(10)])
    store.meta_set(con, "rotation_cursor", 0)

    b1 = [x["hash_name"] for x in run.pick_batch(con, 100, 4)]
    check("取批不推进游标",
          b1 == ["X0", "X1", "X2", "X3"]
          and store.meta_get(con, "rotation_cursor") == "0", str(b1))

    run.advance_cursor(con, 100, 2)   # 只采到 2 件
    b2 = [x["hash_name"] for x in run.pick_batch(con, 100, 4)]
    check("中断后从没采到的那件接上",
          b2[0] == "X2", f"得 {b2[0]}，应为 X2")

    store.meta_set(con, "rotation_cursor", 8)
    run.advance_cursor(con, 100, 4)   # 8+4=12 → 回绕成 2
    check("游标回绕不越界",
          store.meta_get(con, "rotation_cursor") == "2",
          f"得 {store.meta_get(con, 'rotation_cursor')}")
    con.close()


def main():
    t_fees()
    t_steam_parse()
    t_store()
    t_merge()
    t_schedule()
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
