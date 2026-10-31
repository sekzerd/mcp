# Doubao Voice MCP Server

- **名称**：`mcp-doubao-voice`
- **版本**：`1.0.0`
- **协议**：HTTP + JSON（FastAPI），工具名 `generate_audio`
- **上游**：Volcengine Doubao Voice `POST /api/v3/tts/create`
- **默认地址**：`http://127.0.0.1:8200`
- **代码目录**：`mcp_doubao_voice/`
- **OpenAPI 文档**：`GET /docs`（Swagger UI）或 `GET /openapi.json`

把 `text_prompt` 通过 POST 交给本服务，服务转发到豆包语音音频生成接口，再以 JSON
返回音频地址、时长和可选字幕。上游始终返回 base64 音频；本服务默认只返回临时
URL，设置 `return_base64=true` 时额外返回 base64 数据。

---

## Agent 快速调用

1. 发送 `POST /generate`，`Content-Type: application/json`
2. 请求体至少包含 `text_prompt`
3. 从 `url` 读取音频地址；若请求了 `return_base64`，则从 `audio` 读取 base64

```bash
curl -s http://127.0.0.1:8200/generate \
  -H "Content-Type: application/json" \
  -d '{
    "text_prompt": "清晨的森林里，鸟鸣声由远及近，配上一段舒缓的钢琴音乐。",
    "audio_config": {"format": "mp3", "sample_rate": 44100}
  }'
```

---

## 工具定义（MCP / Agent 注册格式）

```json
{
  "name": "generate_audio",
  "title": "Generate Audio",
  "description": "Generate audio from a text prompt with Volcengine Doubao Voice. Supports pure text, audio references (speaker/base64/URL) and one image reference. Returns a temporary URL by default; set return_base64=true to embed the audio bytes.",
  "inputSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "text_prompt": {
        "type": "string",
        "minLength": 1,
        "maxLength": 3000,
        "description": "Prompt or text to synthesize. Use @音频N to reference the N-th audio in references, starting at 1."
      },
      "model": {
        "type": "string",
        "default": "seed-audio-1.0",
        "description": "Optional model override."
      },
      "references": {
        "type": "array",
        "maxItems": 3,
        "description": "At most 3 audio references or 1 image reference. Audio and image references cannot be mixed. Each item must set exactly one source.",
        "items": {
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "speaker": {
              "type": "string",
              "description": "Voice ID from Doubao TTS 2.0 or a cloned voice."
            },
            "audio_data": {
              "type": "string",
              "description": "Base64 reference audio (wav/mp3/pcm/ogg_opus, <=10MB)."
            },
            "audio_url": {
              "type": "string",
              "description": "URL of a reference audio file."
            },
            "image_data": {
              "type": "string",
              "description": "Base64 reference image (jpeg/png/webp, <=10MB)."
            },
            "image_url": {
              "type": "string",
              "description": "URL of a reference image."
            }
          }
        }
      },
      "audio_config": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "format": {
            "type": "string",
            "enum": ["wav", "mp3", "pcm", "ogg_opus"],
            "default": "wav",
            "description": "Output audio format."
          },
          "sample_rate": {
            "type": "integer",
            "description": "Output sample rate in Hz. Allowed values depend on format; omitted means upstream default."
          },
          "speech_rate": {
            "type": "integer",
            "minimum": -50,
            "maximum": 100,
            "default": 0,
            "description": "Speaking rate; -50 is 0.5x, 100 is 2.0x."
          },
          "loudness_rate": {
            "type": "integer",
            "minimum": -50,
            "maximum": 100,
            "default": 0,
            "description": "Loudness; -50 is 0.5x, 100 is 2.0x."
          },
          "pitch_rate": {
            "type": "integer",
            "minimum": -12,
            "maximum": 12,
            "default": 0,
            "description": "Pitch shift in semitones."
          },
          "enable_subtitle": {
            "type": "boolean",
            "default": false,
            "description": "Return sentence and word timestamps."
          }
        }
      },
      "watermark": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "aigc_watermark": {
            "type": "boolean",
            "default": false,
            "description": "Add an audible AIGC rhythm marker at the end."
          }
        }
      },
      "aigc_metadata": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "enable": {"type": "boolean", "default": false},
          "content_producer": {"type": "string"},
          "produce_id": {"type": "string"},
          "content_propagator": {"type": "string"},
          "propagate_id": {"type": "string"}
        }
      },
      "return_base64": {
        "type": "boolean",
        "default": false,
        "description": "Include the base64 audio in the JSON response."
      }
    },
    "required": ["text_prompt"]
  },
  "endpoint": {
    "method": "POST",
    "url": "http://127.0.0.1:8200/generate",
    "contentType": "application/json"
  }
}
```

