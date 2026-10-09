# mining-daily-agent · 矿权日报 Agent

按 **MCP (Model Context Protocol)** 协议实现的一个端到端研究 Agent：
**3 个 MCP server + 1 个 Agent client**，输入一句话，输出一份带引用源链接的
Markdown 矿权简报。

```
$ python -m mining_agent "给我生成一份关于 Pilbara 锂矿的今日简报"
→ output/mining-briefing_...md
   # 矿权日报 · 给我生成一份关于 Pilbara 锂矿的今日简报
   ## 一、摘要        ## 五、风险提示
   ## 二、新闻动态     ## 六、观察点
   ## 三、资源储量     ## 七、引用源
   ## 四、价格走势     ## 八、运行记录
```

---

## 架构

```
                    ┌──────────────────────────────────────────┐
                    │  Agent client  (src/mining_agent/)        │
   自然语言查询  ──▶ │                                          │
                    │  cli.py         CLI / 参数 / 输出          │
                    │  orchestrator   ① ReAct 规划器（Claude）   │
                    │                 ② 规则化规划器（无 Key）    │
                    │  risks.py       规则化风险引擎             │
                    │  evidence.py    引用注册表 [1][2][3]…      │
                    │  report.py      Markdown 渲染（布局由代码  │
                    │                 掌控，模型只提供内容）      │
                    │  mcp_client.py  MCP hub（stdio / SSE）     │
                    └───────┬──────────────┬──────────────┬─────┘
                            │ MCP          │ MCP          │ MCP
                    ┌───────▼──────┐ ┌─────▼────────┐ ┌───▼──────────┐
                    │mining-news   │ │mineral-pdf   │ │lme-price     │
                    │-mcp          │ │-mcp          │ │-mcp          │
                    ├──────────────┤ ├──────────────┤ ├──────────────┤
                    │ search       │ │ extract_     │ │ get_price    │
                    │ fetch_article│ │  resources   │ │ get_trend    │
                    │              │ │ list_        │ │ list_        │
                    │              │ │  documents   │ │  commodities │
                    ├──────────────┤ ├──────────────┤ ├──────────────┤
                    │offline 语料库 │ │pdfplumber +  │ │离线价格序列   │
                    │newsapi.org   │ │NI43 解析器   │ │HTTP 行情 API │
                    │              │ │HTTP 下载     │ │              │
                    └──────────────┘ └──────────────┘ └──────────────┘
```

三个 server 都是**独立进程**，通过 MCP 协议通信：

- **stdio**（默认）—— agent 作为父进程拉起子进程，这也是 Claude Desktop / Cursor 的接法；
- **SSE** —— `docker-compose.yml` 用这种方式，三个 server 是各自独立的容器，
  agent 按 URL 连接，和上面那张图的分布式形态完全一致。

---

## 三个 MCP server

### 1. `mining-news-mcp` — 新闻聚合

| 工具 | 说明 |
| --- | --- |
| `search(query, days, limit)` | 关键词 + 时效性排序。CJK 查询按字与二元组切分，`锂矿` 能命中 `锂矿项目`；地域别名词典把 `Pilbara` 映射到具体项目 |
| `fetch_article(url)` | 取全文（离线走语料库，在线抓 HTML 并剥离标签） |

### 2. `mineral-pdf-mcp` — NI 43-101 解析

| 工具 | 说明 |
| --- | --- |
| `extract_resources(pdf_url)` | 提取 Measured / Indicated / Inferred / Total 资源量：吨位、品位、含金属量，并换算 **Li₂CO₃ 当量** |
| `list_documents()` | 列出可读文档，让模型**发现**合法 `pdf_url` 而不是猜 |

解析器（`servers/ni43.py`）与 PDF 库解耦，两级策略：

1. **有线表格** —— 取 `pdfplumber` 的 `extract_tables()`，表头带单位（`Tonnes (Mt)`、
   `Grade (% Li2O)`），类别词在数据行里；
2. **无线文本** —— 行级正则兜底，同时支持「行内带单位」与「单位在上方表头」两种排版。

换算用真实化学计量：`Li₂O → Li × 0.46457`，`Li → Li₂CO₃ × 5.32279`，
即 `1% Li2O ≈ 2.47% LCE` —— 这条业界经验值在测试里被断言。

### 3. `lme-price-mcp` — 价格行情

| 工具 | 说明 |
| --- | --- |
| `get_price(commodity, date)` | 单日收盘价；非交易日回溯至最近交易日，越界会**显式告警**而不是静默返回 |
| `get_trend(commodity, days)` | 日频序列 + 区间涨跌、最高最低、日收益率标准差 |
| `list_commodities()` | 品种、单位与别名表 |

品种别名归一化支持 `碳酸锂`、`Li2CO3`、`SC6`、`Ni`、`铜` 等写法。

---

## Agent 设计

### 双规划器，同一个交付契约

| | ReAct 规划器 | 规则化规划器 |
| --- | --- | --- |
| 触发 | 配了 `ANTHROPIC_API_KEY` | 无 Key / `--no-llm` / LLM 失败自动降级 |
| 决策 | 模型自主选择工具与参数 | 固定流水线 |
| 输出 | `BriefingContext`（完全一致） | `BriefingContext` |

