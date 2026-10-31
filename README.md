# mcp

一组可独立运行的 MCP（Model Context Protocol）服务。

## 目录结构

| 目录 | 说明 |
| --- | --- |
| `mcp_index/` | MCP 索引 / 注册中心，支持 REST 与 MCP JSON-RPC，并向 agent 提供 markdown 说明 |
| `mcp_seedream/` | 基于火山方舟 Doubao Seedream 的文生图 HTTP 服务 |

## MCP Index

索引服务默认监听 `http://127.0.0.1:8100`，当前已收录 `mcp-seedream`。

```bash
cd mcp_index
pip install -r requirements.txt
python mcp_index_server.py
```

常用入口：

- Agent 说明：`GET /mcp.md`
- Swagger：`GET /docs`
- 索引列表：`GET /api/mcps`
- MCP JSON-RPC：`POST /mcp`

注册表保存在 `mcp_index/registry.json`，首次启动会自动写入内置种子。

## Seedream

文生图服务默认监听 `http://127.0.0.1:8000`，需要先配置 `ARK_API_KEY`。

```bash
cd mcp_seedream
pip install -r requirements.txt
cp .env.example .env   # 填入 ARK_API_KEY
python mcp_seedream_server.py
```

调用示例：

```bash
curl -s http://127.0.0.1:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt":"a red panda astronaut floating in space","size":"2K"}'
```

详细接口与错误码见各目录下的 `MCP_SERVER.md`。
