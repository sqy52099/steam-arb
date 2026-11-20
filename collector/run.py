#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采集编排。因为 Steam 对机房 IP 按接口区别对待（详见 steam.py 顶部说明），
一轮采集必须拆成两个阶段、跑在不同 job 里：

  collect（N 个 job 并行）
      每台机器只干一件便宜事：拿第三方候选名单 → 抓物品页 → 把盘口原样
      写成 shard-<序号>.json。不碰数据库，因为并行 job 之间没有共享状态。
      每个 job 出口 IP 不同、限流额度独立，这是整条链路唯一的放大器。

  merge（1 个 job）
      读完所有 shard、标定「国区人民币 ÷ 美元」折算比、算净得与面值比、
      写库、导出前端用的 latest.json。数据库只有它能写。

为什么不在 collect 阶段就算人民币：盘口给的是美元分，而 Steam 国区定价
系统性比真实汇率低 5~10%，用 7.1 直接乘会把价格高估、把亏的说成赚的。
折算比只能在 merge 阶段用 priceoverview 的美元报价 / 人民币报价标定。

--fake 不联网，两个阶段都用合成数据跑一遍，用于验证链路。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fees
import thirdparty
from store import (connect, day_key, hist_series, index_series, latest_quotes,
                   meta_get, meta_set, purge_hist, purge_old, stats,
                   upsert_hist, upsert_items, upsert_quotes, HIST_DAYS)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ARB_DATA 让 CI 把产物直接写进 data 分支的克隆目录
DATA = os.environ.get("ARB_DATA") or os.path.join(BASE, "data")
APPID = 730

# 轮次长度（秒）。要和 collect.yml 的 cron 对齐：同一轮里所有 shard 必须
# 算出同一个窗口，否则会重复采同一批物品、漏掉另一批。
ROUND_SECONDS = 1800

# 盘口可信度门槛（合并阶段用）。见 run_merge 里的注释说明为什么需要这两个。
MIN_SELLS = 10      # 挂单少于这么多件，最低挂单价不代表市场
MAX_DEV = 0.40      # 与第三方自报中间价偏离超过 40% 就判为不可信
# 预估面值比低于这个数就不值得花一次请求去核实。一次请求 2.5 秒，
# 而候选有六千多件，全查一遍要四个多小时 —— 只能先粗排再细查。
MIN_EST = 0.95

# 本轮采集的硬截止时间点（monotonic）。CI 上超时会被硬杀，那时一步都没
# 提交，整轮白跑；所以宁可少采一点也要自己先收工，把拿到的写出去。
_DEADLINE = None

# 各环节成败，跟着 latest.json 一起导出。CI 上采集失败也 exit 0（为了保住
# 旧数据），有了 health 汇总步骤才能分辨「Steam 被拦」和「本来没变化」。
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
    """真实汇率，只用来把 Waxpeer 的美元报价换成人民币买入价。
    不能用来换算 Steam 盘口 —— 那个要用 merge 阶段标定的折算比。"""
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


