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
    return 0


if __name__ == "__main__":
    sys.exit(main())
