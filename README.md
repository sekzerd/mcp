# mcp

一组可独立运行的 MCP（Model Context Protocol）服务。

## 目录结构

| 目录 | 说明 |
| --- | --- |
| `mcp_index/` | MCP 索引 / 注册中心，支持 REST 与 MCP JSON-RPC，并向 agent 提供 markdown 说明 |
| `mcp_seedream/` | 基于火山方舟 Doubao Seedream 的文生图 HTTP 服务 |
| `mcp_doubao_voice/` | 基于火山方舟豆包语音的音频生成 HTTP 服务 |

## MCP Index

索引服务默认监听 `http://127.0.0.1:8100`，当前已收录 `mcp-seedream`、`mcp-doubao-voice`。

```bash
cd mcp_index
pip install -r requirements.txt
cp .env.example .env   # 可选：调整端口、注册表路径或写入令牌
python mcp_index_server.py
```

常用入口：

- Agent 说明：`GET /mcp.md`
- Swagger：`GET /docs`
- 索引列表：`GET /api/mcps`
- MCP JSON-RPC：`POST /mcp`

注册表保存在 `mcp_index/registry.json`，首次启动会自动写入内置种子。`.env.example` 列出了可选的环境变量（端口、注册表路径、写入令牌等）。

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

## Doubao Voice

音频生成服务默认监听 `http://127.0.0.1:8200`，需要先配置 `DOUBAO_VOICE_API_KEY`。

```bash
cd mcp_doubao_voice
pip install -r requirements.txt
cp .env.example .env   # 填入 DOUBAO_VOICE_API_KEY
python mcp_doubao_voice_server.py
```

调用示例：

```bash
curl -s http://127.0.0.1:8200/generate \
  -H "Content-Type: application/json" \
  -d '{"text_prompt":"你好，欢迎使用豆包语音。","audio_config":{"format":"mp3"}}'
```

从响应 `url` 取音频地址；需要内联数据时设置 `return_base64=true` 并读取 `audio`。

详细接口与错误码见各目录下的 `MCP_SERVER.md`。
