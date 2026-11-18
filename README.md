# Steam 皮肤比价站

只做信息展示：把「第三方买入价」和「Steam 社区市场卖出后净得」放在一起算比例。
不收款、不代充、不撮合、不存任何用户数据。

## 为什么采集不能跑在本机

Steam 社区市场（`steamcommunity.com`）在国内直连不通。所以：

```
GitHub Actions 4 个并行 job（境外出口，免费）  →  采物品页盘口，各自上传 artifact
        ↓ 汇总
GitHub Actions 1 个 merge job                 →  标定折算比 → 算比例 → 写 data 分支
        ↓
GitHub Pages（静态站）                         →  只读展示
```

本机只管开发和预览，不需要能连 Steam。

## 目录

```
collector/
  fees.py           Steam 阶梯手续费精确模拟（不是 ×0.85）
  thirdparty.py     Skinport + Waxpeer 买入价（只用来出候选名单和买入价）
  steam.py          priceoverview + 物品页盘口解析 + 限流退避
  store.py          SQLite（items / quotes / meta），只存最新快照，7 天清理
  run.py            两阶段编排：--mode collect（并行分片）/ merge（汇总）/ fake
web/index.html      静态前端（单文件，无依赖）
build_site.py       拼出可发布的 _site/
serve.py            本地预览（web/ + data/）
tools/build_zh_names.py  从 CSGO-API 生成物品中文名查表 data/zh.json
data/               本地开发用：arb.db、latest.json、zh.json、shards/
.github/workflows/collect.yml   4 路采集 + 汇总 + 部署
```

## 本地怎么跑

```bash
# 1. 不联网，用合成数据跑通全链路
python collector/run.py --mode fake

# 2. 起预览
python serve.py
# 打开 http://127.0.0.1:8770

# 3. 自检（100 项，全离线）
python selftest.py

# 4. 真实采集（这台机器连不上 Steam，Steam 部分会失败，第三方能通）
python collector/run.py --mode collect --shard 0 --shards 4 --batch 30
python collector/run.py --mode merge --in-dir data/shards
```

`--mode collect` 只产出 `data/shards/shard-NN.json`，**不碰数据库**。
必须再跑一次 `--mode merge` 才会入库、才会生成 `data/latest.json`。
这样分工是因为 4 个 job 同时跑，谁写库都会互相覆盖。

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

「面值比」还有两列：按 Steam **最高求购价**秒卖（`ratio`），和按**最低挂单价**
排队（`ratio_sell`）。前者立刻成交但价低，后者价高但可能等很久。

## 物品中文名

