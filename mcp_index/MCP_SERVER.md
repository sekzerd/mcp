# MCP Index Server

- **名称**：`mcp-index`
- **版本**：`1.0.0`
- **协议**：MCP Streamable HTTP（JSON-RPC 2.0）+ REST/JSON（FastAPI）
- **默认地址**：`http://127.0.0.1:8100`
- **MCP 端点**：`POST http://127.0.0.1:8100/mcp`
- **本说明**：`GET /mcp.md`（agent 可直接读取的 markdown）
- **OpenAPI 文档**：`GET /docs` 或 `GET /openapi.json`
- **代码目录**：`mcp_index/`
- **注册表文件**：`mcp_index/registry.json`
- **首次启动种子**：`mcp-seedream`

本服务是 **MCP 的索引 / 注册中心**。它不生成图片、不执行具体业务工具，只回答：

1. 现在有哪些 MCP 服务？
2. 每个服务暴露了哪些工具，参数 schema 是什么？
3. 应该调用哪个地址，文档在哪里？
4. 上游现在是否健康？

Agent 的典型流程是：**先查索引 → 拿到目标 MCP 的地址与工具 schema → 再直接调用目标 MCP**。
索引本身也可以通过 MCP 协议访问，因此支持 MCP 的客户端可以把本服务注册为一个工具源。

---

## Agent 快速开始

1. 读取本文件：`GET http://127.0.0.1:8100/mcp.md`
2. 列出服务：`GET http://127.0.0.1:8100/api/mcps`，或 MCP 工具 `list_mcps`
3. 查看详情：`GET http://127.0.0.1:8100/api/mcps/mcp-seedream`，或 MCP 工具 `get_mcp`
4. 按详情里的 `base_url` / `tools[].endpoint` 调用目标服务

```bash
# 机器可读索引
curl -s http://127.0.0.1:8100/api/mcps

# 单个服务（含工具 inputSchema）
curl -s http://127.0.0.1:8100/api/mcps/mcp-seedream

# 读取目标 MCP 自己的 markdown 说明
curl -s http://127.0.0.1:8100/api/mcps/mcp-seedream/readme
```

---

## MCP 接入

传输方式：**Streamable HTTP，无状态**。请求体为单个 JSON-RPC 2.0 对象，响应为
`application/json`。`initialize` 会返回 `Mcp-Session-Id` 响应头；本服务不校验会话，
客户端可以原样回传，也可以忽略。

### initialize

```bash
curl -s http://127.0.0.1:8100/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-06-18",
      "capabilities": {},
      "clientInfo": {"name": "demo-agent", "version": "0.1.0"}
    }
  }'
```

响应：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2025-06-18",
    "capabilities": {
      "tools": {"listChanged": false},
      "resources": {"subscribe": false, "listChanged": false}
    },
    "serverInfo": {"name": "mcp-index", "version": "1.0.0"},
    "instructions": "MCP index service. Use list_mcps/search_mcps ..."
  }
}
```

`notifications/initialized` 等通知返回 `202 Accepted`，无响应体。

### tools/list

```bash
curl -s http://127.0.0.1:8100/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

### tools/call

```bash
curl -s http://127.0.0.1:8100/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "get_mcp",
      "arguments": {"id": "mcp-seedream"}
    }
  }'
```

工具结果格式：

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [{"type": "text", "text": "{ ... JSON 字符串 ... }"}],
    "isError": false
  }
}
```

工具执行失败时返回 `isError: true`，并在 `content[0].text` 中给出原因。

### resources

| URI                | MIME            | 内容                       |
| ------------------ | --------------- | -------------------------- |
| `mcp-index://docs` | `text/markdown` | 本说明（含实时索引附录）   |

支持 `resources/list` 与 `resources/read`。

---

## MCP 工具

| 工具               | 说明                                         | 必填参数 |
| ------------------ | -------------------------------------------- | -------- |
| `list_mcps`        | 列出已索引的 MCP 服务                        | —        |
| `search_mcps`      | 按 id/名称/描述/标签/工具名全文搜索           | `query`  |
| `get_mcp`          | 获取单个服务的完整条目（含工具 schema）       | `id`     |
| `list_tools`       | 展开列出所有工具，可按 `server_id` 过滤       | —        |
| `get_mcp_markdown` | 返回本服务说明；传 `id` 时返回该服务的 readme | —        |
| `check_mcp_health` | 探测某个服务的健康检查地址                    | `id`     |

参数 schema：

```json
{
  "list_mcps": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "tag": {"type": "string", "description": "精确标签过滤"},
      "transport": {"type": "string", "description": "如 http / stdio"}
    }
  },
  "search_mcps": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "query": {"type": "string", "minLength": 1},
      "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
    },
    "required": ["query"]
  },
  "get_mcp": {
    "type": "object",
    "additionalProperties": false,
    "properties": {"id": {"type": "string"}},
    "required": ["id"]
  },
  "list_tools": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "query": {"type": "string"},
      "server_id": {"type": "string"}
    }
  },
  "get_mcp_markdown": {
    "type": "object",
    "additionalProperties": false,
    "properties": {"id": {"type": "string"}}
  },
  "check_mcp_health": {
    "type": "object",
    "additionalProperties": false,
    "properties": {"id": {"type": "string"}},
    "required": ["id"]
  }
}
```

