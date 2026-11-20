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

# 3. 自检（141 项，全离线）
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

## 折、面值比、折现净额

挂刀圈说的是「**折**」：7.9 折 = 花 79 元买到 100 元余额，**数字越小越划算**。
页面主列就是它，`折 = 买入价 ÷ Steam 净得 × 10`。面值比是它的倒数
（`净得 ÷ 买入价`），只当小字留着，老用户看惯了。

| 指标 | 公式 | 含义 |
| --- | --- | --- |
| 折 | 买入价 ÷ 净得 × 10 | 7.9 折 = 79 元拿 100 元余额，越小越好 |
| 面值比 | 净得 ÷ 买入价 | 折的倒数，≥ 1 才不亏面值 |
| 折现净额 | 净得 × 折现系数 − 买入价 | 余额当现金只能按 0.75~0.85 用 |

**三种卖法（三个口径）**，同一件物品能差出好几折：

| 口径 | 卖在哪 | 代价 |
| --- | --- | --- |
| 求购价卖（`ratio`） | Steam 最高求购价 | 立刻成交，但价最低，常只有一件的深度 |
| 稳定求购（`ratio3`） | 求购价**第三档** | 卖几件还吃得到，比最高价低一点 |
| 寄售价卖（`ratio_sell`） | 最低挂单价 | 价最高，但要等有人买，可能等几天 |

顶栏三个按钮就是切它，全页跟着变（榜、表、计算器、CSV）。默认「求购价卖」：
快进快出，代价算得出来。「稳定求购」是多件时的真实水平 —— 最高求购价常只挂
一件，拿它估整批会算得太漂亮。

**临界折**：折现系数 0.8 时，只有折 ≤ 12.5 才真赚钱（`1/0.8×10`）。页面上
写着这个数，改系数它会跟着变。

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

- 间隔 2.5 秒；429 时退避 `min(封顶, 1+1×连续次数) + 随机 0~1.5 秒`，封顶
  默认 6 秒（`STEAM_BACKOFF_CAP` 可调），不放弃。
  第一版的退避是 `min(45, 3+4×次数)`，12 分钟里光睡就 576 秒、只试到 10 件
  全 429 —— 而同期单发探针能拿 200，说明限流是**窗口式**的，长睡没用，
  不如多试几次赶上恢复窗口。
- 连续 40 次 429 才收工（`--give-up`）。限流是**阵发**的：8 分钟压测里
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
真实求购价、改用第三方自报的 Steam 中间价近似 —— 后者已经做成了「估算
模式」，见下一节。

## 挂刀指数

页签下面那条是**全站中位折**：把当天 `hist` 表里所有物品的折取中位数，按天连起来。
一件挂单只剩一件的离谱高价能把平均值拽上天，所以取中位不取平均；一天至少
10 件才画点（`floor = 10`），不然收工早的那天会被当成「行情忽然变好」。

前端只有一天数据时不画线（一个点连不成趋势），会写「基本持平」。

## 多平台同屏、流动性、成交量

这三样都是参考「挂刀行情站」的做法补的，目的只有一个：让「看着便宜」和
「真的能赚」分开。

### 平台报价（`bsp` / `bwp` / `qsp` / `qwp`）

`thirdparty.merge` 以前只留「更便宜那家」的价格。问题是页面上只能写
「某处更便宜」，说不出便宜多少，也看不出另一家有没有货。现在两家各自的
最低价和库存都留下（`buy_skinport` / `buy_waxpeer` / `qty_skinport` /
`qty_waxpeer`），导出成 `bsp` / `bwp` / `qsp` / `qwp`。

**没货写 `None`，不写 `0`** —— `0` 会被当成「免费」参与比价。
同一件两家的价差常有 3~5%，页面上加粗的就是更便宜那家。

### 流动性（前端算，不占字段）

`liqOf()` 把三项合成一档：买盘前 10 档深度 ≥ 50 件、买单条数 ≥ 500、
挂单件数 ≥ 100，三项里够两项算「好」、够一项算「中」、否则「差」。

不用后端算、不入库，因为它的阈值随行情走，改一次不该动数据库。估算行
返回 `null` 显示「—」—— 没有盘口就别给一个「好」，那会误导人。

全表里也有一个「买盘深度」列，两个都留着：深度是原始数字，流动性是给人
扫一眼用的结论。

### 24 小时成交量（`vol`）

唯一可靠来源是 Steam `priceoverview` 的 `volume`，而那个接口正是被限流封的。
不额外发请求的做法：标定折算比时本来就会问这个接口几件，顺手把 `volume`
带回来（`market_factor` 的返回值里多一个 `vols`）。

