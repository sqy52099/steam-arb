#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第三方买入价采集：Skinport + Waxpeer。

这两个站国内能直连，不需要代理；只给「最低挂单价」，
给不了 Steam 最高求购价 —— 求购价必须靠 steam.py 的 itemordershistogram。

单位约定：本模块返回的一切价格都是人民币元（float）。
  Skinport：?currency=CNY 直接给元，但强制要求 brotli 响应。
  Waxpeer ：固定美元，字段是「千分之一美元」的整数（乘以汇率换元）。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request

SKINPORT_URL = ("https://api.skinport.com/v1/items"
                "?app_id=730&currency=CNY&tradable=1")
WAXPEER_URL = "https://api.waxpeer.com/v1/prices?game=csgo"

# 采集时剔除的品类：贴纸、涂鸦、纪念品、容器这类不能当「余额」倒
EXCLUDE = (
    "Sticker", "Graffiti", "Patch", "Medal", "Coin", "Music Kit", "Souvenir",
    "Autograph", "Capsule", "Key", "Pass", "Ticket", "Name Tag", "StatTrak",
    "Charm", "Sealed", "Package", "Gift", "Doppler", "Fade", "Pin",
)


def is_junk(name: str) -> bool:
    return any(k in name for k in EXCLUDE)


def find_brotli_curl() -> str | None:
    """Skinport 只回 brotli；Windows 自带 system32\\curl 不带 brotli，
    会 406 'Client must support Brotli compression'。挑一个支持的。"""
    cands = []
    p = None
    try:
        import shutil
        p = shutil.which("curl")
    except Exception:
        pass
    if p:
        cands.append(p)
    for extra in (
        "/usr/bin/curl",                                   # GitHub Actions / Linux
        r"C:\msys64\ucrt64\bin\curl.exe",
        r"C:\msys64\mingw64\bin\curl.exe",
        r"C:\Program Files\Git\mingw64\bin\curl.exe",
        r"C:\Windows\System32\curl.exe",
    ):
        if os.path.exists(extra):
            cands.append(extra)
    seen = set()
    for c in cands:
        key = os.path.normcase(os.path.abspath(c))
        if key in seen:
            continue
        seen.add(key)
        try:
            v = subprocess.run([c, "--version"], capture_output=True, timeout=15)
        except Exception:
            continue
        if "brotli" in v.stdout.decode("utf-8", "replace").lower():
            return c
    return None


def fetch_skinport(timeout: int = 150) -> dict[str, dict]:
    """返回 {market_hash_name: {buy_cny, suggested_cny, qty}}"""
    curl = find_brotli_curl()
    if not curl:
        raise RuntimeError("没找到支持 brotli 的 curl，Skinport 取不了")
    cmd = [curl, "-sS", "--compressed", "-m", "120",
           "-H", "User-Agent: steam-arb/1.0", SKINPORT_URL]
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"Skinport curl rc={p.returncode} {p.stderr[:200]!r}")
    arr = json.loads(p.stdout.decode("utf-8", "replace"))
    if not isinstance(arr, list):
        raise RuntimeError(f"Skinport 返回不是数组：{str(arr)[:160]}")
    out = {}
    for it in arr:
        if not isinstance(it, dict):
            continue
        name = it.get("market_hash_name")
        mp = it.get("min_price")
        if not name or not mp:
            continue
        out[name] = {
            "buy_cny": round(float(mp), 2),
            "suggested_cny": round(float(it.get("suggested_price") or 0), 2),
            "qty": int(it.get("quantity") or 0),
        }
    return out


def fetch_waxpeer(usd_cny: float, timeout: int = 180) -> dict[str, dict]:
    """Waxpeer 的 min / steam_price 单位是千分之一美元，这里换成人民币元。"""
    req = urllib.request.Request(WAXPEER_URL,
                                 headers={"User-Agent": "steam-arb/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read())
    items = j.get("items") or []
    if not isinstance(items, list):
        raise RuntimeError("Waxpeer items 不是数组，结构可能变了")
    out = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name")
        mn, sp = it.get("min") or 0, it.get("steam_price") or 0
        if not name or mn <= 0 or sp <= 0:
            continue
        out[name] = {
            "buy_cny": round(mn / 1000.0 * usd_cny, 2),
            "steam_mid_cny": round(sp / 1000.0 * usd_cny, 2),
            "qty": int(it.get("count") or 0),
            "type": it.get("type") or "",
            "rarity": it.get("rarity") or "",
        }
    return out


def merge(wax: dict, skin: dict) -> list[dict]:
    """以 Waxpeer 为主表（它有 type/rarity，便于筛选），补上 Skinport 的价，
    取两家里面更便宜的作为「买入价」。

    两家各自的价格也一并留下（buy_skinport / buy_waxpeer）—— 页面要把
    「同一件东西在几个平台各卖多少钱」并排显示。只留最低价的话，前端
    只能说「某处更便宜」，说不出便宜多少、也看不出另一家有没有货。
    """
    rows = []
    now = int(time.time())
    for name, w in wax.items():
        if is_junk(name):
            continue
        s = skin.get(name) or {}
        wb = w["buy_cny"]
        sb = s.get("buy_cny") or 0
        if sb > 0:
            buy, src = (sb, "skinport") if sb < wb else (wb, "waxpeer")
        else:
            buy, src = wb, "waxpeer"
        # 太便宜的没法批量操作，太贵的超出常见预算
        if not (3.0 <= buy <= 20000.0):
            continue
        rows.append({
            "name": name,
            "buy_cny": buy,
            "src": src,
            "src_qty": (s.get("qty") if src == "skinport" else w["qty"]) or 0,
            # 各平台原始报价。没有货写 None 不要写 0 —— 0 会被当成免费
            "buy_skinport": round(sb, 2) if sb > 0 else None,
            "buy_waxpeer": round(wb, 2),
            "qty_skinport": int(s.get("qty") or 0) if sb > 0 else None,
            "qty_waxpeer": int(w["qty"] or 0),
            "steam_mid_cny": w["steam_mid_cny"],
            "type": w["type"],
            "rarity": w["rarity"],
            "ts": now,
        })
    rows.sort(key=lambda r: r["name"])
    return rows


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=7.10, help="USD→CNY")
    ap.add_argument("--out", default="", help="把合并结果写到这个 json")
    a = ap.parse_args()

    t0 = time.time()
    wax, skin = {}, {}
    try:
        wax = fetch_waxpeer(a.rate)
        print(f"Waxpeer  {len(wax)} 条")
    except Exception as e:
        print(f"Waxpeer 失败：{e}")
    try:
        skin = fetch_skinport()
        print(f"Skinport {len(skin)} 条")
    except Exception as e:
        print(f"Skinport 失败：{e}")
    rows = merge(wax, skin)
    print(f"合并后 {len(rows)} 条可倒品，用时 {time.time()-t0:.1f}s")
    for r in rows[:5]:
        print(f"  ¥{r['buy_cny']:>8.2f}  {r['name']}")
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        print(f"已写入 {a.out}")
