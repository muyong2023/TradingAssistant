# 交易小助手

美股 watchlist 的看盘助手：抓行情、算指标、推 Telegram 告警、本地网页看板。
**只做分析和提醒，不接下单接口，不构成投资建议。**

## 能做什么

| 能力 | 说明 |
|---|---|
| **自选股管理** | 网页 `/watchlist` 点选增删，或 Telegram `/add` `/remove` `/list`，或命令行 |
| **RSI 信号** | 09:30–16:00 每 5 分钟检查，**日线**与 **5 分钟线** RSI 触及 20/80 时分别推送 |
| **定点巡检** | 交易日 07:00 / 09:30 / 12:00 / 14:00 ET 各一次，无论有无信号都回报 |
| 盘前简报 | 交易日 09:00 ET 推送隔夜变动与当日关注点（*当前关闭*）|
| 盘后复盘 | 16:15 ET 推送领涨领跌与信号变化（*当前关闭*）|
| 宏观日历 | FOMC 会议、CPI / 非农 / PCE 等发布日程 |
| 财报日程 | watchlist 各股财报日期与分析师预期 |
| 网页看板 | 概览表 + 个股详情（价格/均线/RSI 图表） |
| Telegram 问答 | 直接向 bot 提问，Claude 调工具查实时数据、联网搜索后作答 |
| 命令行 | `./ta.sh scan` 直接看技术面状态表 |

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp config/.env.example config/.env    # 填入四把 key
python3 scripts/get_chat_id.py        # 取 Telegram chat_id

./ta.sh scan                          # 命令行扫描
./ta.sh web                           # 看板 http://127.0.0.1:8787
python3 scripts/install_launchd.py --install   # 装定时任务 + 常驻 bot（macOS）
```

## 需要的凭据

四项都写在 `config/.env`（已在 `.gitignore` 中，切勿提交）：

- `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET` — [alpaca.markets](https://alpaca.markets) Paper 账户即可
- `TELEGRAM_BOT_TOKEN` — 向 [@BotFather](https://t.me/BotFather) 发 `/newbot` 获取
- `TELEGRAM_CHAT_ID` — 先给 bot 发一条消息，再跑 `scripts/get_chat_id.py`
- `ANTHROPIC_API_KEY` — Telegram 问答功能使用
- `FRED_API_KEY`（可选）— [免费申请](https://fredaccount.stlouisfed.org/apikey)。
  配了就用美联储官方的经济发布日历（CPI / PPI / PCE / 非农 / 零售 / GDP），
  不配则退回 Nasdaq 的公开接口 —— 后者较远的未来日期填充不全。

## 数据源分工

两个源不是互为备份的等价物，而是各司其职：

- **Alpaca**（IEX feed）负责实时报价。免费档的成交量只有 IEX 一家的量，
  实测 NVDA 为 278 万，而全市场合并量是 8676 万，相差 31 倍 ——
  故 `Quote.volume_is_partial` 会标记出来，量能指标不使用它。
- **yfinance** 负责历史日线（合并成交量）与基本面数据，行情延迟约 15 分钟。

`ta/data/router.py` 按用途路由，主源失败时降级到另一源并在界面上标注。

## 日历数据的三个来源

按可靠性分层，上层覆盖下层，界面上区分标注，不把推测当确定：

1. **FOMC** — 抓美联储官网，权威。带 `*` 的会议附经济预测与记者会。
2. **经济发布** — 配了 `FRED_API_KEY` 用官方 API；否则退回 Nasdaq。
3. **规则推导** — 仅填补上层没给出的项（初请失业金每周四、非农每月首个周五），
   标注为"预计"。

**Nasdaq 接口的 `date` 参数偏移一天**：查 `date=D` 返回的是 `D-1` 的事件，
代码里已修正。判定依据见 `ta/data/econ.py` 的模块说明。

**FRED 的 release id 必须核对后再写。** 凭印象填会静默展示张冠李戴的数据 ——
`54` 是 Personal Income and Outlays（PCE）而非就业报告，
`21` 是 H.6 Money Stock Measures 而非零售销售。
核对方式：`GET /fred/releases`。

## 功能开关

代码全部保留，用 `config/config.yaml` 的开关控制启停。当前只开了自选股管理与
RSI 信号两项，其余为省 API 额度而关闭：

```yaml
jobs:
  premarket: false    # 盘前简报
  postclose: false    # 盘后复盘
  intraday: true      # RSI 检查