---

## 接口

### `POST /generate`

请求字段：

| 字段            | 类型    | 必填 | 默认值            | 说明                                                    |
| --------------- | ------- | ---- | ----------------- | ------------------------------------------------------- |
| `text_prompt`   | string  | 是   | —                 | 提示词或待合成文本，1 ~ 3000 字符                       |
| `model`         | string  | 否   | `seed-audio-1.0`  | 模型覆盖                                                |
| `references`    | array   | 否   | `[]`              | 最多 3 条音频参考或 1 张图片参考；两者不能混用          |
| `audio_config`  | object  | 否   | 见下表            | 输出音频配置                                            |
| `watermark`     | object  | 否   | 不加水印          | 显式水印配置                                            |
| `aigc_metadata` | object  | 否   | 不加水印          | 隐式水印元数据                                          |
| `return_base64` | boolean | 否   | `false`           | 是否在 JSON 中返回 base64 音频                          |

`audio_config` 字段：

| 字段              | 类型    | 默认值 | 说明                                                         |
| ----------------- | ------- | ------ | ------------------------------------------------------------ |
| `format`          | string  | `wav`  | `wav` / `mp3` / `pcm` / `ogg_opus`                           |
| `sample_rate`     | integer | 上游默认 | wav/pcm 默认 40000；mp3 默认 44100；ogg_opus 仅 48000       |
| `speech_rate`     | integer | `0`    | 语速，范围 `[-50, 100]`                                      |
| `loudness_rate`   | integer | `0`    | 音量，范围 `[-50, 100]`                                      |
| `pitch_rate`      | integer | `0`    | 音调，范围 `[-12, 12]`                                       |
| `enable_subtitle` | boolean | `false` | 开启后返回句级和词级时间戳                                  |

支持的采样率：

| 格式       | 可用采样率                                             |
| ---------- | ------------------------------------------------------ |
| `wav`      | 8000 / 16000 / 24000 / 32000 / 40000 / 44100 / 48000   |
| `pcm`      | 8000 / 16000 / 24000 / 32000 / 40000 / 44100 / 48000   |
| `mp3`      | 8000 / 16000 / 24000 / 32000 / 44100 / 48000           |
| `ogg_opus` | 48000                                                  |

参考资源规则：

- 纯文本生成：不传 `references`。
- 音频参考：`speaker`、`audio_data`、`audio_url` 三选一；最多 3 条。
- 图片参考：`image_data`、`image_url` 二选一；最多 1 张。
- 图片参考不能与任何音频参考同时使用。
- `references` 的上传顺序与 `text_prompt` 中的 `@音频N` 严格对应，从 `@音频1` 开始。
- 单条参考音频建议不超过 30 秒、10 MB；参考图片不超过 10 MB。

最小请求：

```json
{ "text_prompt": "你好，欢迎使用豆包语音。" }
```

完整请求：

```json
{
  "model": "seed-audio-1.0",
  "text_prompt": "请用 @音频1 的音色说：今天天气不错，适合出去走走。",
  "references": [
    { "audio_url": "https://example.com/reference.mp3" }
  ],
  "audio_config": {
    "format": "mp3",
    "sample_rate": 44100,
    "speech_rate": 0,
    "loudness_rate": 0,
    "pitch_rate": 0,
    "enable_subtitle": true
  },
  "watermark": { "aigc_watermark": false },
  "return_base64": false
}
```

响应（`response_model_exclude_none=true`，未使用的字段会被省略）：

```json
{
  "request_id": "4f1c2f0b8f2e4d7c9a1b3c5d7e9f0a1b",
  "model": "seed-audio-1.0",
  "created": 1780000000,
  "text_prompt": "你好，欢迎使用豆包语音。",
  "elapsed_ms": 8421,
  "duration": 12.3,
  "original_duration": 12.3,
  "url": "https://....",
  "mime_type": "audio/mpeg",
  "logid": "2026070219553428A01BE5CBAB2D831E92"
}
```