---

## REST 接口

| 方法     | 路径                         | 说明                                      |
| -------- | ---------------------------- | ----------------------------------------- |
| `GET`    | `/`                          | 服务元信息与索引统计                      |
| `GET`    | `/health`                    | 本服务健康检查                            |
| `GET`    | `/mcp.md`                    | agent 说明（静态文档 + 实时索引附录）     |
| `GET`    | `/api/mcps`                  | 列表，支持 `q`、`tag`、`transport`         |
| `GET`    | `/api/mcps/{id}`             | 单个服务完整条目                          |
| `GET`    | `/api/mcps/{id}/tools`       | 该服务的工具列表                          |
| `GET`    | `/api/mcps/{id}/health`      | 探测上游健康检查                          |
| `GET`    | `/api/mcps/{id}/readme`      | 返回该服务的 markdown 说明                |
| `GET`    | `/api/tools`                 | 所有工具，支持 `q`、`server_id`            |
| `POST`   | `/api/mcps`                  | 新增或更新条目（按 `id` upsert）          |
| `PUT`    | `/api/mcps/{id}`             | 替换条目，`id` 必须与路径一致             |
| `DELETE` | `/api/mcps/{id}`             | 删除条目                                  |
| `POST`   | `/api/reload`                | 从 `registry.json` 重新加载注册表         |
| `POST`   | `/mcp`                       | MCP JSON-RPC 端点                         |

注册表写操作默认开放；设置 `INDEX_WRITE_TOKEN` 后，需要请求头
`Authorization: Bearer <token>` 或 `X-Index-Token: <token>`。

### 条目结构

```json
{
  "id": "mcp-seedream",
  "name": "mcp-seedream",
  "title": "Seedream Image Generation",
  "description": "把文本提示词转成图片……",
  "version": "1.0.0",
  "transport": "http",
  "protocol": "HTTP + JSON (FastAPI)",
  "base_url": "http://127.0.0.1:8000",
  "mcp_endpoint": null,
  "health_endpoint": "http://127.0.0.1:8000/health",
  "docs_url": "http://127.0.0.1:8000/docs",
  "openapi_url": "http://127.0.0.1:8000/openapi.json",
  "readme": "mcp_seedream/MCP_SERVER.md",
  "repo": "mcp_seedream/mcp_seedream_server.py",
  "tags": ["image", "image-generation", "seedream", "volcengine", "doubao"],
  "tools": [
    {
      "name": "generate_image",
      "title": "Generate Image",
      "description": "根据提示词生成图片。",
      "inputSchema": {"type": "object", "properties": {}, "required": []},
      "endpoint": {
        "method": "POST",
        "url": "http://127.0.0.1:8000/generate",
        "contentType": "application/json"
      }
    }
  ],
  "notes": "需要目标服务配置 ARK_API_KEY。",
  "added_at": "2026-10-31T00:00:00+00:00",
  "updated_at": "2026-10-31T00:00:00+00:00"
}
```

### 注册新 MCP

```bash
curl -s http://127.0.0.1:8100/api/mcps \
  -H "Content-Type: application/json" \
  -d '{
    "id": "my-mcp",
    "name": "my-mcp",
    "title": "My MCP",
    "description": "示例条目",
    "version": "0.1.0",
    "transport": "streamable-http",
    "protocol": "MCP JSON-RPC 2.0",
    "base_url": "http://127.0.0.1:9000",
    "mcp_endpoint": "http://127.0.0.1:9000/mcp",
    "health_endpoint": "http://127.0.0.1:9000/health",
    "readme": "my_mcp/MCP_SERVER.md",
    "tags": ["example"],
    "tools": [
      {
        "name": "ping",
        "description": "示例工具",
        "inputSchema": {
          "type": "object",
          "additionalProperties": false,
          "properties": {}
        }
      }
    ]
  }'
```

`added_at` / `updated_at` 可省略，服务会自动写入；更新已有条目时保留原 `added_at`。

---

## 已索引服务

### `mcp-seedream` — 文生图

- **版本**：`1.0.0`
- **传输**：`http`；协议：HTTP + JSON（FastAPI）
- **Base URL**：`http://127.0.0.1:8000`
- **健康检查**：`GET http://127.0.0.1:8000/health`
- **OpenAPI**：`GET http://127.0.0.1:8000/openapi.json`
- **说明文档**：`mcp_seedream/MCP_SERVER.md`（也可 `GET /api/mcps/mcp-seedream/readme`）
- **标签**：`image`、`image-generation`、`seedream`、`volcengine`、`doubao`
- **上游**：Volcengine Ark `images.generate`（Doubao Seedream）

工具 `generate_image`：

