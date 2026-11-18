#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Steam 社区市场采集 —— 只用两个还活着的接口。

背景（2026-11 实测，GitHub Actions 出口）：
  死：market/search/render（全量列表）、market/（首页）、market/pricehistory
      —— 机房 IP 上恒 429，冷却也不恢复。原方案「先翻全量列表」这条路废了。
      候选名单改由第三方接口给（thirdparty.py，一次 6580 件带买入价）。
  死：itemordershistogram 需要 item_nameid，而新版 SSR 物品页里
      Market_LoadOrderSpread 出现 0 次，挖不出 nameid。

  活：market/priceoverview/?currency=1|23   最低价/中间价/成交量，很轻
      物品页 /market/listings/730/<name>     HTML 里内嵌完整盘口，很重

盘口藏在 TanStack Query 的脱水状态里，结构是：
    {"state":{"data":{"amtMaxBuyOrder":117,"amtMinSellOrder":119,
                      "eCurrency":1,"cBuyOrders":1799237,"cSellOrders":41827,
                      "rgCompactBuyOrders":[价格,件数,价格,件数,...],...}},
     "queryKey":["market","orderbook",730,"Prisma Case"]}

⚠ 盘口的价格字段单位是**美元分**，不是人民币分。eCurrency 恒为 1，
  加 ?currency=23 / ?cc=CN / cookie steamCurrency 都改不动（13 轮实测）。
  Prisma Case 求购 117 分 = $1.17，而 priceoverview 人民币最低价 ¥7.95
  → 折算比 6.79，而真实汇率 7.1。Steam 国区定价比汇率低几个点，
  所以**必须按市场折算比换**，不能乘汇率（见 market_factor）。

限流：物品页是重接口，同一 IP 平均只能 11.9 件/分钟，而且是成簇的
（前 2 分钟全通、接着 3 分钟几乎全 429、然后恢复）。所以：
  * 429 不立即收工，改为递增退避，把「恢复期」也吃满；
  * 总量靠多 job 并行摊 —— 每个 runner 出口 IP 不同，额度独立。
"""
from __future__ import annotations

import gzip
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

APPID = 730
CURRENCY_CNY = 23      # Steam 的货币 id：23 = 人民币
CURRENCY_USD = 1
COUNTRY = "CN"
BASE = "https://steamcommunity.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 盘口的锚：先定位 orderbook 的 queryKey，再从它前面找 data 对象。
# 注意不能放宽成「随便找个 "data":{」—— 那样会抓到隔壁查询的 data，
# 拿到离谱的数字（实测出现过折算比 1.24 的假数据）。
OB_KEY = '"queryKey":["market","orderbook"'
OB_DATA = '"data":{"amtMaxBuyOrder"'


class RateLimited(RuntimeError):
    """长时间被限流（保留给调用方判断，正常流程不会抛）。"""


class NetworkFailed(RuntimeError):
    """连不上（DNS / TCP / TLS / 超时）。

    和 RateLimited 分开：429 等一等还有救，连不上就是这台机器现在到不了
    Steam，多试几十次只是把整轮预算耗光。线上出现过四个 shard 全部 0
    成功、artifacts 只有 386 字节，就是这么烧掉的。
    """


class Challenge(RuntimeError):
    """Steam 回了 HTML 拦截页而不是要的内容。"""


class ParseError(RuntimeError):
    """页面结构变了，盘口抠不出来。"""


class Limiter:
    """令牌桶 + 429 递增退避。

    gap 可用环境变量 STEAM_GAP 覆盖 —— CI 每轮时间预算固定，必须能调。
    429 不再「连续三次就收工」：实测限流是阵发的，硬退避能等到恢复窗口，
    收工则白丢那一段（8 分钟压测里恢复期贡献了 41 件）。
    """

    def __init__(self, gap: float | None = None, cap: float | None = None):
        if gap is None:
            gap = _env_float("STEAM_GAP", 2.5)
        if cap is None:
            cap = _env_float("STEAM_BACKOFF_CAP", 45.0)
        self.gap = gap
        self.cap = cap
        self._last = 0.0
        self.strikes = 0

    def wait(self):
        dt = time.monotonic() - self._last
        if dt < self.gap:
            time.sleep(self.gap - dt)
        self._last = time.monotonic()

    def on_429(self):
        """被限流：睡一会儿，越连续睡越久，但封顶，然后继续试。"""
        self.strikes += 1
        nap = min(self.cap, 3.0 + 4.0 * self.strikes) + random.random() * 3
        time.sleep(nap)

    def ok(self):
        self.strikes = 0


def _env_float(key: str, default: float) -> float:
    try:
        v = os.environ.get(key)
        return float(v) if v else default
    except ValueError:
        return default


LIMITER = Limiter()

# 单个请求的连接/读取上限。原来写 45~50 秒，一台机器整片连不上 Steam 时
# 每件要白等 50 秒 × retries，12 分钟预算只够试十来件就收工 —— 线上出现过
# 四个 shard 全部 0 成功、artifacts 只有 386 字节的情况。物品页正常 1~3 秒
# 就回来，15 秒还拿不到基本就是挂了。
FETCH_TIMEOUT = _env_float("STEAM_TIMEOUT", 15.0)


HTML_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Referer": f"{BASE}/market/",
}
JSON_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    # Steam 的 JSON 接口靠这个头要「JSON 而非 HTML」
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE}/market/",
}


def _fetch(url: str, headers: dict, timeout: int = 45,
           retries: int = 2) -> tuple[int, bytes]:
    """返回 (状态码, 内容)。429 在这里消化掉，不往上抛。"""
    last = None
    for i in range(retries + 1):
        LIMITER.wait()
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    try:
                        raw = gzip.decompress(raw)
                    except OSError:
                        pass
                LIMITER.ok()
                return r.status, raw
        except urllib.error.HTTPError as e:
            if e.code == 429:
                LIMITER.on_429()
                last = e
                continue
            if e.code in (502, 503, 504):
                last = e
                LIMITER.on_429()
                continue
            # 404 之类是真实结果，交给调用方判断
            return e.code, e.read()[:400]
        except Exception as e:          # 网络抖动 / 超时
            last = e
            # 连着失败说明这台机器现在根本连不上，多睡没意义；第一次
            # 抖动给一次重试机会，之后就快速把 (0, 原因) 交出去，让上层
            # 按 RateLimited 计数收工，别把 12 分钟耗在等超时上。
            if i == 0 and retries > 0:
                time.sleep(2)
    return 0, str(last or "请求失败").encode()


def _json(url: str, timeout: int = 30, retries: int = 2) -> dict:
    st, raw = _fetch(url, JSON_HEADERS, timeout=timeout, retries=retries)
    if st != 200:
        raise Challenge(f"HTTP {st}：{raw[:120]!r}")
    head = raw[:400].lstrip()
    if head.startswith(b"<"):
        raise Challenge(f"返回了 HTML 而非 JSON（被拦了？）：{head[:120]!r}")
    try:
        return json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"JSON 解析失败：{e}；前 200 字节 {raw[:200]!r}")


def _parse_price(s) -> float | None:
    """'¥ 1.23' / '¥1,234.56' / '1.234,56€' / '' → float

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