def round_index() -> int:
    return int(time.time() // ROUND_SECONDS)


def est_ratio(c: dict) -> float | None:
    """用第三方自报的 Steam 中间价，粗略估一下这件值不值得查。

    这个数不是最终结果 —— 中间价是「最近成交价」的中位，不是此刻能立刻
    卖掉的最优求购价，通常偏高一点，而且中间价本身有延迟。它的用处只有
    一个：把六千多件排个序，别把请求浪费在明显不划算的东西上。

    真正的盘口还是要查物品页才知道。这里估多少不重要，排对顺序才重要。
    """
    mid = c.get("steam_mid_cny") or 0
    buy = c.get("buy_cny") or 0
    if mid <= 0 or buy <= 0:
        return None
    net = fees.net_from_buyer(mid)
    return net / buy


def pick_candidates(cand: dict[str, dict]) -> list[str]:
    """先粗排，只留值得花请求的那一批。

    线上教训：六千多件候选照单全查，一轮要四个多小时，而每个 job 的
    时间预算只有十二分钟 —— 结果是每轮只查到清单最前面那几十件（按名字
    排序，等于随机），好东西一件都碰不到。排序之后再查，等于把有限的
    请求全花在最可能有赚头的那些上。
    """
    scored = []
    for name, c in cand.items():
        r = est_ratio(c)
        if r is None or r < MIN_EST:
            continue
        scored.append((r, name))
    scored.sort(reverse=True)
    return [n for _, n in scored]


def build_est_rows(cand: dict[str, dict], real_names: set[str],
                   cap: int = 800) -> list[dict]:
    """没有真实盘口的物品，用第三方自报的中间价估一个面值比。

    这是「Steam 完全拿不到」时的兜底，**不能当成真价**：中间价是最近成交
    价的中位、不是此刻能秒卖的求购价，通常偏高 10~20%；第三方自己也有
    延迟。所以这些行导出时一律带 est=1，前端单独标成「估」、排在实测行
    之后 —— 混在一起会让人把估算当成真机会。

    真正的价值在于「有个能看的榜单」：实测只有几十件，而候选池有六千多，
    估算虽然不准，至少能把明显不划算的排掉。
    """
    now = int(time.time())
    rows = []
    for name, c in cand.items():
        if name in real_names:
            continue
        buy = c.get("buy_cny") or 0
        mid = c.get("steam_mid_cny") or 0
        if buy <= 0 or mid <= 0:
            continue
        net = fees.net_from_buyer(mid)
        ratio = net / buy
        if ratio < MIN_EST:
            continue
        rows.append({
            "n": name,
            "sell": round(mid, 2),
            "bid": None,
            "bid3": None,
            "net": round(net, 2),
            "net_sell": round(net, 2),
            "buy": round(buy, 2),
            "src": c.get("src"),
            "qty": c.get("src_qty") or 0,
            "vol": None,
            "bsp": c.get("buy_skinport"), "bwp": c.get("buy_waxpeer"),
            "qsp": c.get("qty_skinport"), "qwp": c.get("qty_waxpeer"),
            "depth": None,
            "orders": None,
            "sell_orders": None,
            "ratio": round(ratio, 4),
            "ratio3": None,
            "ratio_sell": None,
            "ts": now,
            "est": 1,
        })
    rows.sort(key=lambda r: -r["ratio"])
    return rows[:cap]


def shard_slice(names: list[str], shard: int, shards: int,
                batch: int) -> list[str]:
    """按时间算出本轮该采哪一段。

    不用数据库里的游标：collect 阶段是 N 个并行 job、彼此看不到状态，
    读到的游标随时会过期。用「当前轮次」推位置是纯函数，同一个 job 内
    结果稳定，跨 job 也能对齐。
    """
    if not names or batch <= 0 or shards <= 0:
        return []
    pool = len(names)
    per_round = shards * batch
    if per_round >= pool:
        # 一轮就能扫完整个池子：shard 之间均分，不必轮转
        seg = names[shard::shards]
        return seg[:batch]
    start = (round_index() * per_round) % pool
    rot = names[start:] + names[:start]
    return rot[shard * batch:(shard + 1) * batch]


# ---------------- collect 阶段 ----------------

def fetch_candidates(log_prefix: str = "") -> dict[str, dict] | None:
    """第三方买入价。两个站都失败才返回 None。"""
    rate = usd_cny()
    wax, skin = {}, {}
    try:
        wax = thirdparty.fetch_waxpeer(rate)
        log(f"{log_prefix}Waxpeer  {len(wax)} 条")
    except Exception as e:
        log(f"{log_prefix}Waxpeer 失败：{e}")
    try:
        skin = thirdparty.fetch_skinport()
        log(f"{log_prefix}Skinport {len(skin)} 条")
    except Exception as e:
        log(f"{log_prefix}Skinport 失败：{e}")
    if not wax and not skin:
        mark("thirdparty", False, "Skinport 和 Waxpeer 都失败")
        return None
    rows = thirdparty.merge(wax, skin)
    mark("thirdparty", True,
         f"waxpeer {len(wax)} / skinport {len(skin)} / 合并 {len(rows)}")
    return {r["name"]: r for r in rows}


def run_collect(args) -> None:
    """抓一批物品页盘口，写 shard JSON。不连数据库。"""
    import steam as st

    cand = fetch_candidates(f"shard{args.shard} ")
    if cand is None:
        _write_shard(args, [], "两家第三方都拿不到数据")
        return

    names = pick_candidates(cand)
    log(f"第三方给了 {len(cand)} 个候选，粗排后剩 {len(names)} 件值得查")
    # 只在前 N 名里轮转。再往后预估越来越不准、盘口也越来越薄，
    # 花请求去看它们不划算 —— 而不设上限的话，排名靠前的每轮都被
    # 往后推很远，等于好货几小时才轮到一次。
    if args.pool_cap > 0 and len(names) > args.pool_cap:
        log(f"  只取预估最好的前 {args.pool_cap} 件参与轮转")
        names = names[:args.pool_cap]
    picked = shard_slice(names, args.shard, args.shards, args.batch)
    log(f"分给 shard{args.shard} 的 {len(picked)} 件")
    if not picked:
        _write_shard(args, [], "")
        return

    items = []
    tried = 0        # 真发过请求的件数
    ok = 0
    throttled = 0
    netfail = 0
    last_err = ""
    for hn in picked:
        if out_of_time():
            log("  时间预算用完，提前收工")
            break
        try:
            ob = st.item_orderbook(hn, APPID)
        except st.NetworkFailed as e:
            tried += 1
            netfail += 1
            last_err = f"{hn}: {e}"
            # 连不上和限流不是一回事：429 睡一会还能等到恢复窗口，
            # 连不上就只是这台机器到不了 Steam。线上出现过四个 shard
            # 全部 0 成功、artifacts 只有 386 字节 —— 全是这样烧掉的。
            if netfail >= args.net_give_up:
                log(f"  连续 {netfail} 次连不上 Steam，收工")
                break
            continue
        except st.RateLimited as e:
            tried += 1
            throttled += 1
            last_err = f"{hn}: {e}"
            # 被限流不代表这批没救：间隔会自己拉长，恢复后还能继续拿。
            # 只有连续失败到预算见底才走不下去。
            if throttled >= args.give_up:
                log(f"  连续 {throttled} 次被限流，收工")
                break
            continue
        except st.ParseError as e:
            tried += 1
            last_err = f"{hn}: {e}"
            continue
        except Exception as e:
            tried += 1
            last_err = f"{hn}: {e}"
            continue
        tried += 1
        throttled = 0
        netfail = 0
        c = cand[hn]
        items.append({
            "n": hn,
            "usd_bid": ob["usd_bid"], "usd_sell": ob["usd_sell"],
            "usd_bid3": ob.get("usd_bid3"),
            "buy": c["buy_cny"], "src": c["src"], "qty": c.get("src_qty") or 0,
            # 平台分价：没有报价的写 None（前端显示「—」），别写成 0
            "bsp": c.get("buy_skinport"), "bwp": c.get("buy_waxpeer"),
            "qsp": c.get("qty_skinport"), "qwp": c.get("qty_waxpeer"),
            # 第三方自报的 Steam 中间价（人民币）。合并阶段用它做合理性校验：
            # 物品页的最低挂单价在「挂单只剩一两件」时会离谱得离谱，得有个
            # 独立来源对一下才知道那个盘口能不能信。
            "mid": c.get("steam_mid_cny"),
            "depth": ob["bid_depth"], "blevels": ob["bid_levels"],
            "buys": ob["buy_orders"], "sells": ob["sell_orders"],
            "ts": int(time.time()),
        })
        ok += 1
        if ok % 20 == 0:
            log(f"  已采 {ok}/{len(picked)}")

    detail = f"{ok}/{tried} 成功" + (f" · {last_err}" if last_err else "")
    # 「连不上」不算通。发出去过请求不等于拿到回音 —— 线上第五轮四个
    # shard 全 0 成功，但 tried 是正数，标成「通」会让人以为接口没问题。
    reachable = tried - netfail
    mark("steam_orders", ok > 0 or reachable > 0, detail)
    log(f"shard{args.shard} 完成：{detail}")

    # 顺手标定一份折算比。merge 是单独一个 job、只有一个出口 IP，撞上
    # 限流就整轮拿不到折算比 —— 线上连着两轮都是这样，只好退回汇率，
    # 所有人民币价偏高约 6%。而每个 collect job 有自己的 IP，四个都试
    # 一遍，总有一个能通。取最接近中位数的那份，各 shard 的结果落在
    # merge 里再挑。
    fx_marks = [it["n"] for it in sorted(
                    [it for it in items if (it.get("usd_sell") or 0) >= 100],
                    key=lambda x: -(x.get("usd_sell") or 0))][:args.fx_samples]
    fx_info = {"factor": None, "n": 0}
    if fx_marks:
        try:
            fx_info = st.market_factor(fx_marks, max_seconds=60)
        except Exception as e:
            fx_info = {"factor": None, "n": 0}
            log(f"  折算比标定抛异常：{e}")

    # 标定那几件顺便会把 24 小时成交量带回来（同一个接口的 volume 字段，
    # 不另发请求）。挂到对应的件上：多一件真成交量，就少一处「—」。
    vols = (fx_info or {}).get("vols") or {}
    if vols:
        hit = 0
        for it in items:
            v = vols.get(it["n"])
            if v:
                it["vol"] = v
                hit += 1
        log(f"  顺带拿到 {hit} 件的 24 小时成交量")
    _write_shard(args, items, "", fx_info)


def _write_shard(args, items: list[dict], err: str,
                 fx_info: dict | None = None) -> None:
    d = args.out_dir
    os.makedirs(d, exist_ok=True)
    fp = os.path.join(d, f"shard-{args.shard:02d}.json")
    out = {
        "shard": args.shard, "shards": args.shards,
        "round": round_index(), "ts": int(time.time()),
        "tried": len(items), "err": err,
        "health": _HEALTH,
        "fx": fx_info or {},
        "items": items,
    }
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, fp)
    log(f"写出 {fp}（{len(items)} 件，{os.path.getsize(fp)/1024:.1f} KB）")