```json
{
  "name": "generate_image",
  "title": "Generate Image",
  "description": "Generate an image from a text prompt with Doubao Seedream. Image URLs are returned by default; set return_base64=true to embed image bytes in the JSON response.",
  "inputSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "prompt": {
        "type": "string",
        "minLength": 1,
        "maxLength": 4000,
        "description": "Text description of the image to generate."
      },
      "size": {
        "type": "string",
        "default": "2K",
        "description": "Output size: \"1K\", \"2K\", \"4K\" or \"WIDTHxHEIGHT\"."
      },
      "watermark": {
        "type": "boolean",
        "default": false,
        "description": "Let the provider stamp a watermark."
      },
      "return_base64": {
        "type": "boolean",
        "default": false,
        "description": "Download the image server-side and include base64 data."
      },
      "model": {
        "type": "string",
        "description": "Optional model override."
      }
    },
    "required": ["prompt"]
  },
  "endpoint": {
    "method": "POST",
    "url": "http://127.0.0.1:8000/generate",
    "contentType": "application/json"
  }
}
```

调用示例：

```bash
curl -s http://127.0.0.1:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt":"a red panda astronaut floating in space, cinematic lighting","size":"2K"}'
```

从响应 `images[0].url` 取图片地址；请求 `return_base64=true` 时改读
`images[0].b64_json` 与 `images[0].mime_type`。图片 URL 可能过期，需要长期保存时请及时落盘
或使用 base64 模式。

> 注意：`mcp-seedream` 目前暴露的是普通 HTTP 端点而非 JSON-RPC。Agent 应先通过本索引的
> `get_mcp` 拿到 `endpoint.url` 与 `inputSchema`，再直接 POST 调用。
> 它所在的 `mcp_seedream/` 目录需要单独启动，并且要配置 `ARK_API_KEY`。

---

## 错误码

### REST

| HTTP  | 场景                                         |
| ----- | -------------------------------------------- |
| `401` | 已配置 `INDEX_WRITE_TOKEN` 但写入令牌缺失/错误 |
| `404` | 条目或 readme 不存在                         |
| `422` | 请求体字段校验失败（未知字段、id 不匹配等）    |
| `502` | 探测上游健康检查时出现异常（以结果字段表达）  |

### MCP JSON-RPC

| 代码     | 场景                       |
| -------- | -------------------------- |
| `-32700` | 请求体不是合法 JSON        |
| `-32600` | 不是 JSON-RPC 对象         |
| `-32601` | 未知方法                   |
| `-32602` | 参数不合法                 |

工具自身的业务错误通过 `result.isError = true` 返回，而不是 JSON-RPC error。

---

## 部署与配置

```bash
cd mcp_index
pip install -r requirements.txt
cp .env.example .env                # 可选：调整端口、注册表路径或写入令牌
python mcp_index_server.py          # 监听 127.0.0.1:8100
# 或
uvicorn mcp_index_server:app --host 0.0.0.0 --port 8100
```

首次启动时如果没有 `registry.json`，服务会自动写入内置种子（包含 `mcp-seedream`）。

环境变量：

| 变量                   | 默认值                        | 说明                                             |
| ---------------------- | ----------------------------- | ------------------------------------------------ |
| `HOST`                 | `127.0.0.1`                   | `python mcp_index_server.py` 的监听地址          |
| `PORT`                 | `8100`                        | 监听端口                                         |
| `INDEX_REGISTRY`       | `mcp_index/registry.json`     | 注册表文件路径（相对路径基于脚本目录）           |
| `INDEX_PROJECT_ROOT`   | `mcp_index/` 的上一级         | 解析 `readme` 相对路径的根目录                   |
| `INDEX_WRITE_TOKEN`    | （空）                        | 设置后写接口需要 Bearer token                    |
| `INDEX_HEALTH_TIMEOUT` | `3.0`                         | 健康检查超时（秒）                               |
| `LOG_LEVEL`            | `INFO`                        | 日志级别                                         |

`.env` 会从脚本所在目录（`mcp_index/.env`）读取，模板见 `mcp_index/.env.example`；普通环境变量优先级更高。

---

## 运行说明

- **无状态**：MCP 端点不保存会话；多副本部署时只需共享/同步 `registry.json`
  （或改为外部数据库）。
- **持久化**：写接口在内存更新后原子替换 `registry.json`（先写 `.tmp` 再 `os.replace`）。
- **健康检查**：`GET /api/mcps/{id}/health` 只探测条目里的 `health_endpoint`，
  默认 3 秒超时，不会自动剔除条目。`mcp-seedream` 未启动时会返回 `unreachable`。
- **readme 安全**：`readme` 只允许解析到 `INDEX_PROJECT_ROOT` 内部；越界或缺失返回 404。
  `http(s)` 的 readme 会以 302 重定向方式返回。
- **不代理业务调用**：本服务只做索引，不转发 `/generate` 之类的业务请求，避免额外延迟与
  密钥集中风险。
- **安全**：默认无鉴权，建议只监听内网/本机；对外暴露时请设置 `INDEX_WRITE_TOKEN`
  并在网关层收紧 CORS 与访问控制。
- **实时附录**：`GET /mcp.md` 会在静态文档末尾追加当前注册表的实时表格，
  因此文档内容始终与索引一致。
