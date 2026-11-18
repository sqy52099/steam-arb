#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 CSGO-API 的中英文对照整理成一张查表，给前端把物品名换成中文。

为什么不在运行时问 Steam：
  - 物品页和 market 接口在机房 IP 上常年 429（见 README 的接口死活表）；
  - 就算通了，一页几百件物品就是几百次请求，纯浪费限额。

数据源：https://github.com/ByMykel/CSGO-API
  - public/api/{en,zh-CN}/skins.json  2246 条，名字**不带磨损**（`AWP | Asiimov`），
    磨损另给在每条自己的 wears 数组里
  - 其余带 market_hash_name 的文件（箱子/探员/钥匙/音乐盒/挂件…）名字本身就是完整的

所以只需要两张「基名 → 中文名」表，剩下的在前端拼：
  `StatTrak™ AWP | Asiimov (Field-Tested)`
  → 前缀 StatTrak™ 原样 + 基名 `AWP | Asiimov` 查表得 `AWP | 二西莫夫`
     + 后缀 `(Field-Tested)` 查 wear 表得 `久经沙场`
  → `StatTrak™ AWP | 二西莫夫（久经沙场）`

  输出：{"wear": {英文磨损: 中文}, "skins": {基名: 中文}, "flat": {完整名: 中文}}
  约 330 KB。直接展开成 2 万多条完整名要 2.1 MB，为一个查表多传 1.8 MB 不划算。

用法：
    python tools/build_zh_names.py --out data/zh.json
    python tools/build_zh_names.py --out data/zh.json --src /path/to/CSGO-API

没给 --src 时会自己稀疏克隆一份到临时目录再删。CI 里能直连 github.com，
本机连不上（见 README），要本地跑就先自己把仓库弄下来再传 --src。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = "https://github.com/ByMykel/CSGO-API.git"
# 只要这两个语言的 api 目录；整仓几百 MB，全克隆没必要
SPARSE = ("public/api/en", "public/api/zh-CN")

WEAR_ZH = {
    "Factory New": "崭新出厂",
    "Minimal Wear": "略有磨损",
    "Field-Tested": "久经沙场",
    "Well-Worn": "破损不堪",
    "Battle-Scarred": "战痕累累",
}

# 这些文件带 market_hash_name，名字能直接当 key，不用按磨损展开
FLAT_FILES = (
    "crates",        # 武器箱
    "agents",        # 探员
    "collectibles",  # 收藏品 / 奖牌类
    "keys",          # 钥匙
    "music_kits",    # 音乐盒
    "patches",       # 布章
    "keychains",     # 挂件
    "highlights",    # 精彩时刻
)


def sparse_clone(dest: str) -> str:
    """只把 public/api/{en,zh-CN} 拉下来。"""
    def run(*a):
        r = subprocess.run(a, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("%s\n%s" % (" ".join(a), r.stderr.strip()[:500]))
        return r.stdout

    run("git", "clone", "-q", "--depth", "1", "--filter=blob:none",
        "--sparse", REPO, dest)
    run("git", "-C", dest, "sparse-checkout", "set", *SPARSE)
    return dest


def load(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build(src: str) -> dict:
    api = os.path.join(src, "public", "api")
    en_skins = load(os.path.join(api, "en", "skins.json"))
    zh_skins = load(os.path.join(api, "zh-CN", "skins.json"))
    zh_by_id = {x["id"]: x["name"] for x in zh_skins}

    skins = {}
    for s in en_skins:
        skins[s["name"]] = zh_by_id.get(s["id"], s["name"])

    flat = {}
    for name in FLAT_FILES:
        p_en = os.path.join(api, "en", name + ".json")
        p_zh = os.path.join(api, "zh-CN", name + ".json")
        if not (os.path.exists(p_en) and os.path.exists(p_zh)):
            continue
        zh_by_id = {x["id"]: x["name"] for x in load(p_zh)}
        for x in load(p_en):
            k = x.get("market_hash_name")
            # 基名表优先：它带的是「不带磨损的裸名」，比完整名更通用
            if k and k not in skins:
                flat[k] = zh_by_id.get(x["id"], x["name"])

    return {
        "_note": "由 tools/build_zh_names.py 从 ByMykel/CSGO-API 生成，别手改",
        "wear": WEAR_ZH,
        "skins": skins,
        "flat": flat,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/zh.json")
    ap.add_argument("--src", default="", help="已经克隆好的 CSGO-API 目录；不给就自己克隆")
    args = ap.parse_args()

    tmp = None
    try:
        src = args.src
        if not src:
            tmp = tempfile.mkdtemp(prefix="csgoapi-")
            print("克隆 CSGO-API（只取 public/api）…")
            src = sparse_clone(os.path.join(tmp, "repo"))
        doc = build(src)
    except Exception as e:
        print("生成失败：%s" % e, file=sys.stderr)
        return 1
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    size = os.path.getsize(args.out)
    print("写出 %s：skins %d 条 / flat %d 条 / %.1f KB"
          % (args.out, len(doc["skins"]), len(doc["flat"]), size / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
