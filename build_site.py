#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 web/ 和 data/latest.json 拼成一个可以直接发布的目录 _site/。

CI 里由 collect.yml 调它（ARB_DATA 指向 /tmp/arbdata/data），
产物交给 actions/upload-pages-artifact 发到 GitHub Pages。
本地想手动构建就直接：
    python build_site.py

产物：
  _site/index.html
  _site/data/latest.json
  _site/data/zh.json      （物品中文名查表，缺了页面退回英文名）

也可以本地跑完直接看 _site/index.html（需要起个静态服务器，
因为 fetch 读本地 file:// 会被浏览器拦）。
"""
import os
import shutil
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(BASE, "web")
DATA = os.environ.get("ARB_DATA") or os.path.join(BASE, "data")
OUT = os.path.join(BASE, "_site")


def main() -> int:
    if not os.path.isdir(WEB):
        print(f"缺少 {WEB}")
        return 1
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    shutil.copytree(WEB, OUT)

    src = os.path.join(DATA, "latest.json")
    if not os.path.exists(src):
        print(f"警告：{src} 不存在，站点会显示「数据加载失败」")
        return 0
    dst_dir = os.path.join(OUT, "data")
    os.makedirs(dst_dir, exist_ok=True)
    shutil.copy2(src, os.path.join(dst_dir, "latest.json"))

    size = os.path.getsize(os.path.join(dst_dir, "latest.json")) / 1024
    print(f"已构建 {OUT}")
    print(f"  latest.json {size:.1f} KB")

    # 按天历史，只有「我的库存」页签会去取。缺了不影响主表，
    # 那边的涨跌线退回用浏览器本地攒的点。
    hp = os.path.join(DATA, "hist.json")
    if os.path.exists(hp):
        shutil.copy2(hp, os.path.join(dst_dir, "hist.json"))
        print(f"  hist.json {os.path.getsize(hp) / 1024:.1f} KB")
    else:
        print("  没有 hist.json，库存页的涨跌线只能用浏览器本地的记录")

    # 中文名查表。不是每轮都变（CSGO-API 出新的才变），缺了不影响页面能用，
    # 只是物品名退回英文，所以找不到只警告不算失败。
    # 先看 ARB_DATA（CI 里 data 分支被 checkout 到 /tmp），再退回仓库自己的 data/
    for zh in (os.path.join(DATA, "zh.json"),
               os.path.join(BASE, "data", "zh.json")):
        if os.path.exists(zh):
            shutil.copy2(zh, os.path.join(dst_dir, "zh.json"))
            print(f"  zh.json {os.path.getsize(zh) / 1024:.1f} KB")
            break
    else:
        print("警告：没找到 zh.json，物品名会显示成英文"
              "（跑 python tools/build_zh_names.py 生成）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