`return_base64=true` 时会额外返回：

```json
{
  "audio": "SUQzBAAAAAAAI1RTU0UAAA..."
}
```

开启 `enable_subtitle` 时会额外返回：

```json
{
  "subtitle": {
    "text": "你好，欢迎使用豆包语音。",
    "sentences": [
      {
        "start_time": 0,
        "end_time": 2400,
        "text": "你好，欢迎使用豆包语音。",
        "words": [
          { "start_time": 0, "end_time": 480, "text": "你好" }
        ]
      }
    ]
  }
}
```

响应字段：

| 字段                  | 类型    | 说明                                              |
| --------------------- | ------- | ------------------------------------------------- |
| `request_id`          | string  | 本次请求 ID，同时作为上游 `X-Api-Request-Id`      |
| `model`               | string  | 实际使用的模型                                    |
| `created`             | integer | 服务端生成时间戳                                  |
| `text_prompt`         | string  | 回显请求中的提示词                                |
| `elapsed_ms`          | integer | 服务端总耗时（毫秒）                              |
| `duration`            | float   | 处理后音频时长（秒）                              |
| `original_duration`   | float   | 模型原始音频时长（秒），计费依据                  |
| `url`                 | string  | 带过期时间的音频地址，有效期约 2 小时             |
| `audio`               | string  | base64 音频，仅 `return_base64=true` 时返回       |
| `mime_type`           | string  | 根据 `audio_config.format` 推断的 MIME 类型       |
| `subtitle`            | object  | 字幕与时间戳，仅 `enable_subtitle=true` 时返回    |
| `logid`               | string  | 上游返回的 `X-Tt-Logid`，便于排查问题             |

### 辅助接口

| 方法  | 路径            | 说明                                   |
| ----- | --------------- | -------------------------------------- |
| `GET` | `/`             | 服务元信息                             |
| `GET` | `/health`       | 健康检查，返回鉴权是否已配置           |
| `GET` | `/docs`         | Swagger UI                             |
| `GET` | `/openapi.json` | OpenAPI 3 描述                         |

---

## 调用示例

### curl（纯文本，默认返回 URL）

```bash
curl -s http://127.0.0.1:8200/generate \
  -H "Content-Type: application/json" \
  -d '{
    "text_prompt": "一段紧张悬疑的配乐，马林巴为主奏乐器，加入合成器和电贝斯。",
    "audio_config": {"format": "mp3", "sample_rate": 44100}
  }'
```

### curl（使用音色 ID）

```bash
curl -s http://127.0.0.1:8200/generate \
  -H "Content-Type: application/json" \
  -d '{
    "text_prompt": "你好，我是豆包语音。",
    "references": [{"speaker": "<speaker_id>"}],
    "audio_config": {"format": "mp3"}
  }'
```

### curl（使用参考音频 URL）

```bash
curl -s http://127.0.0.1:8200/generate \
  -H "Content-Type: application/json" \
  -d '{
    "text_prompt": "请用 @音频1 的音色说：欢迎来到直播间。",
    "references": [{"audio_url": "https://example.com/reference.mp3"}],
    "audio_config": {"format": "wav"}
  }'
```

### curl（内联 base64 并保存）

```bash
curl -s http://127.0.0.1:8200/generate \
  -H "Content-Type: application/json" \
  -d '{"text_prompt":"你好，欢迎使用豆包语音。","return_base64":true,"audio_config":{"format":"mp3"}}' \
  | python -c "import json,sys,base64; d=json.load(sys.stdin); open('out.mp3','wb').write(base64.b64decode(d['audio']))"
```

### Python

```python
import httpx

response = httpx.post(
    "http://127.0.0.1:8200/generate",
    json={
        "text_prompt": "夜晚的城市街道，雨声，远处传来轻柔的爵士乐。",
        "audio_config": {"format": "mp3", "sample_rate": 44100},
    },
    timeout=300,
)
response.raise_for_status()
data = response.json()
print(data["url"])
print(f"{data['duration']} s, {data['elapsed_ms']} ms")
```

### TypeScript / JavaScript

```ts
const response = await fetch("http://127.0.0.1:8200/generate", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    text_prompt: "欢迎收听今天的节目。",
    audio_config: { format: "mp3", sample_rate: 44100 },
  }),
});

if (!response.ok) throw new Error(await response.text());
const data = await response.json();
console.log(data.url);
```

