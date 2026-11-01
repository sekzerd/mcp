# mcp

一组可独立运行的 MCP（Model Context Protocol）服务。

## 目录结构

| 目录                | 说明                                                                          |
| ------------------- | ----------------------------------------------------------------------------- |
| `mcp_index/`        | MCP 索引 / 注册中心，支持 REST 与 MCP JSON-RPC，并向 agent 提供 markdown 说明 |
| `mcp_seedream/`     | 基于火山方舟 Doubao Seedream 的文生图 HTTP 服务                               |
| `mcp_doubao_voice/` | 基于火山方舟豆包语音的音频生成 HTTP 服务                                      |

根目录的 `run.py` 可一键启动 MCP Index。

## MCP Index

索引服务默认监听 `http://127.0.0.1:8100`，当前已收录 `mcp-seedream`、`mcp-doubao-voice`。

推荐在项目根目录一键启动：

```bash
python run.py
```

首次使用先安装索引服务依赖：

```bash
python -m pip install -r mcp_index/requirements.txt
```

也可以进入 `mcp_index/` 手动启动：

```bash
cd mcp_index
cp .env.example .env   # 可选：调整端口、注册表路径或写入令牌
python mcp_index_server.py
```

`run.py` 支持 `--host`、`--port`、`--reload`、`--log-level`，并会读取
`mcp_index/.env` 中的 `HOST`、`PORT`、`LOG_LEVEL`（命令行参数优先）。默认地址为
`http://127.0.0.1:8100`；端口被占用时会直接给出提示。

### 后台运行（Windows 服务）

需要后台常驻时，可用 `service.py` 把 `run.py` 注册为 Windows 服务（依赖 `pywin32`）。
`enable` / `disable` 需要管理员权限；当前终端不是管理员时会弹出 UAC，并在新的管理员
命令行窗口中自动继续执行，窗口会保留以便查看结果：

```bash
python -m pip install pywin32

python service.py enable    # 安装并启动服务，开机自动运行
python service.py restart   # 只重启服务，不重新安装
python service.py status    # 查看服务状态
python service.py disable   # 停止并卸载服务
```

服务以 LocalSystem 身份运行，内部以子进程方式守护 `run.py`：异常退出会自动重启，
输出写入 `logs/service.log`（按大小轮转）。排查问题时可执行
`python service.py console`，在前台运行同样的守护逻辑。

停服务时 `service.py` 只通过 SCM 发停止指令，不会去枚举服务注册表（枚举会在权限受限的
键上失败并报 `(5, 'RegOpenKeyEx', '拒绝访问')`）。如果服务在 `SERVICE_STOP_PENDING`
卡住，`enable` / `disable` / `restart` 会超时后强制结束服务宿主进程；在服务内部还有
一个 40 秒看门狗负责保底。修改了 `service.py` 本身后需要重新 `enable`，因为守护逻辑
是随服务宿主一起加载的。

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