# ---------------- merge 阶段 ----------------

def load_shards(in_dir: str) -> tuple[list[dict], dict, int]:
    """读回所有 shard。返回 (物品列表, 合并的 health, 本轮 round)。"""
    items, health, rnd, _ = load_shards_fx(in_dir)
    return items, health, rnd


def load_shards_fx(in_dir: str) -> tuple[list[dict], dict, int, list[dict]]:
    """同上，另外把各 shard 交上来的折算比样本一并带回来。"""
    files = sorted(glob.glob(os.path.join(in_dir, "shard-*.json")))
    items: list[dict] = []
    health: dict = {}
    rounds: list[int] = []
    fxinfos: list[dict] = []
    for fp in files:
        try:
            with open(fp, encoding="utf-8") as f:
                j = json.load(f)
        except Exception as e:
            log(f"跳过 {os.path.basename(fp)}：{e}")
            continue
        rows = j.get("items") or []
        items.extend(rows)
        rounds.append(int(j.get("round") or 0))
        fx = j.get("fx") or {}
        if fx.get("factor"):
            fxinfos.append(fx)
        for k, v in (j.get("health") or {}).items():
            # 任何一台机器失败就记失败，不要被后面的成功覆盖
            if k not in health or not health[k].get("ok"):
                health[k] = v
        log(f"{os.path.basename(fp)}  {len(rows)} 件")
    rnd = max(rounds) if rounds else 0
    return items, health, rnd, fxinfos


