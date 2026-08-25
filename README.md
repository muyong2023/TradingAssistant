# 交易小助手

美股 watchlist 的看盘助手：抓行情、算指标、推 Telegram 告警、本地网页看板。
**只做分析和提醒，不接下单接口，不构成投资建议。**

## 能做什么

| 能力 | 说明 |
|---|---|
| 盘前简报 | 交易日 09:00 ET 推送隔夜变动与当日关注点 |
| 盘中告警 | 09:30–16:00 每 5 分钟检查，触发涨跌幅分档或 RSI 极值时推送 |
| 盘后复盘 | 16:15 ET 推送领涨领跌与信号变化 |
| 网页看板 | 概览表 + 个股详情（价格/均线/RSI 图表） |
| 命令行 | `./ta.sh scan` 直接看技术面状态表 |

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp config/.env.example config/.env    # 填入四把 key
python3 scripts/get_chat_id.py        # 取 Telegram chat_id

./ta.sh scan                          # 命令行扫描
./ta.sh web                           # 看板 http://127.0.0.1:8787
python3 scripts/install_launchd.py --install   # 装定时任务（macOS）
```

## 需要的凭据

四项都写在 `config/.env`（已在 `.gitignore` 中，切勿提交）：

- `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET` — [alpaca.markets](https://alpaca.markets) Paper 账户即可
- `TELEGRAM_BOT_TOKEN` — 向 [@BotFather](https://t.me/BotFather) 发 `/newbot` 获取
- `TELEGRAM_CHAT_ID` — 先给 bot 发一条消息，再跑 `scripts/get_chat_id.py`
- `ANTHROPIC_API_KEY` — 供后续的报告撰写功能使用

## 数据源分工

两个源不是互为备份的等价物，而是各司其职：

- **Alpaca**（IEX feed）负责实时报价。免费档的成交量只有 IEX 一家的量，
  实测 NVDA 为 278 万，而全市场合并量是 8676 万，相差 31 倍 ——
  故 `Quote.volume_is_partial` 会标记出来，量能指标不使用它。
- **yfinance** 负责历史日线（合并成交量）与基本面数据，行情延迟约 15 分钟。

`ta/data/router.py` 按用途路由，主源失败时降级到另一源并在界面上标注。

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
├── cli.py           命令行
└── web/             FastAPI 看板 + 服务端 SVG 图表
```

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

RSI 以 Wilder《New Concepts》原书的 14 日样例（70.53）作为基准。

## 已知限制

- 上市不足 200 个交易日的标的（近期 IPO）无 MA200，显示为"数据不足"。
- 盘中的量比是按已过时段比例折算的全日预估，界面上以 `*` 标注。
- launchd 在 Mac 睡眠期间不触发，唤醒后会补跑错过的任务，时间会晚。
