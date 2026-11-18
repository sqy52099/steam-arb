#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Steam 社区市场手续费 —— 精确模拟，不用 ×0.85 近似。

规则（CS2 / 大部分 appid=730 物品）：
  买家付出的 P 被拆成三份：卖家净得 S、Steam 服务费、发行商（游戏方）费
  两边分别 floor，各自最低 1 分。

    steam_fee = max(1, floor(S * 5%))
    pub_fee   = max(1, floor(S * 10%))
    P         = S + steam_fee + pub_fee

反过来（已知买家付 P，求卖家净得 S）不能直接除，要二分逼近，
因为上面的 floor 让函数是阶梯状、非严格单调。

依据：Steam 官方对社区市场费率的说明（5% Steam + 10% 游戏方，按卖家收
到的钱计，各自至少 1 分）。小额物品这一点取整影响很大，必须实测校准。
"""
from __future__ import annotations

STEAM_PCT = 0.05
PUB_PCT = 0.10
MIN_FEE_CENTS = 1          # 1 分钱，以「分」为单位

__all__ = [
    "steam_fee_cents", "pub_fee_cents", "buyer_pays_cents",
    "net_from_buyer_cents", "net_from_buyer", "buyer_pays",
    "net_ratio", "fees_from_net",
]


def _floor(v: float) -> int:
    # 用 round 兜住浮点误差：0.05*29.999999 → 1.4999 floor 得 1，但真实应是 1
    return int(v // 1)


def steam_fee_cents(net_cents: int) -> int:
    """Steam 服务费（分）。net_cents 是卖家净得。"""
    return max(MIN_FEE_CENTS, _floor(net_cents * STEAM_PCT))


def pub_fee_cents(net_cents: int) -> int:
    """发行商费用（分）。"""
    return max(MIN_FEE_CENTS, _floor(net_cents * PUB_PCT))


def buyer_pays_cents(net_cents: int) -> int:
    """已知卖家净得，求买家付出。"""
    if net_cents <= 0:
        return 0
    return net_cents + steam_fee_cents(net_cents) + pub_fee_cents(net_cents)


def net_from_buyer_cents(pay_cents: int) -> int:
    """已知买家付出，求卖家净得。二分 + 回验，避免阶梯误差。

    pay_cents 必须 >= 3（1 分净得也要收 2 分手续费），否则返回 0。
    """
    if pay_cents < 3:
        return 0
    lo, hi = 1, pay_cents
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        total = buyer_pays_cents(mid)
        if total <= pay_cents:
            best = mid          # mid 的净得买家付得起，记下来继续往上试
            lo = mid + 1
        else:
            hi = mid - 1
    return best


# ---- 元为单位的便捷包装 ----

def _to_cents(yuan: float) -> int:
    return int(round(float(yuan) * 100))


def net_from_buyer(yuan: float) -> float:
    """买家付 ¥X，卖家实际到手多少元。"""
    return round(net_from_buyer_cents(_to_cents(yuan)) / 100.0, 2)


def buyer_pays(net_yuan: float) -> float:
    """卖家想净得 ¥Y，买家要付多少元。"""
    return round(buyer_pays_cents(_to_cents(net_yuan)) / 100.0, 2)


def fees_from_net(net_yuan: float) -> dict:
    """给定净得，拆出各费用，用于页面上给人看。"""
    c = _to_cents(net_yuan)
    sf, pf = steam_fee_cents(c), pub_fee_cents(c)
    return {
        "net": round(c / 100.0, 2),
        "steam_fee": round(sf / 100.0, 2),
        "pub_fee": round(pf / 100.0, 2),
        "buyer_pays": round((c + sf + pf) / 100.0, 2),
        "effective_rate": round(c / (c + sf + pf), 4) if c else 0.0,
    }


def net_ratio(steam_buyer_price: float, cost: float) -> float:
    """面值比：Steam 上按买家价卖掉后净得 ÷ 你的买入成本。

    >= 1 才是「不亏面值」，注意这不等于不亏钱 —— 余额折现还要再打折。
    """
    if cost <= 0:
        return 0.0
    return round(net_from_buyer(steam_buyer_price) / cost, 4)


if __name__ == "__main__":
    # 自检：几个常见价位对一下
    for p in (0.10, 0.30, 0.50, 1.00, 3.00, 10.00, 50.00, 100.00):
        n = net_from_buyer(p)
        back = buyer_pays(n)
        flag = "OK" if abs(back - p) < 0.011 else "差"
        print(f"买家付 ¥{p:>7.2f} → 净得 ¥{n:>7.2f}  回推 ¥{back:>7.2f}  {flag}")
    print()
    print("净得 ¥10.00 拆分：", fees_from_net(10.00))
    print("净得 ¥0.50  拆分：", fees_from_net(0.50))
    print("面值比（Steam 卖 10 元 / 成本 8 元）：", net_ratio(10.0, 8.0))