def book_trusted(sell_cny, mid, sells) -> str:
    """盘口能不能信。返回 ""（可信）或丢弃理由。

    最低挂单价只在「有足够多人挂着」时才代表市场价。实测反面例子：
      ★ Talon Knife | Safari Mesh (Factory New) 挂单只剩 1 件，
      amtMinSellOrder 报到 $13.92，而 Waxpeer 自报中间价 $3.33，差 4 倍
      —— 第一轮真跑里 ratio 3.03 那条「机会」就是这么来的。

    这个判断必须**写入和读取都做一遍**：每轮只采几十件，表里五千多件都
    是往轮留下的，只在写入时过滤的话，上一轮的脏行还会从 latest_quotes
    里被导出来，页面上照样挂着假机会。
    """
    if sells is not None and sells < MIN_SELLS:
        return "没卖单"
    if mid and sell_cny and abs(sell_cny - mid) / mid > MAX_DEV:
        return "偏离参考价"
    return ""


def run_merge(args) -> None:
    """把 shard 结果折成人民币、算面值比、写库、导出。"""
    items, health, rnd, shard_fx = load_shards_fx(args.in_dir)
    _HEALTH.update(health)

    con = connect(args.db)

    # 兜底：Steam 那边一件都拿不到时（出口被限流，线上连续十几轮都是这样），
    # 页面不能空着。用第三方自报的中间价估一份，导出的每行带 est=1。
    # 放在 early return 之前算 —— 「没有任何 shard 数据」恰恰是最需要它的场景。
    est_rows: list[dict] = []
    if getattr(args, "est_cap", 0) > 0:
        cand = fetch_candidates("估 ")
        if cand:
            real = {it["n"] for it in items}
            est_rows = build_est_rows(cand, real, args.est_cap)
            log(f"另用第三方中间价估了 {len(est_rows)} 件"
                + ("（Steam 那边一件真实盘口都没拿到）" if not items else ""))
            mark("est", True, f"{len(est_rows)} 件按中间价估，没有真实盘口")
        else:
            mark("est", False, "第三方候选也拿不到，只能导出旧数据")

    if not items:
        mark("merge", False, "没有任何 shard 数据")
        log("没有可合并的数据，保留旧库")
        export(args, con, est_rows)
        return

    # 折算比：先看采集阶段的 shard 交上来的，不得已才在这里现问。
    #
    # 为什么不在 merge 里问：merge 是单独一个 job，只有一个出口 IP，撞上
    # Steam 限流就整轮拿不到 —— 线上连着两轮都是「标定失败，退回汇率」，
    # 于是所有人民币价偏高约 6%。而 4 个 collect job 各有自己的 IP，
    # 让它们顺手各标一份，总数里挑中位数，成功率高得多。
    import steam as st
    fx_info: dict = {}
    if shard_fx:
        vals = sorted(float(f["factor"]) for f in shard_fx)
        fx_info = {
            "factor": round(vals[len(vals) // 2], 4),
            "n": sum(int(f.get("n") or 0) for f in shard_fx),
            "spread": round((vals[-1] - vals[0]) / vals[len(vals) // 2], 4)
                      if len(vals) > 1 and vals[len(vals) // 2] else None,
            "src": "shard",
            "detail": shard_fx,
        }
        log(f"折算比取自采集阶段（{len(shard_fx)} 台各标了一次）")

    fx = fx_info.get("factor")
    if not fx and args.fx_samples > 0:
        sample = [it["n"] for it in sorted(
                      [it for it in items if (it.get("usd_sell") or 0) >= 100],
                      key=lambda x: -(x.get("usd_sell") or 0))][:args.fx_samples]
        fx_info = st.market_factor(sample) if sample else {"factor": None, "n": 0}
        fx = fx_info.get("factor")
        if fx:
            log("采集阶段没给出折算比，这里现问 Steam 才拿到")

    fx_note = ""
    if fx:
        mark("fx", True,
             f"{fx} ×{fx_info['n']} 样本 · 来自{fx_info.get('src') or 'merge'}")
        log(f"折算比 1 USD = ¥{fx}（{fx_info['n']} 个样本）")
    else:
        # 本轮没标出来。**不要立刻退回汇率** —— 汇率 7.1 比 Steam 国区定价
        # 高约 6%，用它算出来的每个数都偏乐观，方向正好是「让你以为更赚」。
        # 先沿上一次真正测到的值：国区定价和汇率的差距变化很慢，昨天的
        # 折算比今天照样能用；沿用的时候说清楚是哪天的，别让人以为是新的。
        # 必须读 fx_cal（真标出来的值），不能读 fx —— fx 会被退回汇率的值
        # 覆盖，读它等于把汇率当成「上次测到的」，那和直接退回汇率没区别。
        prev = None
        prev_ts = None
        try:
            prev = float(meta_get(con, "fx_cal") or 0) or None
            prev_ts = int(meta_get(con, "fx_cal_ts") or 0) or None
        except Exception:
            pass
        if prev:
            fx = prev
            if prev_ts:
                age_h = int((time.time() - prev_ts) / 3600)
                when = "刚刚" if age_h < 1 else f"{age_h} 小时前"
            else:
                when = "上一次"
            mark("fx", False,
                 f"本轮没标定出来，沿用{when}的 {fx}（国区定价变化很慢，能用）")
            log(f"折算比本轮没标定出来，沿用{when}的 {fx}")
            fx_note = (f"本轮没能标定折算比，用的是{when}测到的 {fx}"
                       if when != "刚刚" else
                       f"本轮没能标定折算比，用的是最近一次测到的 {fx}")
        else:
            fx = usd_cny()
            mark("fx", False, f"从没标定成功过，退回汇率 {fx}（价格偏高约 6%）")
            log(f"折算比从没标定成功过，退回真实汇率 {fx}")
            fx_note = (f"折算比一次都没标定成功过，现在用真实汇率 {fx} 换算，"
                       "比 Steam 国区实际定价高约 6%，真实收益会比这里低")

    rows = []
    now = int(time.time())
    skipped = 0
    dropped = {"没卖单": 0, "偏离参考价": 0}
    for it in items:
        ub, us = it.get("usd_bid"), it.get("usd_sell")
        if not ub:
            skipped += 1
            continue
        bid_cny = round(ub / 100.0 * fx, 2)
        sell_cny = round(us / 100.0 * fx, 2) if us else None

        why = book_trusted(sell_cny, it.get("mid"), it.get("sells"))
        if why:
            dropped[why] += 1
            continue

        buy = it.get("buy")
        net = fees.net_from_buyer(bid_cny)              # 挂求购价，立刻成交
        net_sell = fees.net_from_buyer(sell_cny) if sell_cny else None
        # 第三档求购价：买盘不足三档时 collect 那边会退到最后一档，也可能是
        # 没抓到 —— 那就留空，前端这一口径的折显示「—」，不要拿最高价顶，
        # 那等于把「一件的运气」当成「几件的价」。
        b3 = it.get("usd_bid3")
        bid3_cny = round(b3 / 100.0 * fx, 2) if b3 else None
        net3 = fees.net_from_buyer(bid3_cny) if bid3_cny else None
        rows.append({
            "hash_name": it["n"], "ts": it.get("ts") or now,
            "first_ts": now,
            "sell_cny": sell_cny, "bid_cny": bid_cny, "bid3_cny": bid3_cny,
            "net_cny": net, "net3_cny": net3, "net_sell_cny": net_sell,
            "usd_bid": ub, "usd_sell": us, "fx": fx,
            "bid_depth": it.get("depth"), "bid_levels": it.get("blevels"),
            "sell_orders": it.get("sells"), "buy_orders": it.get("buys"),
            "src_buy_cny": buy, "src_name": it.get("src"),
            "src_qty": it.get("qty"),
            # 两家平台各自报的价，给前端「多平台同屏」用
            "vol": it.get("vol"),
            "buy_sp": it.get("bsp"), "buy_wp": it.get("bwp"),
            "qty_sp": it.get("qsp"), "qty_wp": it.get("qwp"),
            "ratio": round(net / buy, 4) if buy else None,
            "ratio3": round(net3 / buy, 4) if (buy and net3) else None,
            "ratio_sell": round(net_sell / buy, 4)
                          if (buy and net_sell) else None,
            "src_mid_cny": it.get("mid"),      # 导出时还要用它复筛一遍
        })

    # 同一件在本轮里可能被多个 shard 各采到一次（切片按名字排序切，正常
    # 不重叠，但 artifact 重跑或手工补采会撞上）。只留求购价最高的那条：
    # 盘口本身会抖几个点，可一旦撞上「同页抓错对象」那种脏数据，差值会是
    # 几倍。留低的会把真正能赚的漏掉，所以留高的——脏数据要靠上面那道
    # 盘口可信度门槛挡，不能靠这里猜。
    best: dict[str, dict] = {}
    for r in rows:
        k = r["hash_name"]
        cur = best.get(k)
        if cur is None or (r.get("usd_bid") or 0) > (cur.get("usd_bid") or 0):
            best[k] = r
    if len(best) != len(rows):
        log(f"本轮有 {len(rows) - len(best)} 条重复物品，按求购价取优后剩 {len(best)} 条")
        rows = list(best.values())

    n = upsert_quotes(con, rows)
    upsert_items(con, [{"hash_name": it["n"]} for it in items])
    note = (f"{n} 条入库 / {len(items)} 件采到 / round {rnd}"
            + (f" / {skipped} 件无求购价跳过" if skipped else "")
            + (f" / 丢掉 {dropped['没卖单']} 件挂单太少"
               if dropped["没卖单"] else "")
            + (f" / 丢掉 {dropped['偏离参考价']} 件偏离参考价"
               if dropped["偏离参考价"] else ""))
    mark("merge", True, note)
    log(f"入库 {n} 条（{skipped} 件没有求购价，跳过）")
    if dropped["没卖单"] or dropped["偏离参考价"]:
        log(f"丢掉不可信盘口：挂单太少 {dropped['没卖单']} 件、"
            f"偏离参考价 {dropped['偏离参考价']} 件")

    gone = purge_old(con, 7)
    if gone:
        log(f"清理 7 天前没再出现的 {gone} 条")
    # 按天记一笔，给前端画涨跌。一天里跑多少轮都只覆盖当天那一行。
    hn = upsert_hist(con, rows)
    if hn:
        log(f"历史表记下 {hn} 条（{time.strftime('%Y-%m-%d', time.localtime(day_key() + 12*3600))}）")
    hgone = purge_hist(con, HIST_DAYS)
    if hgone:
        log(f"清理 {HIST_DAYS} 天前的历史 {hgone} 条")
    meta_set(con, "last_merge", int(time.time()))
    meta_set(con, "fx", fx)
    if fx_info.get("factor"):
        # 只有真标出来的才更新 fx_cal，沿用的旧值不该把自己刷新成「刚测的」。
        # fx 存本轮实际用的值（可能是沿用的），fx_cal 只存标定成功的值。
        meta_set(con, "fx_cal", fx_info["factor"])
        meta_set(con, "fx_cal_ts", int(time.time()))
        meta_set(con, "fx_src", fx_info.get("src") or "merge")
    meta_set(con, "fx_note", fx_note)
    log(f"库状态：{stats(con)}")
    export(args, con, est_rows)


def export(args, con, est_rows: list[dict] | None = None) -> None:
    """给前端用的静态 JSON：只放排序后的前 N 条，控制体积。

    est_rows 是「没查到真实盘口、只能用第三方中间价估」的那些行。它们
    跟实测行放在同一个 items 里（前端才有东西可看），但每行都带 est=1，
    前端据此单独标出来、排在实测行后面。
    """
    rows = latest_quotes(con, appid=APPID, within_hours=args.within_hours,
                         limit=args.export_limit)

    # 读取时再过滤一遍盘口。写入端已经挡过一次，但表里绝大多数行是往轮
    # 留下的（一轮只采几十件，候选池五千多），它们从来没经过现在的门槛。
    # 只靠写入端筛，上一轮的脏行会照着旧数一直挂在页面上。
    keep = []
    stale_bad = 0
    for r in rows:
        if book_trusted(r.get("sell_cny"), r.get("src_mid_cny"),
                        r.get("sell_orders")):
            stale_bad += 1
            continue
        keep.append(r)
    if stale_bad:
        log(f"导出时又挡掉 {stale_bad} 条往轮留下的不可信盘口")
    rows = keep
    fx = None
    try:
        fx = float(meta_get(con, "fx"))
    except Exception:
        pass

    # 挂刀指数：每天全站中位折扣。参考站首页左上角那个数就是它，
    # 但我们不用另外采 —— hist 表里每件每天的 ratio 已经攒着了。
    try:
        idx = index_series(con, HIST_DAYS, appid=APPID)
    except Exception as e:
        log(f"挂刀指数算不出来：{e}")
        idx = {"days": HIST_DAYS, "series": [], "floor": 0}
    # 今天的点用当前这批行情补一个（hist 要等 merge 写完才有当天行，
    # 而导出的这一刻手上就有全部行，直接算更准）。
    today = day_key()
    cur_disc = sorted((r["src_buy_cny"] / r["net_cny"]
                       for r in rows
                       if r.get("net_cny") and r.get("src_buy_cny")))
    if cur_disc:
        mid = cur_disc[len(cur_disc) // 2]
        idx["series"] = [p for p in idx["series"] if p[0] != today]
        idx["series"].append([today, round(mid, 4), len(cur_disc)])
        idx["series"].sort()
    idx["now"] = round(cur_disc[len(cur_disc) // 2], 4) if cur_disc else None

    out = {
        "generated": int(time.time()),
        "rate_usd_cny": usd_cny(),
        "fx": fx,
        # 折算比不理想时给前端一句人话，空字符串表示没问题。分成「沿用旧值」
        # 和「退回汇率」两种情况——前者差几个点，后者差 6% 且方向危险。
        "fx_note": (meta_get(con, "fx_note") or ""),
        "currency": "CNY",
        "count": len(rows) + len(est_rows or []),
        "real": len(rows),
        "est": len(est_rows or []),
        "index": idx,
        "note": ("折 = 买入价 ÷ 卖掉的净得余额，7.9 折就是花 79 元拿到 100 元余额，越低越划算；"
                 "「稳定求购」按第三档求购价算，「寄售」按最低挂单价算（要等有人买）。"
                 "余额不能提现，折成现金还要再乘 0.75~0.85。"),
        "health": _HEALTH,
        "items": [{
            "n": r["hash_name"],
            "sell": r["sell_cny"],
            "bid": r["bid_cny"],
            "bid3": r["bid3_cny"],
            "net": r["net_cny"],
            "net3": r["net3_cny"],
            "net_sell": r["net_sell_cny"],
            "buy": r["src_buy_cny"],
            "src": r["src_name"],
            "qty": r["src_qty"],
            "vol": r["vol"],
            "bsp": r["buy_sp"], "bwp": r["buy_wp"],
            "qsp": r["qty_sp"], "qwp": r["qty_wp"],
            "depth": r["bid_depth"],
            "orders": r["buy_orders"],
            "sell_orders": r["sell_orders"],
            "ratio": r["ratio"],
            "ratio3": r["ratio3"],
            "ratio_sell": r["ratio_sell"],
            "ts": r["ts"],
            "est": 0,
        } for r in rows] + list(est_rows or []),
    }
    os.makedirs(DATA, exist_ok=True)
    fp = os.path.join(DATA, "latest.json")
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, fp)
    log(f"导出 {fp}（{len(rows)} 条实测 + {len(est_rows or [])} 条估算，"
        f"{os.path.getsize(fp)/1024:.1f} KB）")

    # 按天历史单独一个文件：只有进「我的库存」页才需要它，
    # 混进 latest.json 会让首屏多下几百 KB，而绝大多数人根本不看那个页签。
    hist = hist_series(con, days=HIST_DAYS)
    hp = os.path.join(DATA, "hist.json")
    htmp = hp + ".tmp"
    with open(htmp, "w", encoding="utf-8") as f:
        json.dump({"days": HIST_DAYS, "series": hist}, f,
                  ensure_ascii=False, separators=(",", ":"))
    os.replace(htmp, hp)
    if hist:
        pts = sum(len(v) for v in hist.values())
        log(f"导出 {hp}（{len(hist)} 件 / {pts} 个点，"
            f"{os.path.getsize(hp)/1024:.1f} KB）")
    else:
        log("历史表还没有两天以上的数据，hist.json 是空的（正常，跑几天就有了）")


# ---------------- fake：离线跑通两个阶段 ----------------

def run_fake(args) -> None:
    rnd = random.Random(42)
    d = args.out_dir or os.path.join(DATA, "_fake_shards")
    os.makedirs(d, exist_ok=True)
    per = max(1, args.batch)
    for s in range(max(1, args.shards)):
        items = []
        for i in range(per):
            k = s * per + i
            buy = round(rnd.uniform(3, 400), 2)
            usd_sell = int(round(buy * rnd.uniform(1.0, 1.45) / 6.8 * 100))
            usd_bid = int(round(usd_sell * rnd.uniform(0.75, 0.98)))
            usd_bid3 = int(round(usd_bid * rnd.uniform(0.90, 0.995)))
            items.append({
                "n": f"Fake Item {k:03d}",
                "usd_bid": usd_bid, "usd_sell": usd_sell,
                "usd_bid3": usd_bid3,
                "buy": buy, "src": "fake", "qty": rnd.randint(1, 50),
                # 平台分价：让两家都报，其中一家正好是买入价
                "bsp": buy if rnd.random() < 0.6 else round(buy * rnd.uniform(1.01, 1.2), 2),
                "bwp": buy if rnd.random() < 0.6 else round(buy * rnd.uniform(1.01, 1.2), 2),
                "qsp": rnd.randint(0, 40), "qwp": rnd.randint(0, 40),
                "vol": rnd.choice([0, None, rnd.randint(1, 2000)]),
                "depth": rnd.randint(0, 60), "blevels": rnd.randint(1, 20),
                "buys": rnd.randint(1, 900), "sells": rnd.randint(1, 900),
                "ts": int(time.time()),
            })
        fp = os.path.join(d, f"shard-{s:02d}.json")
        with open(fp, "w", encoding="utf-8") as f:
            json.dump({"shard": s, "shards": args.shards,
                       "round": round_index(), "ts": int(time.time()),
                       "tried": len(items), "err": "",
                       "health": {"thirdparty": {"ok": True, "detail": "fake"},
                                  "steam_orders": {"ok": True, "detail": "fake"}},
                       "fx": {"factor": 6.80, "n": 5, "spread": 0.01,
                              "detail": [], "src": "fake"},
                       "items": items}, f, ensure_ascii=False)
    log(f"[fake] 写了 {args.shards} 个 shard 到 {d}")
    # merge 阶段：不联网，折算比用固定值
    args.in_dir = d
    args.fx_samples = 0
    merge_fake(args)


def merge_fake(args) -> None:
    items, health, rnd = load_shards(args.in_dir)
    _HEALTH.update(health)
    con = connect(args.db)
    fx = 6.80
    rows = []
    now = int(time.time())
    for it in items:
        bid_cny = round(it["usd_bid"] / 100.0 * fx, 2)
        sell_cny = round(it["usd_sell"] / 100.0 * fx, 2)
        b3 = it.get("usd_bid3")
        bid3_cny = round(b3 / 100.0 * fx, 2) if b3 else None
        net = fees.net_from_buyer(bid_cny)
        net_sell = fees.net_from_buyer(sell_cny)
        net3 = fees.net_from_buyer(bid3_cny) if bid3_cny else None
        rows.append({
            "hash_name": it["n"], "ts": it["ts"], "first_ts": now,
            "sell_cny": sell_cny, "bid_cny": bid_cny, "bid3_cny": bid3_cny,
            "net_cny": net, "net3_cny": net3, "net_sell_cny": net_sell,
            "usd_bid": it["usd_bid"], "usd_sell": it["usd_sell"], "fx": fx,
            "bid_depth": it["depth"], "bid_levels": it["blevels"],
            "sell_orders": it["sells"], "buy_orders": it["buys"],
            "src_buy_cny": it["buy"], "src_name": it["src"],
            "src_qty": it["qty"],
            "vol": it.get("vol"),
            "buy_sp": it.get("bsp"), "buy_wp": it.get("bwp"),
            "qty_sp": it.get("qsp"), "qty_wp": it.get("qwp"),
            "ratio": round(net / it["buy"], 4),
            "ratio3": round(net3 / it["buy"], 4) if net3 else None,
            "ratio_sell": round(net_sell / it["buy"], 4),
        })
    upsert_quotes(con, rows)
    upsert_items(con, [{"hash_name": it["n"]} for it in items])
    # 造假数据时顺手往前补几天历史，否则离线看涨跌线永远是空的
    today = day_key()
    for back in range(4, 0, -1):
        fake_hist = []
        for r in rows:
            wobble = 1 + (hash(r["hash_name"] + str(back)) % 17 - 8) / 100.0
            fake_hist.append({**r, "net_cny": round(r["net_cny"] * wobble, 2)})
        upsert_hist(con, fake_hist, ts=today - back * 86400)
    upsert_hist(con, rows)
    mark("fx", True, "fake")
    mark("merge", True, f"fake {len(rows)} 条")
    meta_set(con, "fx", fx)

    # 造几条「只有估算、没有真实盘口」的行，离线也能验前端怎么标它们
    est_rows: list[dict] = []
    if getattr(args, "est_cap", 0) > 0:
        fake_rnd = random.Random(7)
        cand = {}
        for i in range(12):
            buy = round(fake_rnd.uniform(4, 300), 2)
            cand[f"Est Only {i:03d}"] = {
                "buy_cny": buy,
                "steam_mid_cny": round(buy * fake_rnd.uniform(1.0, 1.4), 2),
                "src": "fake-est", "src_qty": fake_rnd.randint(1, 40),
            }
        est_rows = build_est_rows(cand, {it["n"] for it in items}, args.est_cap)
        mark("est", True, f"{len(est_rows)} 件按中间价估（fake）")

    log(f"[fake] 入库 {len(rows)} 条，历史补了 5 天，估算 {len(est_rows)} 条")
    export(args, con, est_rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("collect", "merge", "fake"),
                    default="fake")
    ap.add_argument("--db", default="")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=4, help="总共有几个并行 job")
    ap.add_argument("--batch", type=int, default=140, help="每个 shard 采几件")
    ap.add_argument("--pool-cap", type=int, default=400,
                    help="粗排后最多取前多少件参与轮转（0 = 不设上限）。"
                         "设上限是为了让好货每轮都能轮到，而不是被轮转推得很远")
    ap.add_argument("--give-up", type=int, default=12,
                    help="连续被限流多少次就放弃本 shard")
    ap.add_argument("--net-give-up", type=int, default=5,
                    help="连续多少次连不上 Steam 就放弃本 shard（和限流分开，"
                         "连不上等再久也没用，只会把整轮预算烧光）")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--in-dir", default="")
    ap.add_argument("--fx-samples", type=int, default=6,
                    help="标定折算比用几个样本（每个 2 次请求）")
    ap.add_argument("--export-limit", type=int, default=1500,
                    help="实测行最多导出多少条（估算行另有 --est-cap）")
    ap.add_argument("--within-hours", type=int, default=48)
    ap.add_argument("--est-cap", type=int, default=1500,
                    help="Steam 拿不到真实盘口时，用第三方中间价估多少件"
                         "（0 = 关掉估算，只导出实测过的）。默认 1500 "
                         "件 ≈ 380KB，足够把候选池铺满又不至于首屏太慢")
    ap.add_argument("--deadline", type=float, default=0,
                    help="本轮最多干多少分钟，到点自己收工")
    a = ap.parse_args()
    if a.deadline > 0:
        set_deadline(a.deadline)
    if not a.out_dir:
        a.out_dir = os.path.join(DATA, "shards")
    if not a.in_dir:
        a.in_dir = a.out_dir
    if a.mode == "collect":
        run_collect(a)
    elif a.mode == "merge":
        run_merge(a)
    else:
        run_fake(a)


if __name__ == "__main__":
    main()
