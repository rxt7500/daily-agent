# RUN.md — 5 分钟跑起来

两条路径，**都不需要 API Key、不需要联网**：仓库内置了一套合成夹具数据
（新闻语料 + NI 43-101 PDF + 5 个品种的日频价格序列），所以第一次运行一定成功。

---

## 路径 A：本地 Python（约 2 分钟）

```bash
cd mining-daily-agent

# 1. 环境（Python ≥ 3.10）
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. 跑起来
python -m mining_agent "给我生成一份关于 Pilbara 锂矿的今日简报"

# Windows + 中文控制台若报 UnicodeEncodeError，先执行：
#   set PYTHONUTF8=1        (cmd)
#   $env:PYTHONUTF8=1       (PowerShell)
```

**预期输出**（stdout 是运行摘要，完整简报落在文件里）：

```json
{
  "planner": "deterministic",
  "mode": "offline",
  "reference_date": "2026-10-08",
  "news": 7,
  "resource_tables": 3,
  "resource_rows": 10,
  "price_trends": 2,
  "risks": 8,
  "citations": 9,
  "tool_calls": 10,
  "warnings": 0,
  "output": ".../output/mining-briefing_..._20261009.md",
  "json": ".../output/mining-briefing_..._20261009.json"
}
```

打开那个 `.md` 就是交付物本体：摘要 → 新闻 → 储量 → 价格 → 风险 → 引用源 → 运行记录。

一条命令跑完整验证（单元测试 + 真实三进程 MCP 端到端）：

```bash
pip install pytest
python -m pytest -q          # 期望：72 passed
```

---

## 路径 B：Docker Compose（约 4 分钟，主要是首次构建）

```bash
cd mining-daily-agent
docker compose up --build
```

这会起 **4 个容器**：三个 MCP server 各自以 **SSE** 方式监听 `8001/8002/8003`，
`agent` 容器等它们 healthy 之后，通过 `mcp-config.docker.json` 里的 URL 连上去，
把简报写到宿主机 `./output/briefing.md`。

只起 server、自己接任意 MCP 客户端：

```bash
docker compose up -d mining-news-mcp mineral-pdf-mcp lme-price-mcp
# SSE 端点： http://localhost:8001/sse  http://localhost:8002/sse  http://localhost:8003/sse
```

> **SSE + 非 localhost 主机名**：MCP 的 SSE 传输会校验 `Host` 头（防 DNS rebinding），
> 默认只允许 localhost。用容器名 / 局域网地址访问时，必须把它加进
> `MCP_ALLOWED_HOSTS`（compose 里已经为三个 service 配好），否则连接会以 421 被拒。

关掉：

```bash
docker compose down
```

---

## 接到 Claude Desktop / Cursor

`mcp-config.json` 就是这个格式的文件，直接可用：

```bash
pip install -e .        # 让 `python -m mining_agent.servers.*` 全局可用
```

- **Claude Desktop**：把 `mcp-config.json` 里的 `mcpServers` 合并进
  `claude_desktop_config.json`
  （macOS：`~/Library/Application Support/Claude/`，Windows：`%APPDATA%\Claude\`），重启客户端。
- **Cursor**：`Settings → MCP → Add new MCP server`，或把同样的 `mcpServers`
  块写进 `.cursor/mcp.json`。

重启后应看到 3 个 server、共 7 个工具：

| Server | 工具 |
| --- | --- |
| `mining-news-mcp` | `search` · `fetch_article` |
| `mineral-pdf-mcp` | `extract_resources` · `list_documents` |
| `lme-price-mcp` | `get_price` · `get_trend` · `list_commodities` |

不想 `pip install -e .`？生成一份带绝对路径的配置：

```bash
python scripts/render_mcp_config.py -o mcp-config.local.json
```

---

## 用上 LLM 规划器（可选）

不配 Key 时走**规则化规划器**（固定流水线，输出结构完全一致）。
配了 Key 就走 **ReAct 循环**：模型自己决定搜什么、读哪篇、比哪几个价格，最后调用
`submit_briefing` 提交结构化简报。

```bash
export ANTHROPIC_API_KEY=sk-ant-...          # Windows: set ANTHROPIC_API_KEY=...
python -m mining_agent "给我生成一份关于 Pilbara 锂矿的今日简报" --print
```

模型默认 `claude-opus-5`，可用 `--model` / `MINING_AGENT_MODEL` 覆盖。
**任何** LLM 侧失败（无 Key、限流、拒答、工具轮次耗尽）都会自动降级到规则化规划器，
所以永远不会拿不到简报。

---

## 接上真实数据源（可选）

三个 server 都是「真实 provider + 离线夹具」双实现，改环境变量即可切换：

```bash
cp .env.example .env