所以**大多数行是空的**，`vol` 空着表示「没拿到」，不是「0 件成交」—— 这两个
意思差得远，前端显示成「—」而不是 `0`。等出口恢复、能正常采全量了，这一列
才会变厚。

### 周转天数与年化（计算器）

倒余额里真正影响收益的不是某个「7 天冷却」标记 —— 我们买的是第三方、
卖到 Steam，Steam 侧没有锁；卡资金的是发货 1~2 天、挂单成交 1~5 天、余额
用掉再几天。所以计算器里给的是「钱要压几天」这一项（默认 7 天）和一个年化：

```
年化 = (1 + 净赚/花的钱) ^ (365/天数) - 1
```

同样 10% 的利润，7 天一轮和 30 天一轮，一年差好几倍 —— 这才是要不要动手的
关键。**亏钱时不给年化**：复利会把 -26% 算成 -100%，看着像本金亏光，
其实是「一年一直这么亏才会亏光」，这种夸张不如不给。

### 没有做的：假的冷却标记

一开始想过给每件物品标「7 天冷却」，**这是错的**。我们买入走的是第三方站点，
卖出去走 Steam 社区市场，Steam 侧对新买入的物品没有冷却；真正的时间成本是
成交和提现周期，已经放进上面那个「周转天数」里了。给每件标一个假冷却只会
让人以为要等 7 天才能卖。

## 估算模式：拿不到盘口时页面别空着

Steam 一件都采不到时，页面只剩往轮的旧数字，看着像站坏了。所以 `merge`
还会做一件事：把第三方候选（6000 多件）里没被实测覆盖的那些，用第三方自报
的 **Steam 中间价**折算出一个「折」，作为兜底填充。

```bash
python collector/run.py --mode merge --in-dir ... --est-cap 1500   # 0 = 关掉
```

**这些行一律带 `est: 1`**，导出时 `latest.json` 里也分开计数（`real` / `est`），
前端据此：

- 行加虚线左边框、整行压暗，名字后面挂一个虚线「估」标；
- 无论按哪列排序，估算行永远排在实测行**后面**；
- 求购价、买盘深度、挂单件数都是空（`null`）—— 这些数根本没采到，编不得；
- **不进折扣榜、不进计算器**。那两处给的是「花 X 买 → 卖掉净得 Y」，Y 要是
  估的，就等于拿假账教人下单；
- 顶部 warning 和页头计数写明「N 件（M 实测 + N 估）」；
- 有「只看实测」开关（地址栏 `#real=1`）。

**为什么不能当真价**：中间价是最近成交价的中位数，不是此刻就能成交的求购价，
通常比真能卖到的价**高 10~20%**；第三方数据本身也有延迟。所以估算行的折
系统性偏乐观（看着比实际更划算） —— 它的用途只是「有个能看的榜单、把明显不划算的排掉」，
不是「照着它去买」。估算门槛 `MIN_EST = 0.95`，比这还差的直接不显示。

出口通了以后自然会被实测行挤出去（同一件已经实测过的，估算那行会在
`build_est_rows` 里按 `real_names` 剔掉），不需要手动关。

## 部署

1. 仓库设成 **public**（私有仓库 Actions 2000 分钟/月会超，光采集就 620 分钟/天）
2. **注意 `on.schedule` 现在是注释掉的**（2026-11-18 出口被封之后停的）。
   要恢复自动跑，把 `.github/workflows/collect.yml` 里 `schedule` 那段注释
   解开；在那之前只有手动 `Run workflow` 才会跑。
   一旦开了，`git push origin main` 也会触发一轮。
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
| 折算比 | ✅ 1 USD = ¥6.7x | ❌ 标定失败，沿用上次的值或退回汇率（偏高约 6%） |
| 入库 | ✅ 手上 560 件 | ❌ 没读合并到 shard |
| 估算 | ✅ N 件按中间价估 | ❌ 第三方候选也拿不到，只能导出旧数据 |

Summary 底部那行会写「手上 N 件（实测 A + 估算 B）」，B 不为 0 就说明
Steam 那边没打通，页面上的数字是估的。

采集那步失败也 `exit 0`（为了保住旧数据），所以**不要看 job 是绿是红**，
要看 Summary 里那张表。

想单独验证某个 shard 采到了什么，去 Actions 那次运行的 Artifacts 里下
`shard-0`，里面是原始盘口 JSON。

## 边界

- Steam 订阅协议禁止把市场数据用于商业目的。本站**不放广告、不导流到自家店铺**。
- 不提供代挂代卖、不做资金池。一旦涉及收钱，法律性质就完全不同了。
- 页面上必须写明数据延迟，别让人以为实时。数据超过 90 分钟没更新，前端会
  自己标红警告。