---

## 错误码

| HTTP  | 场景                                                   | 返回                                      |
| ----- | ------------------------------------------------------ | ----------------------------------------- |
| `422` | 请求字段缺失、类型错误、引用资源组合非法               | FastAPI 校验详情                          |
| `500` | 服务端未配置鉴权信息                                   | `{"detail": "Doubao Voice credentials ..."}` |
| `502` | 上游报错、网络不可达、返回非 JSON 或未返回音频/地址    | `{"detail": "Doubao Voice API error ..."}` |
| `504` | 上游生成超时（默认 300s）                              | `{"detail": "Audio generation timed out..."}` |

---

## 部署与配置

```bash
cd mcp_doubao_voice
pip install -r requirements.txt
cp .env.example .env             # 填入 DOUBAO_VOICE_API_KEY
python mcp_doubao_voice_server.py  # 监听 127.0.0.1:8200
# 或
uvicorn mcp_doubao_voice_server:app --host 0.0.0.0 --port 8200
```

`.env` 默认从服务脚本所在目录（`mcp_doubao_voice/.env`）读取，因此从仓库根目录执行
`python mcp_doubao_voice/mcp_doubao_voice_server.py` 也能正常加载。

环境变量：

| 变量                     | 默认值                                     | 说明                                             |
| ------------------------ | ------------------------------------------ | ------------------------------------------------ |
| `DOUBAO_VOICE_API_KEY`   | （推荐）                                   | 新版控制台 API Key，作为 `X-Api-Key` 发送        |
| `DOUBAO_VOICE_APP_ID`    | （旧版）                                   | 旧版控制台 App ID，与 Access Key 配套使用        |
| `DOUBAO_VOICE_ACCESS_KEY`| （旧版）                                   | 旧版控制台 Access Key                            |
| `DOUBAO_VOICE_BASE_URL`  | `https://openspeech.bytedance.com`         | API 地址                                         |
| `DOUBAO_VOICE_MODEL`     | `seed-audio-1.0`                           | 默认模型                                         |
| `DOUBAO_VOICE_TIMEOUT`   | `300`                                      | 上游超时（秒）                                   |
| `HOST`                   | `127.0.0.1`                                | `python mcp_doubao_voice_server.py` 的监听地址   |
| `PORT`                   | `8200`                                     | 监听端口                                         |
| `LOG_LEVEL`              | `INFO`                                     | 日志级别                                         |

鉴权优先级：配置了 `DOUBAO_VOICE_API_KEY` 时使用新版单头鉴权；否则使用
`DOUBAO_VOICE_APP_ID` + `DOUBAO_VOICE_ACCESS_KEY` 双头鉴权。两套都未配置时
`/generate` 返回 `500`。

---

## 运行说明

- **性能**：上游同步返回音频，单次最长 120 秒；客户端超时建议 ≥ 300s。
- **URL 有效期**：上游返回的音频 URL 约 2 小时后过期，需要长期保存时请使用
  `return_base64=true` 或及时下载。
- **响应体积**：base64 模式会显著增大 JSON，长音频建议先用 URL 模式。
- **计费**：计费以 `original_duration` 为准，调用上游会消耗额度；请遵守火山方舟的
  QPS 与配额限制。
- **参考资源**：音频参考最多 3 条，单条 ≤ 30 秒、≤ 10 MB；图片参考最多 1 张、
  ≤ 10 MB；图片和音频参考不能混用。
- **安全**：本服务自身不加鉴权，建议只监听内网 / 本机，或放在带鉴权的网关后面。
  API Key 只存在服务端环境变量中，绝不会返回给客户端。
- **CORS**：默认放开 `*`，便于本地页面/浏览器 agent 调用；生产环境请收紧。

---

## 在 MCP Index 中

本服务已注册到 `mcp-index`：

- 索引 id：`mcp-doubao-voice`
- 工具：`generate_audio`
- 索引地址：`GET http://127.0.0.1:8100/api/mcps/mcp-doubao-voice`
- 本说明：`GET http://127.0.0.1:8100/api/mcps/mcp-doubao-voice/readme`

调用前请先通过索引确认 `base_url` 与 `tools[].endpoint.url`，再直接 POST 到本服务。
