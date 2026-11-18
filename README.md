# Steam 皮肤比价站

只做信息展示：把「第三方买入价」和「Steam 社区市场卖出后净得」放在一起算比例。
不收款、不代充、不撮合、不存任何用户数据。

## 为什么采集不能跑在本机

Steam 社区市场（`steamcommunity.com`）在国内直连不通。所以：

```
GitHub Actions（境外出口，免费）  →  采集入库
        ↓ 写 data 分支
GitHub Pages（静态站）            →  只读展示
```

本机只管开发和预览，不需要能连 Steam。

## 目录

```
collector/
  fees.py           Steam 阶梯手续费精确模拟（不是 ×0.85）
  thirdparty.py     Skinport + Waxpeer 买入价
  steam.py          三个 Steam 只读接口 + 令牌桶限流 + 429 退避
  store.py          SQLite（items / quotes / meta），30 天清理
  run.py            一次采集的编排：拉价 → 算比例 → 入库 → 导出 JSON
web/index.html      静态前端（单文件，无依赖）
build_site.py       拼出可发布的 _site/
serve.py            本地预览（web/ + data/）
data/               本地开发用：arb.db、latest.json
.github/workflows/collect.yml   定时采集
```

## 本地怎么跑

```bash
# 1. 不联网，用合成数据跑通全链路
python collector/run.py --fake

# 2. 起预览
python serve.py
# 打开 http://127.0.0.1:8770

# 3. 真实采集（这台机器连不上 Steam，Steam 部分会失败，第三方能通）
python collector/run.py --batch 60
```

## 手续费为什么必须精确算

Steam 按「卖家净得」反推，两边分别 `floor`，各自最低 1 分：

```
steam_fee = max(1, floor(net × 5%))
pub_fee   = max(1, floor(net × 10%))
买家付 = net + steam_fee + pub_fee
```

大额时净得 ≈ 买家付 ÷ 1.15，小额（< ¥1）因为取整差别很大。
`fees.py` 里用二分反查，并且回推验证过。

## 两个指标不要混

| 指标 | 公式 | 含义 |
| --- | --- | --- |
| 面值比 | Steam 净得 ÷ 第三方买入价 | ≥ 1 才不亏面值 |
| 折现净额 | 净得 × 折现系数 − 买入价 | 余额当现金只能按 0.75~0.85 用 |

面值比 1.15 在折现系数 0.8 下，实际净额 = 1.15×0.8 − 1 = **−0.08**，
是亏的。页面上两个都显示，就是这个原因。

## 接口与限流

| 接口 | 用途 | 限制 |
| --- | --- | --- |
| `market/search/render` | 全量列表 + 成交量 | 宽松 |
| `market/priceoverview` | 挂单价 / 中间价 | 约 20 次/分 |
| `market/itemordershistogram` | 真实最高求购价 | 更严，易 429 |

`item_nameid` 藏在物品页 HTML 的 `Market_LoadOrderSpread(123)` 里，**终生不变**，
所以只在第一次见到物品时抓一次，存进 `items.nameid`。这是省请求量的关键。

默认 3.5 秒一个请求；429 指数退避；连续三次 429 本轮收工，进度留在 `meta` 里，
下一轮接着轮转。

## 部署

1. 仓库设成 **public**（私有仓库 Actions 2000 分钟/月会超）
2. 推上去，`.github/workflows/collect.yml` 每 30 分钟跑一次（实际延迟 5~15 分钟）
3. 仓库 **Settings → Pages → Source 选 "GitHub Actions"**（只需设一次）。
   之后每次采集跑完，同一次 workflow 里的 `deploy` job 会自动把 `_site/` 发上去，
   不用另建 workflow，也不会出现页面比数据新/旧的问题。
4. 汇率可在仓库 Variables 里设 `USD_CNY`，或放 `data/rate.json`

站点地址是 `https://<用户名>.github.io/steam-arb/`。
前端所有请求都是相对路径，放项目页子路径下能直接用。

### 本机推代码：走 SSH over 443

本机 `github.com:443` 是不通的（解析到的那个 IP 被墙），但 `ssh.github.com:443`
通 —— 所以推代码走 SSH，别用 HTTPS：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_github -N "" -C "github"
# 公钥贴到 GitHub → Settings → SSH and GPG keys

cat >> ~/.ssh/config <<'EOF'
Host github.com
  HostName ssh.github.com
  Port 443
  User git
  IdentityFile ~/.ssh/id_ed25519_github
EOF

ssh -T git@github.com     # 出现 "Hi <用户名>!" 就通了
git remote add origin git@github.com:<用户名>/steam-arb.git
git push -u origin main
```

### 第一次跑完看哪里

Actions → collect → 最新一次 → 下面的 Summary。
里面有一张「环节 / 状态 / 详情」表：

- 四个环节全 ✅ = 一切正常
- `Steam 物品列表` 或 `Steam 求购价` ❌ = runner 出口被 Steam 拦了（这是最可能
  踩的坑，第一次手动触发就要确认这件事），此时页面上的数字是上一轮的
- 只是数字没变、表全 ✅ = 真的没变化，不是故障

采集那步失败也 `exit 0`（为了保住旧数据），所以**不要看 job 是绿是红**，
要看 Summary 里那张表。

### 时间预算（改参数前必看）

Actions 单 job 上限 25 分钟，而 Steam 那边必须限速。当前配比：

```
列表 60 页 + 补 30 个 nameid + 查 120 件求购价 = 210 请求
210 × 3.5 秒 ≈ 12 分钟   →  --deadline 12
```

`--deadline` 是采集自己的止损线，到点就停、把已拿到的写回去。
**不能只靠 job 的 25 分钟硬超时** —— 那会把进程直接杀掉，`push` 那步
根本不执行，整轮白跑。想加量就同时加 `--deadline`，别让请求数超过
`deadline 分钟 × 17`（17 = 60/3.5）。

cron 也不能排得比一轮耗时更密：一轮要 15~20 分钟，按 10 分钟排会不断
堆队列，最后全是排队等超时的废跑。

## 边界

- Steam 订阅协议禁止把市场数据用于商业目的。本站**不放广告、不导流到自家店铺**。
- 不提供代挂代卖、不做资金池。一旦涉及收钱，法律性质就完全不同了。
- 页面上必须写明数据延迟，别让人以为实时。