alerts:
  rsi_alert: true
  pct_move_alert: false     # 涨跌幅分档告警
  volume_spike_alert: false

chat:
  enabled: false      # 自由问答——本项目唯一消耗 token 的功能
news:    { enabled: false }
macro:   { enabled: false }
earnings: { enabled: false }
```

关掉问答后 bot 仍然常驻，接受 `/add` `/remove` `/list` `/status` 等命令 ——
那些是纯本地逻辑，一个 token 都不花。

改完开关后重装定时任务使其生效（安装脚本会跳过已关闭的任务）：

```bash
python3 scripts/install_launchd.py --install
```

## 自选股怎么改

三个入口改的是同一份 `config/config.yaml`，改完下次检查自动生效，无需重启。

**标的：**

- **网页** http://127.0.0.1:8787/watchlist —— 点 × 移除，填代码选分组添加
- **Telegram** `/list` `/add ORCL growth_ai` `/remove ORCL`
- **命令行** `./ta.sh list` / `./ta.sh add ORCL -g growth_ai` / `./ta.sh remove ORCL`

**分组：**

- **网页** 「新建分组」表单填标识、显示名与阈值；每组右上角可删
- **Telegram** `/newlist dividend 高股息 6 11` / `/dellist dividend [force]`
- **命令行** `./ta.sh newlist dividend 高股息 --low 6 --high 11` / `./ta.sh dellist dividend -f`

分组存在的意义是**按波动性分开设阈值**：防御股跌 5% 是大事，高波动股日内 ±15%
才值得响，用一个阈值必然导致该响的不响、不该响的天天响。

删除非空分组需要显式 force —— 一条命令删掉一串自选股太容易误操作；
也不允许删到一个组都不剩。

添加前会到交易所资产库核对代码是否存在。行情接口对不存在的代码只是静默返回
空数据、不会报错 —— 早期把苹果写成 `APPL`（正确是 `AAPL`）就是这样混进配置的。

写入是对 YAML 的**定点文本编辑**，只替换 `symbols: [...]` 方括号内的内容。
读出后整份 dump 回去会抹掉配置里全部注释，那些注释是配置的一半价值。
因此该文件的 symbols 必须保持方括号写法，改成多行列表会被拒绝并给出提示。

网页的写接口会校验 `Origin`：页面无认证地监听在回环地址，而浏览器允许任意
站点向 127.0.0.1 提交表单，没有这道检查，别处打开的恶意页面就能悄悄改你的自选股。

## 两种推送的分工

- **告警**（每 5 分钟）—— 只在 RSI 真触及 20/80 时响，同一标的同方向每日一次。
- **巡检**（每日四次）—— 无论有无信号都回报，附扫描只数、RSI 区间与最接近阈值的标的。
  只说"没有信号"无法确认程序还活着，带上这些才看得见它确实在算。

巡检时刻在 `config/config.yaml` 的 `checkpoints`，改完需重装定时任务。
07:00 那次在盘前，分钟线读数是上一交易时段的收尾，报告里会标注。
非交易日不巡检 —— 周末推一条"一切正常"只是噪音。

Telegram 的 `/check` 随时手动触发同一份巡检。它刻意**不写去重表**：
手动查看若占用当日额度，会把后面真正的自动告警吞掉。

## 双时间尺度的 RSI

日线与 5 分钟线是**两路独立信号**，独立去重、分别成条推送。实测同一天里
MSFT 可以日线超买 66.2、同时 5 分钟线超卖 43.9 —— 时间尺度不同，混在一条
消息里容易把短线噪音当成趋势信号。

5 分钟线取自 Alpaca 免费档的 IEX feed，**过滤掉盘前盘后**：那些 K 线极稀疏
（实测 NVDA 08:05 那根只有 140 股），几笔零星成交就能把 RSI 拉到极值。

## 配置

全部在 `config/config.yaml`，改完下次任务运行时自动生效。

告警阈值**按分组设定**而非全局统一：消费防御股跌 5% 是重大事件，
高波动股日内 ±15% 属家常便饭，用一个阈值必然导致该响的不响、不该响的天天响。

```yaml
watchlists:
  defensive:
    alert: { pct: [5, 9] }      # 触发 5% 推一次，继续跌破 9% 再推一次
    symbols: [COST, JNJ, KO, ...]