价格数据里存的是 Steam 的 `market_hash_name`（`AWP | Asiimov (Field-Tested)`）。
中文名不来自 Steam，来自 [ByMykel/CSGO-API](https://github.com/ByMykel/CSGO-API)，
由 `tools/build_zh_names.py` 生成 `data/zh.json`。

为什么不在运行时问 Steam：物品页在机房 IP 上常年 429（见下面的死活表），
一页几百件物品就是几百次请求，纯烧限额。

**存的是「基名」不是完整名。** `skins.json` 里 2246 条的名字不带磨损
（`AWP | Asiimov`），磨损单独给在每条自己的 `wears` 数组里。所以表分两张：

```
wear  {"Field-Tested": "久经沙场", ...}
skins {"AWP | Asiimov": "AWP | 二西莫夫", ...}        1974 条
flat  {"Prisma Case": "棱彩武器箱", ...}               1956 条
```

前端 `zhName()` 按顺序拆名字再拼回去：

```
StatTrak™ AWP | Asiimov (Field-Tested)
  → 前缀 StatTrak™ 原样        （Souvenir 要翻成「纪念品」）
  → 基名 AWP | Asiimov 查表    → AWP | 二西莫夫
  → 磨损 (Field-Tested) 查表   → （久经沙场）
  → StatTrak™ AWP | 二西莫夫（久经沙场）
```

**剥前缀的顺序不能换**：`★` 要最先剥出来但**留在查表用的 key 里**，
因为刀具手套的中文名自带「（★）」—— `★ Karambit | Fade` 查的是
`★ Karambit | Fade`，得到「爪子刀（★） | 渐变之色」。
`★ StatTrak™ Karambit | Doppler` 则要先把 ★ 摘掉、剥掉 StatTrak、再拼回 ★。

**为什么存基名：** 展开成两万多条完整名要 2.1 MB，基名表只有 318 KB。
差 1.8 MB，为了一个查表不值得。

**查不到就退回英文名**（`zhName()` 返回 `null`，原样显示 `it.n`），
不显示空白。`title` 属性里始终留着英文原名 —— 去 Steam 搜、跟人核对都要认英文名。
搜索框同时匹配中英文，搜「红线」和 `redline` 都能出 `AK-47 | Redline`。

**本机没法重新生成**：`raw.githubusercontent.com` / `cdn.jsdelivr.net` 都不通，
GitHub 的 blobs API 拿 4.9 MB 文件会在 571 KB 处截断。CI 里能直连 github.com，
所以生成这一步放在 workflow 的「更新中文名查表」，**失败不算错**（只是名字退回英文）。

想本地重新生成，先自己把仓库弄下来，再传 `--src`：

```bash
git clone --depth 1 --filter=blob:none --sparse git@github.com:ByMykel/CSGO-API.git csgoapi
git -C csgoapi sparse-checkout set public/api/en public/api/zh-CN
python tools/build_zh_names.py --out data/zh.json --src csgoapi
```

## 接口：哪些活、哪些死

2026-11 在 GitHub Actions 的 Azure 出口实测（探针 13 轮，逐条验证）：

| 接口 | 状态 | 说明 |
| --- | --- | --- |
| `market/search/render` | **死** | 恒 429，冷却 60 秒不恢复，header/cookie/参数全试过无差异 |
| `market/`（市场首页） | **死** | 同上 |
| `market/pricehistory` | **死** | 同上 |
| `market/itemordershistogram` | **废** | 接口可达（假 nameid 返 400），但挖不出 `item_nameid` |
| `market/priceoverview` | 活 | 最低挂单价 + 24h 成交量，2.5 秒间隔约 16 件/分 |
| **物品页 `market/listings/730/<名>`** | 活 | 200，410~550 KB，**HTML 里内嵌完整盘口** |

物品页已经重写成 React SSR，旧的 `Market_LoadOrderSpread(123)` 一行都没有了 —
所以「抓 nameid 再调 histogram」这条老路彻底断了。

### 盘口从哪来

物品页 HTML 里有 TanStack Query 的脱水状态，找到

```
"queryKey":["market","orderbook",730,"<物品名>"]
```

再往前找 `"data":{"amtMaxBuyOrder"`，取出配对的 JSON：

```json
{"amtMaxBuyOrder":117,"amtMinSellOrder":119,"eCurrency":1,
 "cBuyOrders":1799237,"cSellOrders":41827,
 "rgCompactBuyOrders":[108,311,107,277,...],
 "rgCompactSellOrders":[119,348,120,712,...]}
```

比原来想调的 `itemordershistogram` 更全，而且**不需要 nameid**。
写进仓库里的 `parse_orderbook()` 会校验物品名出现在锚点附近 —— 不校验的话
会抓到隔壁查询的 data 对象，`AK-47 | Redline` 那条就是这样算出 1.24 的假汇率。

## 单位是美元分，而且改不动

`rgCompactBuyOrders` / `amtMaxBuyOrder` 的单位是**美元分**，`eCurrency` 恒为 1。
试过 `?currency=23`、`?cc=CN`、cookie `steamCountry=CN`、cookie `steamCurrency=CNY`
四种设法，数字和 `eCurrency` 一个都不变。

### 折算比不能用汇率

`Prisma Case` 盘口 `$1.17`，`priceoverview` 人民币中间价 `¥7.87`，比值 **6.73**。
实测六件稳定在 **6.36~6.86**，而真汇率是 7.1。

> **Steam 国区定价系统性比汇率低 5~10%。** 直接乘 7.1 会把人民币价高估约 6%，
> 方向正好是「让你以为更赚」—— 这是会让人亏钱的错误。

所以 `merge` 阶段每次都用 `priceoverview` 同时问一遍 USD 和 CNY，取
「人民币中间价 ÷ 美元中间价」的中位数当折算比，剔除 `$0.5` 以下的样本
（一分钱起，舍入会把误差放大几十倍）。标定失败才退回 `USD_CNY`，并在
health 里标红写「价格可能偏高约 6%」——页面顶部和 Actions Summary 都会
把这句话显出来，不会静默退回。

标定必须**有上限**：第一版没有，6 个样本 12 次请求全撞 429，每次退避到
45 秒还要重试两轮，合并 job 在这一步烧了 11 分钟，最后还是退回汇率。
现在每次请求只重试 1 次、连续空手 3 次就认输、总耗时到 90 秒收工。

`USD_CNY`（默认 7.10，可放仓库 Variables 或 `data/rate.json`）只用来把
Waxpeer 的美元买入价换成人民币——那是真的美元计价商品，跟国区定价无关。

### 盘口可信度：最低挂单价会骗人

物品页给的 `amtMinSellOrder` 是**最低挂单价**，只有「有足够多人挂着」时
才代表市场价。实测反例：`★ Talon Knife | Safari Mesh (Factory New)` 挂单
只剩 **1 件**，报 `$13.92`，而 Waxpeer 自报中间价 `$3.33` —— 差 4 倍。
第一轮真跑里 `ratio 3.03` 的「机会」就是这么来的。

合并阶段因此有两道门（`run.py` 的 `MIN_SELLS` / `MAX_DEV`）：

- 挂单少于 **10 件** → 丢。盘口太薄，那个价是孤零零的报价，不代表市场。
- 有第三方参考价时，偏离超过 **40%** → 丢。第三方自报的 `steam_price`
  是独立来源，用来对一下哪个盘口能信。

宁可少几条，也不能给出会让人真去下单的假数字。丢了多少、为什么丢，
会写进 health 和 Summary 里。

## 限流与吞吐（实测）

- 间隔 2.5 秒；429 时退避 `min(45, 3+4×连续次数) + 随机 0~3 秒`，不放弃。
- 连续 25 次 429 才收工（`--give-up`）。限流是**阵发**的：8 分钟压测里
  第 0–1 分成功 44 件 → 第 2–4 分几乎全挂 → 第 5–6 分又成功 41 件。
  按「连续三次就退出」会白扔掉恢复期那 41 件。
- 单 job 平均 **11.9 件/分钟**，12 分钟约 140 件。
- **连不上要和限流分开算**。物品页请求返 `HTTP 0`（DNS/TCP/TLS/超时）时
  等再久也没用，`--net-give-up 5`（默认 5 次）就收工；单请求超时 15 秒
  （`STEAM_TIMEOUT` 可调）。第五轮真跑四个 shard 全 0 成功、artifact 各
  386 字节，就是超时写 50 秒烧光的 —— 一件要等 100 秒，12 分钟只够试七件。
  这种情况 health 里 `steam_orders` 记 **❌**，detail 写「连不上」，不会
  被当成「接口正常但没数据」。
- **每个 job 拿到独立出口 IP**（11 轮探针 11 个不同 IP）→ 每个 job 一份独立
  限流额度。public 仓库给 20 个并发 job，用 4 个 shard ≈ 560 件/轮，
  cron 30 分钟一轮 ≈ 2.7 万件次/天，能覆盖 6580 个候选四遍。

## 时间预算（改参数前必看）

```
单 shard：120 件 × 2.5 秒 ≈ 5 分钟，加上 429 退避 → --deadline 12 足够
job 硬超时 18 分钟，采集自己 12 分钟收工 → 绝不会被硬杀
```

**绝不能只靠 job 的硬超时** —— 被硬杀时后面的 `upload-artifact` / `push`
根本不执行，整轮白跑。`--deadline` 到点就停，把已采到的写出去。

想加量要三个一起改：`--batch`、`--deadline`（≈ `batch×2.5/60 + 3` 分钟）、
job 的 `timeout-minutes`（要比 deadline 多 5 分钟以上）。

cron 也不能比一轮耗时更密：collect(18) + merge(12) 最坏 30 分钟，按 `*/30`
排刚好。再密就会堆队列，最后全是排队超时的废跑。

## 出口被限流时怎么办（2026-11-18 事故记录）

从 `2026-11-18T10:02Z` 起连续 8 轮，四个 shard 的 artifact 全是 386~394
字节（空），health 里 `steam_orders` 一律 `0/x 成功`，报错在 `429` 和
`HTTP 0` 之间跳；同时刻第三方接口完全正常（合并 6500+ 件）。

原因不是接口坏了，是我们自己把 Azure 那段出口打进了限流：每次 push 都
触发一轮 4×120 件采集，8 轮下来请求量远超探测阶段的压测，而 Steam 的
限流按 IP 计、恢复很慢，Azure 的 IP 又是共享的。当时的处置是把 `schedule`
注释掉、停止白烧 runner。

**判断出口现在通不通**：

```
GitHub → Actions → collect → Run workflow
  → batch 填 20（默认 120，探情况时用小的，省得又打一整轮请求）
  → Run
```

跑完看 Summary 那张表：

- `Steam 物品页` ✅ → 通的，不用管
- `Steam 物品页` ❌ 且 detail 写 `429` → 还在限流，别连着点。每点一次都
  在同一段 IP 上加一份负担，点得越勤恢复越慢，隔一两小时再看
- `Steam 物品页` ❌ 且 detail 写 `连不上` → 这台机器到不了 Steam，
  和限流无关

连着几轮都是 ❌ 的话，剩下的路只有换出口（境外 VPS 跑采集），或者放弃
真实求购价、改用第三方自报的 Steam 中间价近似（`thirdparty.py` 里的
`steam_mid_cny` 已经有了，只是不准）。

## 部署

1. 仓库设成 **public**（私有仓库 Actions 2000 分钟/月会超，光采集就 620 分钟/天）
2. `git push origin main`，之后 `.github/workflows/collect.yml` 每 30 分钟自动跑
3. **仓库 Settings → Pages → Source 选 "GitHub Actions"** —— 这一步必须手动做
   一次。workflow 里的 `configure-pages` 虽然传了 `enablement: true`，但
   `GITHUB_TOKEN` 无权创建 Pages 站点（实测 `POST /repos/.../pages` 返 403
   `Resource not accessible by integration`），第一次跑会在那步失败。
   设好之后，同一 workflow 里的 `deploy` job 会自动把 `_site/` 发上去。
4. 折算比自动标定，不用配。真想覆盖汇率才设 Variables 的 `USD_CNY`

站点地址是 `https://<用户名>.github.io/steam-arb/`。
前端所有请求都是相对路径，放项目页子路径下能直接用。

### 本机推代码：走 SSH over 443

本机 `github.com:443` 是不通的（解析到的那个 `20.205.243.166` 被墙），但
`ssh.github.com:443` 通 —— 所以推代码走 SSH，别用 HTTPS：

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

## 第一次跑完看哪里

Actions → collect → 最新一次 → 下面的 Summary。
`merge` job 的 Summary 里有一张「环节 / 状态 / 详情」表：

| 环节 | 正常 | 异常含义 |
| --- | --- | --- |
| 第三方买入价 | ✅ 合并 6580 件 | ❌ Skinport 和 Waxpeer 都失败 |
| Steam 求购价 | ✅ 560/560 | ❌ 一件都没采到 = 出口被 Steam 拦了 |
| 折算比 | ✅ 1 USD = ¥6.7x | ❌ 标定失败，退回汇率（价格偏高约 6%） |
| 入库 | ✅ 手上 560 件 | ❌ 没读合并到 shard |

采集那步失败也 `exit 0`（为了保住旧数据），所以**不要看 job 是绿是红**，
要看 Summary 里那张表。

想单独验证某个 shard 采到了什么，去 Actions 那次运行的 Artifacts 里下
`shard-0`，里面是原始盘口 JSON。

## 边界

- Steam 订阅协议禁止把市场数据用于商业目的。本站**不放广告、不导流到自家店铺**。
- 不提供代挂代卖、不做资金池。一旦涉及收钱，法律性质就完全不同了。
- 页面上必须写明数据延迟，别让人以为实时。数据超过 90 分钟没更新，前端会
  自己标红警告。