# ---------------- 1. 价格概览（轻，用于标定折算比） ----------------

def price_overview(hash_name: str, currency: int = CURRENCY_CNY,
                   appid: int = APPID, retries: int = 1) -> dict | None:
    """最低挂单价 / 中间价 / 24h 成交量。currency 决定返回哪种货币的报价。"""
    q = urllib.parse.urlencode({
        "appid": appid, "currency": currency,
        "market_hash_name": hash_name,
    })
    try:
        j = _json(f"{BASE}/market/priceoverview/?{q}", retries=retries)
    except Challenge:
        return None
    if not j.get("success"):
        return None
    return {
        "lowest": _parse_price(j.get("lowest_price")),
        "median": _parse_price(j.get("median_price")),
        "volume": int(str(j.get("volume") or "0").replace(",", "") or 0),
    }


def market_factor(items: list[str], min_usd: float = 0.5,
                  max_seconds: float = 90.0) -> dict:
    """算出「Steam 国区人民币价 ÷ 美元价」的市场折算比。

    为什么不能直接用汇率：Steam 的国区定价比真实汇率低几个点，而且各物品
    因为两个货币的盘口是各自独立的，逐件还有 ±1% 的抖动。实测 Prisma Case
    $1.19/¥7.95 = 6.68，而汇率是 7.1 —— 直接用汇率会把人民币价高估 6%，
    方向正好是「让你以为更赚」，宁可低估也不能这样错。

    做法：取一篮高流动性物品，同一时刻问两种货币的报价，取比值的中位数。
    用 median_price（中间价）优先，它比最低价稳；低价物品先剔掉，
    因为一分钱的舍入在 $0.10 上有 10% 的误差。

    **必须有时间上限。** 第一版没设，6 个样本 12 次请求，全撞 429 时每次
    退避到 45 秒还要重试两轮，合并 job 在这上面烧了 11 分钟才放弃，最后
    仍退回汇率。现在：每次请求只重试 1 次，连续空手 3 次就认输，总耗时
    到 max_seconds 也立刻收工 —— 拿不到就早点说拿不到，别拖着。
    """
    t0 = time.monotonic()
    pairs = []
    detail = []
    misses = 0
    for hn in items:
        if time.monotonic() - t0 > max_seconds:
            detail.append({"n": hn, "skip": "超时"})
            break
        usd = price_overview(hn, CURRENCY_USD, retries=1)
        if not usd:
            misses += 1
            detail.append({"n": hn, "fail": "USD 报价拿不到"})
            if misses >= 3:
                break
            continue
        cny = price_overview(hn, CURRENCY_CNY, retries=1)
        if not cny:
            misses += 1
            detail.append({"n": hn, "fail": "人民币报价拿不到"})
            if misses >= 3:
                break
            continue
        misses = 0
        u = usd.get("median") or usd.get("lowest")
        c = cny.get("median") or cny.get("lowest")
        if not u or not c or u < min_usd:
            continue
        pairs.append(c / u)
        detail.append({"n": hn, "usd": u, "cny": c, "f": round(c / u, 3)})

    if not pairs:
        return {"factor": None, "n": 0, "spread": None, "detail": detail}

    pairs.sort()
    mid = pairs[len(pairs) // 2]
    if len(pairs) % 2 == 0:
        mid = (pairs[len(pairs) // 2 - 1] + pairs[len(pairs) // 2]) / 2
    lo, hi = pairs[0], pairs[-1]
    return {
        "factor": round(mid, 4),
        "n": len(pairs),
        "spread": round((hi - lo) / mid, 4) if mid else None,
        "detail": detail,
    }


# ---------------- 2. 物品页盘口（重） ----------------

def _match_object(seg: str, start: int) -> str | None:
    """从 seg[start] 的 '{' 开始，配对到对应的 '}'（认得字符串和转义）。"""
    depth, j, in_str, esc = 0, start, False, False
    while j < len(seg):
        c = seg[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return seg[start:j + 1]
        j += 1
    return None


def parse_orderbook(html: str, hash_name: str) -> dict:
    """从物品页 HTML 里抠出盘口。返回的价格单位是**美元分**。"""
    # SSR 的脱水状态是 JSON 字符串再序列化过一层的，先拆掉一层转义
    un = html.replace("\\\\", "\\").replace('\\"', '"')
    i = un.find(OB_KEY)
    if i < 0:
        raise ParseError("页面里没有 orderbook 的 queryKey")
    # 确认这个 queryKey 指的是本物品，别抓成页面上其它物品的盘口
    tail = un[i:i + 200]
    if hash_name.replace('"', "") not in tail:
        raise ParseError(f"orderbook 的 queryKey 不是 {hash_name!r}")

    seg = un[max(0, i - 40000):i]
    di = seg.rfind(OB_DATA)
    if di < 0:
        raise ParseError("orderbook 前面找不到 data 对象")
    obj = _match_object(seg, seg.find("{", di))
    if not obj:
        raise ParseError("盘口对象括号没配平")
    try:
        ob = json.loads(obj)
    except Exception as e:
        raise ParseError(f"盘口 JSON 坏了：{e}")

    bid = ob.get("amtMaxBuyOrder")
    sell = ob.get("amtMinSellOrder")
    if not isinstance(bid, int) or not isinstance(sell, int) or bid <= 0 or sell <= 0:
        raise ParseError(f"盘口数字非法 bid={bid!r} sell={sell!r}")
    if bid > sell:
        # 求购价高于挂单价是不合逻辑的，说明抓错了对象
        raise ParseError(f"求购 {bid} > 挂单 {sell}，数据不合理")

    def pair(a):
        if not isinstance(a, list) or len(a) < 2:
            return []
        return [(int(a[k]), int(a[k + 1])) for k in range(0, len(a) - 1, 2)]

    buys = pair(ob.get("rgCompactBuyOrders"))
    sells = pair(ob.get("rgCompactSellOrders"))
    # 买盘深度：吃掉前 10 档能接多少件。倒余额最怕「看着有价，卖不动」
    depth = sum(n for _, n in buys[:10])

    return {
        "usd_bid": bid,            # 最高求购价，美元分
        "usd_sell": sell,          # 最低挂单价，美元分
        "buy_orders": int(ob.get("cBuyOrders") or 0),
        "sell_orders": int(ob.get("cSellOrders") or 0),
        "bid_depth": depth,
        "bid_levels": len(buys),
        "e_currency": int(ob.get("eCurrency") or 0),
        "buy_levels": buys[:10],
        "sell_levels": sells[:10],
    }


def item_orderbook(hash_name: str, appid: int = APPID) -> dict:
    """抓一个物品页并解析盘口。失败抛异常，由调用方决定记不记。"""
    url = (f"{BASE}/market/listings/{appid}/"
           f"{urllib.parse.quote(hash_name, safe='')}")
    st, raw = _fetch(url, HTML_HEADERS, timeout=FETCH_TIMEOUT, retries=1)
    if st == 0:
        raise NetworkFailed(f"连不上：{raw[:100].decode('utf-8', 'replace')}")
    if st == 429:
        raise RateLimited(f"物品页 HTTP {st}")
    if st == 404:
        raise ParseError("物品不存在（404）")
    if st != 200:
        raise RateLimited(f"物品页 HTTP {st}")
    ob = parse_orderbook(raw.decode("utf-8", "replace"), hash_name)
    ob["hash_name"] = hash_name
    return ob


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", default="", help="测单个物品的盘口")
    ap.add_argument("--factor", default="", nargs="*",
                    help="测折算比，给几个物品名")
    a = ap.parse_args()

    if a.test:
        print(json.dumps(item_orderbook(a.test), ensure_ascii=False, indent=2))
    elif a.factor:
        f = market_factor(a.factor)
        print(json.dumps(f, ensure_ascii=False, indent=2))
    else:
        print("用法：--test 'Prisma Case'  或  --factor 'Prisma Case' 'Fracture Case'")