```

同一标的同一档位每个交易日只推送一次（去重记录在 SQLite），但升档会再推。

## Telegram 问答

`ta/bot.py` 长轮询接收消息（这台机器在 NAT 后面，webhook 需要域名和端口转发，
长轮询不需要任何暴露），交给 `ta/chat.py` 用 Claude 回答。

**只响应 `config/.env` 里配置的那个 `TELEGRAM_CHAT_ID`。** bot 用户名是公开可搜的，
不加这道门，陌生人的提问会消耗你的 API 额度，还能通过工具读到你的持仓。
对未授权的 chat 完全静默 —— 回一句"无权访问"等于确认 bot 存在。

数据以**工具**形式提供给模型，而非一次性塞进 prompt：全量塞入每轮要几万 token，
且模型看到的永远是快照；做成工具则问哪只查哪只，回答里的数字必定来自实时接口。
模型被明确要求不依赖自身记忆陈述公司近况或股价 —— 训练数据有截止日期。

除本地数据工具外还挂了 Anthropic 托管的 `web_search`，用于本地数据源覆盖不到的
内容：Reddit 讨论、散户情绪、媒体报道、行业分析。**X/Twitter 覆盖很差** ——
其内容 2023 年后基本锁在登录墙后，搜索引擎索引不到，能搜到的多是第三方转载。
系统提示要求模型把论坛观点标注为观点、注明出处与时效，且股价一类的事实一律
走本地工具而非搜索。

联网搜索的一轮问答实测 40 秒以上。等待期间 bot 会先发一条占位消息，随模型调用
工具实时改写它（已完成的步骤划掉、当前步骤显示为进行中），答案出来后**就地替换**
这条占位消息，不留下"正在查…"的残迹。同时后台线程每 4 秒续发一次"正在输入"提示
——Telegram 的这个提示只维持约 5 秒，不续发就会提前消失。带服务端工具的回合可能以 `pause_turn` 中断，
Python 版 tool runner 不会自动续跑（会静默返回截断的答案），`ta/chat.py` 在外层
重启续跑最多 3 次。

对话历史只保存纯文本，不保存 thinking 与 tool_use 块：跨轮重放会让上下文迅速膨胀，
而模型下一轮本来就会按需重新调用工具。

## 项目结构

```
ta/
├── config.py        配置与凭据加载
├── market.py        交易时段计算
├── data/            双源数据层（base/alpaca/yahoo/router）
├── indicators.py    SMA/EMA/RSI/量比，纯函数
├── alerts.py        告警规则与去重
├── reports.py       晨报与盘后报告正文
├── notify/          Telegram 推送
├── store.py         SQLite 持久化
├── jobs.py          定时任务入口
├── chat.py          问答工具集与 Claude 调用
├── bot.py           Telegram 长轮询循环
├── macro.py         FOMC 与宏观日历
├── earnings.py      财报日程
├── cli.py           命令行
└── web/             FastAPI 看板 + 服务端 SVG 图表
```

## 凭据安全

`scripts/install_hooks.sh` 安装 pre-commit 钩子，提交前扫描暂存区里是否出现
`config/.env` 里的真实凭据，命中就拒绝提交。**克隆后请运行一次。**

代码里所有对外部错误的日志都经过 `ta.config.redact()`。这不是预防性洁癖：
FRED 把 api_key 放在查询参数里，`requests` 的 `raise_for_status()` 会把整条 URL
写进异常消息，曾导致密钥明文落进 `logs/premarket.err.log`。

`.gitignore` 里忽略缓存目录用的是根锚定的 `/data/`。写成 `data/` 会匹配任意层级，
把 `ta/data/` 这个源码包一起吞掉 —— 曾导致 `ta/data/econ.py` 从未进入仓库。
另外 `.gitignore` 不支持行尾注释，注释必须独占一行。

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

RSI 以 Wilder《New Concepts》原书的 14 日样例（70.53）作为基准。

## 已知限制

- 上市不足 200 个交易日的标的（近期 IPO）无 MA200，显示为"数据不足"。
- 盘中的量比是按已过时段比例折算的全日预估，界面上以 `*` 标注。
- launchd 在 Mac 睡眠期间不触发，唤醒后会补跑错过的任务，时间会晚。
