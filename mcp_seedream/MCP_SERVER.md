# Seedream MCP Server

- **名称**：`mcp-seedream`
- **版本**：`1.0.0`
- **协议**：HTTP + JSON（FastAPI），工具名 `generate_image`
- **上游**：Volcengine Ark `images.generate`（Doubao Seedream）
- **默认地址**：`http://127.0.0.1:8000`
- **代码目录**：`mcp_seedream/`
- **OpenAPI 文档**：`GET /docs`（Swagger UI）或 `GET /openapi.json`

把 `prompt` 通过 POST 传给服务，服务转发到 `client.images.generate(...)`，再以 JSON
返回图片地址（可选内联 base64）。上游固定使用 `response_format="url"` 以降低响应体积、
缩短延迟；需要内联图片数据时在请求里设置 `return_base64=true`。

---

## Agent 快速调用

1. 发送 `POST /generate`，`Content-Type: application/json`
2. 请求体至少包含 `prompt`
3. 从 `images[0].url` 读取图片地址；若请求了 `return_base64`，则读取
   `images[0].b64_json` 与 `images[0].mime_type`

```bash
curl -s http://127.0.0.1:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt":"a red panda astronaut floating in space, cinematic lighting","size":"2K"}'
```

---

## 工具定义（MCP / Agent 注册格式）

```json
{
  "name": "generate_image",
  "title": "Generate Image",
  "description": "Generate an image from a text prompt with Doubao Seedream. Returns image URLs by default; set return_base64=true to embed the image bytes in the JSON response.",
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
        "description": "Let the provider stamp a watermark on the image."
      },
      "return_base64": {
        "type": "boolean",
        "default": false,
        "description": "Download the image server-side and include base64 data in the response."
      },
      "model": {
        "type": "string",
        "description": "Optional model override, defaults to ARK_IMAGE_MODEL."
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

---

## 接口

### `POST /generate`

请求字段：

| 字段            | 类型    | 必填 | 默认值   | 说明                                                |
| --------------- | ------- | ---- | -------- | --------------------------------------------------- |
| `prompt`        | string  | 是   | —        | 提示词，1 ~ 4000 字符                               |
| `size`          | string  | 否   | `"2K"`   | `"1K"` / `"2K"` / `"4K"`，或 `"2048x2048"` 这类宽高 |
| `watermark`     | boolean | 否   | `false`  | 是否由上游添加水印                                  |
| `return_base64` | boolean | 否   | `false`  | 是否把图片转成 base64 放进 JSON                     |
| `model`         | string  | 否   | 环境变量 | 覆盖默认模型                                        |

最小请求：

```json
{ "prompt": "星际穿越，黑洞，复古列车，电影大片，强对比光影" }
```

完整请求：

```json
{
  "prompt": "星际穿越，黑洞，复古列车，电影大片，强对比光影",
  "size": "2K",
  "watermark": false,
  "return_base64": true,
  "model": "doubao-seedream-5-0-pro-260628"
}
```

响应（`response_model_exclude_none=true`，未使用的字段会被省略）：

```json
{
  "request_id": "4f1c2f0b8f2e4d7c9a1b3c5d7e9f0a1b",
  "model": "doubao-seedream-5-0-pro-260628",
  "created": 1780000000,
  "prompt": "星际穿越，黑洞，复古列车，电影大片，强对比光影",
  "elapsed_ms": 8421,
  "images": [
    {
      "url": "https://ark-content-generation-cn-beijing.tos-cn-beijing.volces.com/....jpeg",
      "mime_type": "image/jpeg",
      "b64_json": "iVBORw0KGgoAAAANSUhEUg..."
    }
  ]
}
```

响应字段：

| 字段                      | 类型    | 说明                                                |
| ------------------------- | ------- | --------------------------------------------------- |
| `request_id`              | string  | 本次请求 ID，便于排查日志                           |
| `model`                   | string  | 实际使用的模型                                      |
| `created`                 | integer | 上游返回的创建时间戳                                |
| `prompt`                  | string  | 回显请求中的提示词                                  |
| `elapsed_ms`              | integer | 服务端总耗时（毫秒）                                |
| `images`                  | array   | 图片列表；当前每次返回 1 张                         |
| `images[].url`            | string  | 图片地址（默认模式）                                |
| `images[].b64_json`       | string  | base64 编码的图片（`return_base64=true` 时）        |
| `images[].mime_type`      | string  | 图片 MIME 类型，例如 `image/jpeg`                   |
| `images[].revised_prompt` | string  | 上游改写后的提示词（若有）                          |
| `images[].error`          | string  | 下载 base64 失败时的错误说明（此时 `url` 仍然可用） |

### 辅助接口

| 方法  | 路径            | 说明                                   |
| ----- | --------------- | -------------------------------------- |
| `GET` | `/`             | 服务元信息                             |
| `GET` | `/health`       | 健康检查，返回是否已配置 `ARK_API_KEY` |
| `GET` | `/docs`         | Swagger UI                             |
| `GET` | `/openapi.json` | OpenAPI 3 描述                         |

---

## 调用示例

### curl（默认返回 URL）

```bash
curl -s http://127.0.0.1:8000/generate \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "星际穿越，黑洞，末日既视感，电影大片，超现实主义，深蓝色调",
    "size": "2K",
    "watermark": false
  }'
