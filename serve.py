#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地预览服务器：把 web/ 当站点根，把 data/ 挂到 /data/ 下。

用法：python serve.py  然后打开 http://127.0.0.1:8770
只用于本机看效果，不要拿去部署 —— 公开站点走 Cloudflare Pages。
"""
import json
import os
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from functools import partial

BASE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(BASE, "web")
DATA = os.path.join(BASE, "data")
PORT = int(os.environ.get("PORT", "8770"))


class H(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        # /data/xxx.json → <项目>/data/xxx.json
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if clean.startswith("/data/"):
            rel = clean[len("/data/"):]
            return os.path.join(DATA, rel.replace("/", os.sep))
        return super().translate_path(path)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *a):
        if "/data/" in (fmt % a) or True:
            print(f"  {self.client_address[0]} {fmt % a}", flush=True)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), partial(H, directory=WEB))
    print(f"预览：http://127.0.0.1:{PORT}")
    print(f"  站点根 {WEB}")
    print(f"  数据   {DATA}")
    if not os.path.exists(os.path.join(DATA, "latest.json")):
        print("  提示：还没有 data/latest.json，先跑 python collector/run.py --fake")
    print("Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("已停止")


if __name__ == "__main__":
    main()