# 新闻：newsapi.org
NEWS_API_KEY=your_key
MINING_AGENT_NEWS_PROVIDER=newsapi

# 价格：任何返回 JSON 的行情服务
MINING_AGENT_PRICE_PROVIDER=http
MINING_AGENT_PRICE_BASE_URL=https://your-api.example/v1
MINING_AGENT_PRICE_API_KEY=...

# PDF：允许下载远程技术报告
MINING_AGENT_DATA_MODE=live
MINING_AGENT_PDF_ALLOW_NETWORK=1
```

价格接口约定：

```
GET {base}/price?commodity=lithium-carbonate&date=2026-10-08
    -> {"close": 8509.9, "date": "2026-10-08", "unit": "USD/t", "source": "..."}
GET {base}/trend?commodity=lithium-carbonate&days=30
    -> {"points": [{"date": "2026-10-08", "close": 8509.9}, ...], "unit": "USD/t"}
```

---

## 排查

| 现象 | 原因 / 处理 |
| --- | --- |
| `MCP 连接失败：failed to connect to ...` | 用 `python -m mining_agent.servers.news` 直接跑该 server，看 stderr 报错 |
| 想看到 server 日志 | stdio 下 server 的 stderr 默认被丢弃（避免 Windows GBK 解码崩掉传输层）。设 `MINING_AGENT_LOG_FILE=./output/servers.log`，或 `MINING_AGENT_MCP_STDERR=1` 直通到终端 |
| 控制台中文乱码 | 只影响日志显示。日志已强制 ASCII 转义；简报文件本身就是 UTF-8 |
| 简报里新闻/价格是演示数据 | 正常 —— 默认 `offline`。`MINING_AGENT_DATA_MODE=live` + 上面的 Key 可切真实源 |
| 新闻为空 / `window_expanded: true` | 请求窗口内没有内容，server 自动放宽到语料最新条目，并在报告里标注 |
| `docker compose up` 卡在 healthcheck | `docker compose logs mining-news-mcp`；端口 8001-8003 被占用时改 compose 里的映射 |
| 想重新生成夹具 | `python scripts/make_fixtures.py`（确定性，同参数同结果） |

---

## 交付产物

| 文件 | 说明 |
| --- | --- |
| `src/mining_agent/servers/news.py` | **MCP server 1** — `mining-news-mcp` |
| `src/mining_agent/servers/mineral_pdf.py` + `ni43.py` | **MCP server 2** — `mineral-pdf-mcp` |
| `src/mining_agent/servers/lme_price.py` | **MCP server 3** — `lme-price-mcp` |
| `src/mining_agent/orchestrator.py` | **Agent 编排** — ReAct 循环 + 规则化规划器 |
| `src/mining_agent/mcp_client.py` | MCP 客户端（stdio / SSE 双传输） |
| `src/mining_agent/report.py` | Markdown 简报渲染 + 引用编号 |
| `mcp-config.json` | 可直接接 Claude Desktop / Cursor |
| `docker-compose.yml` | 一条命令起全栈 |
| `output/*.md` | 生成的简报 |