```

### curl（内联 base64，适合需要持久化图片的场景）

```bash
curl -s http://127.0.0.1:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt":"一只戴宇航头盔的柴犬","return_base64":true}' \
  | python -c "import json,sys,base64; d=json.load(sys.stdin); open('out.jpg','wb').write(base64.b64decode(d['images'][0]['b64_json']))"
```

### Python

```python
import httpx

response = httpx.post(
    "http://127.0.0.1:8000/generate",
    json={
        "prompt": "a cozy cabin in a snowy forest, warm window light",
        "size": "2K",
        "return_base64": False,
    },
    timeout=180,
)
response.raise_for_status()
data = response.json()
print(data["images"][0]["url"])
print(f"{data['elapsed_ms']} ms")
```

### TypeScript / JavaScript

```ts
const response = await fetch("http://127.0.0.1:8000/generate", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    prompt: "neon city at night, rain, cinematic",
    size: "2K",
  }),
});

if (!response.ok) throw new Error(await response.text());
const data = await response.json();
console.log(data.images[0].url);
```

---

## 错误码

| HTTP  | 场景                                                 | 返回                                          |
| ----- | ---------------------------------------------------- | --------------------------------------------- |
| `422` | 请求字段缺失或类型错误（如 `prompt` 为空、未知字段） | FastAPI 校验详情                              |
| `500` | 服务端未配置 `ARK_API_KEY`                           | `{"detail": "..."}`                           |
| `502` | 上游 API 报错 / 网络不可达 / 未返回图片              | `{"detail": "Image API error ..."}`           |
| `504` | 上游生成超时（默认 180s）                            | `{"detail": "Image generation timed out..."}` |

---

## 部署与配置

```bash
cd mcp_seedream
pip install -r requirements.txt
cp .env.example .env        # 填入 ARK_API_KEY
python mcp_seedream_server.py  # 监听 127.0.0.1:8000
# 或
uvicorn mcp_seedream_server:app --host 0.0.0.0 --port 8000
```

`.env` 默认从服务脚本所在目录（`mcp_seedream/.env`）读取，因此从仓库根目录执行
`python mcp_seedream/mcp_seedream_server.py` 也能正常加载。

环境变量：

| 变量              | 默认值                                     | 说明                                       |
| ----------------- | ------------------------------------------ | ------------------------------------------ |
| `ARK_API_KEY`     | （必填）                                   | 火山方舟 API Key                           |
| `ARK_BASE_URL`    | `https://ark.cn-beijing.volces.com/api/v3` | API 地址                                   |
| `ARK_IMAGE_MODEL` | `doubao-seedream-5-0-pro-260628`           | 默认模型                                   |
| `ARK_TIMEOUT`     | `180`                                      | 上游超时（秒）                             |
| `HOST`            | `127.0.0.1`                                | `python mcp_seedream_server.py` 的监听地址 |
| `PORT`            | `8000`                                     | 监听端口                                   |
| `LOG_LEVEL`       | `INFO`                                     | 日志级别                                   |

---

## 运行说明

- **性能**：始终使用 `response_format="url"`，服务端不中转图片字节；只有
  `return_base64=true` 时才额外下载图片一次。
- **URL 有效期**：上游返回的图片 URL 可能过期，需要长期保存时请使用
  `return_base64=true` 或及时落盘。
- **耗时**：2K 图片通常需要数秒到数十秒，客户端超时建议 ≥ 120s。
- **计费/限流**：调用上游会消耗额度，请遵守火山方舟的 QPS 与配额限制；
  服务端只做了单次重试，不做请求排队。
- **安全**：服务不加鉴权，建议只监听内网 / 本机，或放在带鉴权的网关后面。
  `ARK_API_KEY` 只存在服务端环境变量中，绝不返回给客户端。
- **CORS**：默认放开 `*`，便于本地页面/浏览器 agent 调用；生产环境请收紧。
