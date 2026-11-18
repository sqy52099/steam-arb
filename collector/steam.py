#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Steam 社区市场采集 —— 三个只读接口，不需要登录。

  1. market/search/render      全量物品列表 + 24h 成交量（分页，一次 100 条）
  2. market/priceoverview      最低挂单价 / 中间价 / 24h 成交量
  3. market/itemordershistogram  真实最高求购价 + 盘口深度（最重，要限流）

item_nameid 藏在物品页 HTML 的 Market_LoadOrderSpread( 1760231 ) 里，
每个物品终生不变 —— 取一次存进 db，以后只调 histogram，请求量砍半。

限流：默认 3.5 秒一个请求。steam 限得比官方说的更死，收到 429 就退避，
连续三次 429 本轮直接收工（把进度留在 meta 里，下一轮接着来）。

注意：这个模块必须跑在能访问 steamcommunity.com 的机器上（境外出口）。
本机会全部失败，那是预期行为，不是 bug。
"""
from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

APPID = 730
CURRENCY = 23          # 人民币
COUNTRY = "CN"
BASE = "https://steamcommunity.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

NAMEID_RE = re.compile(r"Market_LoadOrderSpread\(\s*(\d+)\s*\)")


class RateLimited(RuntimeError):
    pass


class Challenge(RuntimeError):
    """Steam 回了一个 HTML 拦截页（多半是 IP 被临时盯上）。"""


class Limiter:
    """令牌桶 + 429 退避。"""

    def __init__(self, gap: float = 3.5, max_429: int = 3):
        self.gap = gap
        self.max_429 = max_429
        self._last = 0.0
        self.strikes = 0

    def wait(self):
        dt = time.monotonic() - self._last
        if dt < self.gap:
            time.sleep(self.gap - dt)
        self._last = time.monotonic()

    def backoff(self):
        self.strikes += 1
        if self.strikes >= self.max_429:
            raise RateLimited(f"连续 {self.strikes} 次 429，本轮收工")
        nap = min(120, 30 * (2 ** (self.strikes - 1))) + random.random() * 5
        time.sleep(nap)

    def ok(self):
        self.strikes = 0


LIMITER = Limiter()


def _get(url: str, timeout: int = 30, retries: int = 2) -> bytes:
    last = None
    for i in range(retries + 1):
        LIMITER.wait()
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            # Steam 的 JSON 接口靠这个头要「JSON 而非 HTML」
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE}/market/",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                LIMITER.ok()
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                LIMITER.backoff()
                continue
            if e.code in (502, 503, 504):
                last = e
                time.sleep(5 + i * 5)
                continue
            raise
        except Exception as e:          # 网络抖动
            last = e
            time.sleep(3 + i * 3)
    raise RuntimeError(f"请求失败 {url[:120]}：{last}")


def _json(url: str, timeout: int = 30) -> dict:
    raw = _get(url, timeout=timeout)
    head = raw[:400].lstrip()
    if head.startswith(b"<"):
        raise Challenge(f"返回了 HTML 而非 JSON（被拦了？）：{head[:120]!r}")
    try:
        return json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"JSON 解析失败：{e}；前 200 字节 {raw[:200]!r}")


# ---------------- 1. 全量列表 ----------------

def search_page(start: int = 0, count: int = 100, appid: int = APPID) -> dict:
    q = urllib.parse.urlencode({
        "appid": appid, "norender": 1, "count": count, "start": start,
        "sort_column": "popular", "sort_dir": "desc",
        "currency": CURRENCY, "country": COUNTRY,
    })
    return _json(f"{BASE}/market/search/render/?{q}")


def iter_all(appid: int = APPID, count: int = 100, max_pages: int = 250,
             on_page=None) -> list[dict]:
    """翻完全量列表。CS2 约 2 万件 → 200 页。max_pages 防止无限跑。"""
    rows, start, pages = [], 0, 0
    while pages < max_pages:
        try:
            j = search_page(start, count, appid)
        except Challenge:
            break
        res = j.get("results") or []
        if not res:
            break
        for it in res:
            hn = it.get("hash_name") or it.get("name")
            if not hn:
                continue
            rows.append({
                "hash_name": hn,
                "sell_listings": int(it.get("sell_listings") or 0),
                # sell_price 是「分」，这里换成元
                "sell_cny": round((it.get("sell_price") or 0) / 100.0, 2),
            })
        pages += 1
        start += count
        if on_page:
            on_page(pages, len(rows), j.get("total_count"))
        total = j.get("total_count") or 0
        if total and start >= total:
            break
    return rows


# ---------------- 2. 价格概览 ----------------

def _parse_price(s) -> float | None:
    """'¥ 1.23' / '¥1,234.56' / '1.234,56€' / '' → float 元

    麻烦的是逗号既可能是小数点（欧陆写法）又可能是千分位。判定规则：
      1. 两种符号都在 → 靠右那个是小数点，另一个是千分位
      2. 只有一种、且出现多次 → 千分位
      3. 只有一种、只出现一次 → 后面正好 3 位数字且前面还有数字 → 千分位，否则小数点
    """
    if s is None:
        return None
    txt = str(s).strip()
    if not txt:
        return None
    # 空格有时也当千分位（'¥ 12 500,00'），先把夹在数字中间的空格去掉
    txt = re.sub(r"(?<=\d)[ \u00a0\u202f]+(?=\d)", "", txt)
    m = re.search(r"\d[\d.,]*", txt)
    if not m:
        return None
    txt = m.group(0).rstrip(".,")
    if not txt:
        return None

    has_dot, has_comma = "." in txt, "," in txt
    if has_dot and has_comma:
        # 靠右的是小数点
        dec = "." if txt.rfind(".") > txt.rfind(",") else ","
        thou = "," if dec == "." else "."
        txt = txt.replace(thou, "").replace(dec, ".")
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        head, _, tail = txt.rpartition(sep)
        if sep in head:
            # 出现多次 → 全是千分位
            txt = txt.replace(sep, "")
        elif len(tail) == 3 and head and head != "0":
            # 1,234 这种当千分位
            txt = head + tail
        else:
            txt = head + "." + tail

    try:
        return round(float(txt), 2)
    except ValueError:
        return None


def price_overview(hash_name: str, appid: int = APPID) -> dict | None:
    q = urllib.parse.urlencode({
        "appid": appid, "currency": CURRENCY,
        "market_hash_name": hash_name,
    })
    try:
        j = _json(f"{BASE}/market/priceoverview/?{q}")
    except Challenge:
        return None
    if not j.get("success"):
        return None
    return {
        "sell_cny": _parse_price(j.get("lowest_price")),
        "median_cny": _parse_price(j.get("median_price")),
        "volume": int(str(j.get("volume") or "0").replace(",", "") or 0),
    }


# ---------------- 3. 求购价（重） ----------------

def fetch_nameid(hash_name: str, appid: int = APPID) -> int | None:
    """从物品页 HTML 抠 item_nameid。每件只做一次，存进 db。"""
    url = (f"{BASE}/market/listings/{appid}/"
           f"{urllib.parse.quote(hash_name, safe='')}")
    raw = _get(url, timeout=40)
    m = NAMEID_RE.search(raw.decode("utf-8", "replace"))
    return int(m.group(1)) if m else None


def orders_histogram(nameid: int) -> dict | None:
    """真实最高求购价 + 最低挂单价 + 盘口。价格字段单位是「分」。"""
    q = urllib.parse.urlencode({
        "item_nameid": nameid, "currency": CURRENCY, "country": COUNTRY,
    })
    try:
        j = _json(f"{BASE}/market/itemordershistogram?{q}")
    except Challenge:
        return None
    if not j.get("success"):
        return None

    def cents(v):
        try:
            return round(int(v) / 100.0, 2)
        except (TypeError, ValueError):
            return None

    buy_graph = j.get("buy_order_graph") or []
    sell_graph = j.get("sell_order_graph") or []
    highest_bid = cents(j.get("highest_buy_order")) if j.get("highest_buy_order") else None
    lowest_sell = cents(j.get("lowest_sell_order")) if j.get("lowest_sell_order") else None
    if highest_bid is None and buy_graph:
        highest_bid = cents(buy_graph[0][0])
    if lowest_sell is None and sell_graph:
        lowest_sell = cents(sell_graph[0][0])

    # 求购盘口：前 10 档有多少件挂单，判断「卖掉 N 件会不会砸穿」
    bid_depth = sum(int(g[1]) for g in buy_graph[:10]) if buy_graph else 0

    return {
        "bid_cny": highest_bid,       # 最高求购价（买家愿付）
        "lowest_sell_cny": lowest_sell,
        "bid_depth": bid_depth,       # 前 10 档可承接件数
        "bid_levels": len(buy_graph),
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", default="", help="测单个物品")
    ap.add_argument("--pages", type=int, default=1)
    a = ap.parse_args()

    if a.test:
        hn = a.test
        print("priceoverview:", price_overview(hn))
        nid = fetch_nameid(hn)
        print("nameid:", nid)
        if nid:
            print("histogram:", orders_histogram(nid))
    else:
        rows = iter_all(max_pages=a.pages,
                        on_page=lambda p, n, t: print(f"  第 {p} 页，累计 {n}/{t}"))
        print(f"共 {len(rows)} 条")
        for r in rows[:5]:
            print(" ", r)