两者共享 `BriefingRunner`（调用、记录、引用收集、产物归一化）与
`report.py`（排版）。**模型永远不直接产出 Markdown**——它调用
`submit_briefing` 提交结构化叙事，版式、引用编号、表格全部由代码生成。
这样格式稳定，且每一句话的数字都能追回某次工具调用。

### 引用是数据，不是提示词

`EvidenceStore` 为每个来源分配稳定编号。每次工具调用后，hub 会把
`_new_citations`（`{"1": "标题", "2": "项目名 — NI 43-101 ..."}`）附在结果里回给模型，
模型正文写 `[1]`，渲染器再把它接到「七、引用源」的真实链接上。
去重按 `(kind, url)`，所以同一个 PDF 不论是 `list_documents` 还是
`extract_resources` 拿到的，都只占一个编号。

### 风险提示来自规则引擎

`risks.py` 不调用模型，纯计算，因此两种规划器输出的风险口径一致：

- **价格**：区间涨跌幅、日波动率
- **储量**：Inferred 占比、是否缺少 Measured/Indicated、边界品位口径
- **新闻面**：把标题映射到 15 类主题（运营中断 / 审批申诉 / 政策税费 / 物流 …），
  所有正则都带词边界——否则 `executed` 里的 `cut` 会把每份承购公告误判成「指引下调」
- **数据面**：数据时效性、离线夹具声明

### 稳健性

- 模型侧：自适应思考（`thinking: adaptive`）+ `effort` 可调；显式处理
  `refusal` 与 `pause_turn`；启用服务端 refusal fallback（`fallbacks: "default"`），
  被 API 拒绝时自动降级重试。
- 工具侧：单个工具失败只记 warning 并继续；轮次耗尽则回退规则化摘要。
- 传输侧：stdio 子进程 stderr 默认丢弃——MCP 客户端用平台默认编码（中文 Windows 是
  GBK）解码子进程 stderr，任何 UTF-8 中文日志都可能在读取协程里抛异常并拖垮会话。
  server 端日志统一 ASCII 转义，需要时用 `MINING_AGENT_LOG_FILE` 落文件。

---

## 目录结构

```
mining-daily-agent/
├── src/mining_agent/
│   ├── cli.py                  CLI 入口
│   ├── config.py              环境变量 → Settings（单一事实来源）
│   ├── models.py              pydantic 领域模型
│   ├── mcp_client.py          MCP hub（stdio / SSE，工具命名空间化）
│   ├── evidence.py            引用注册表
│   ├── risks.py               规则化风险引擎
│   ├── orchestrator.py        ReAct + 规则化规划器
│   ├── report.py              Markdown 渲染
│   └── servers/
│       ├── common.py          日志 / HTTP 重试 / 夹具 / TTL 缓存
│       ├── news.py            mining-news-mcp
│       ├── mineral_pdf.py     mineral-pdf-mcp
│       ├── ni43.py            NI 43-101 解析规则（纯函数，易测）
│       └── lme_price.py       lme-price-mcp
├── scripts/
│   ├── make_fixtures.py       生成合成夹具（确定性）
│   ├── render_mcp_config.py   生成带绝对路径的 MCP 配置
│   └── package.py             打包交付
├── tests/                     79 个测试，含真实三进程端到端与 ReAct 循环
├── data/fixtures/             新闻语料 / 价格序列 / NI 43-101 PDF
├── examples/                  一份已生成的样例简报（md + json）
├── mcp-config.json            Claude Desktop / Cursor 直接用
├── mcp-config.docker.json     SSE 版本
├── docker-compose.yml         3 server + 1 agent
├── Dockerfile                 一份镜像，四种角色
├── RUN.md                     5 分钟上手
└── .env.example               全部可调项
```

---

## 工程化

- **配置**：所有开关走环境变量，`config.py` 是唯一读取点；`Settings.resolve_*_provider()`
  把 `auto` 解析成具体 provider（有 Key 走在线，否则走夹具）。
- **日志**：结构化可选（`MINING_AGENT_LOG_JSON=1`），stdio 下强制 stderr 且 ASCII 安全。
- **HTTP**：指数退避重试、体积上限、超时；`ProviderError` 统一上抛。
- **测试**：`pytest` 79 例。覆盖解析规则、别名归一化、风险规则、渲染；
  **真的把三个 server 拉起来**跑完整简报（`tests/test_e2e_offline.py`）；
  以及用打桩客户端驱动 ReAct 循环，验证工具派发、引用注入、`submit_briefing`
  捕获、拒答降级、`pause_turn` 续跑、轮次耗尽兜底（`tests/test_react_planner.py`）。
- **容器**：非 root 运行；compose 带 socket healthcheck 与 `service_healthy` 依赖。

---

## 数据声明

`data/fixtures/` 下**全部为合成演示数据**，由 `scripts/make_fixtures.py` 生成：

- 公司名、项目名（Mt Pilbara / Yarrie Ridge / Coongan Gap）、资源量数字、新闻标题、
  价格序列均为虚构，不对应任何真实矿权或行情；
- 新闻链接指向保留域名 `*.example`，不可访问；
- 目的是让整条链路（MCP 传输、PDF 解析、引用编号、风险规则）在无网络无 Key 的
  环境下可复现、可测试。`data/fixtures/manifest.json` 与每份简报都带有该声明。

接真实数据源的方式见 `RUN.md`。

---

## 许可

演示代码，按原样提供，不构成投资建议。
