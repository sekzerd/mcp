"""FastAPI MCP index service.

The service keeps a registry of MCP servers and their tools so that agents can
discover what exists before calling anything.  It exposes three surfaces:

* ``/mcp``     - MCP Streamable HTTP endpoint (JSON-RPC 2.0, stateless) with
                 tools such as ``list_mcps`` / ``search_mcps`` / ``get_mcp``.
* ``/api/...`` - Plain REST/JSON endpoints for browsers, scripts and gateways.
* ``/mcp.md``  - Agent-facing markdown documentation (the same file that ships
                 in this directory as ``MCP_SERVER.md``).

The registry is persisted to ``registry.json`` next to this file and is seeded
with ``mcp-seedream`` on first run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field


def _load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader, so python-dotenv is not required."""
    if path is None:
        path = Path(__file__).resolve().parent / ".env"

    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        pass


_load_dotenv()


def _resolve_path(raw: str, default: Path) -> Path:
    candidate = Path(raw).expanduser() if raw else default
    if not candidate.is_absolute():
        candidate = Path(__file__).resolve().parent / candidate
    return candidate.resolve()


BASE_DIR = Path(__file__).resolve().parent
REGISTRY_PATH = _resolve_path(
    os.environ.get("INDEX_REGISTRY", ""),
    BASE_DIR / "registry.json",
)
PROJECT_ROOT = _resolve_path(
    os.environ.get("INDEX_PROJECT_ROOT", ""),
    BASE_DIR.parent,
)
DOC_PATH = BASE_DIR / "MCP_SERVER.md"

REGISTRY_VERSION = 1
DEFAULT_MCP_PROTOCOL_VERSION = "2025-06-18"
HEALTH_TIMEOUT = float(os.environ.get("INDEX_HEALTH_TIMEOUT", "3.0"))
MAX_HEALTH_BODY_CHARS = 4000

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("mcp-index")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ToolEndpoint(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    method: str = Field("POST", description="HTTP method used to call the tool.")
    url: str = Field(..., description="Absolute URL of the tool endpoint.")
    content_type: str | None = Field(
        None,
        alias="contentType",
        description="Request content type, e.g. application/json.",
    )


class McpTool(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = Field(..., description="Tool name as exposed by the MCP server.")
    title: str | None = Field(None, description="Human friendly tool title.")
    description: str = Field("", description="What the tool does.")
    input_schema: dict[str, Any] = Field(
        default_factory=dict,
        alias="inputSchema",
        description="JSON Schema for the tool arguments.",
    )
    output_schema: dict[str, Any] | None = Field(
        None,
        alias="outputSchema",
        description="Optional JSON Schema for the tool result.",
    )
    endpoint: ToolEndpoint | None = Field(
        None,
        description="HTTP endpoint for servers that do not speak MCP JSON-RPC.",
    )


class McpServerBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
        description="Stable slug used to look the server up.",
    )
    name: str = Field(..., min_length=1, max_length=128)
    title: str | None = Field(None, max_length=200)
    description: str = Field("", max_length=2000)
    version: str = Field("0.0.0", max_length=32)
    transport: str = Field(
        "http",
        max_length=32,
        description='Transport, e.g. "http", "sse", "streamable-http", "stdio".',
    )
    protocol: str = Field(
        "HTTP + JSON",
        max_length=64,
        description='Wire protocol, e.g. "HTTP + JSON (FastAPI)" or "MCP JSON-RPC 2.0".',
    )
    base_url: str | None = Field(None, max_length=500)
    mcp_endpoint: str | None = Field(
        None,
        max_length=500,
        description="JSON-RPC endpoint when the server speaks MCP.",
    )
    health_endpoint: str | None = Field(None, max_length=500)
    docs_url: str | None = Field(None, max_length=500)
    openapi_url: str | None = Field(None, max_length=500)
    readme: str | None = Field(
        None,
        max_length=500,
        description="Relative markdown path (under the project root) or absolute URL.",
    )
    repo: str | None = Field(None, max_length=500)
    tags: list[str] = Field(default_factory=list)
    tools: list[McpTool] = Field(default_factory=list)
    notes: str | None = Field(None, max_length=2000)


class McpServerEntry(McpServerBase):
    added_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class McpServerUpsert(McpServerBase):
    added_at: datetime | None = None
    updated_at: datetime | None = None


class McpServerSummary(BaseModel):
    id: str
    name: str
    title: str | None = None
    description: str = ""
    version: str
    transport: str
    protocol: str
    base_url: str | None = None
    mcp_endpoint: str | None = None
    health_endpoint: str | None = None
    docs_url: str | None = None
    tags: list[str] = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list)
    updated_at: datetime


class McpListResponse(BaseModel):
    count: int
    servers: list[McpServerSummary]


class ToolRecord(BaseModel):
    server_id: str
    server_name: str
    tool: McpTool


class ToolListResponse(BaseModel):
    count: int
    tools: list[ToolRecord]


class HealthProbeResult(BaseModel):
    id: str
    status: str
    url: str | None = None
    status_code: int | None = None
    latency_ms: int | None = None
    detail: str | None = None
    body: Any | None = None


# ---------------------------------------------------------------------------
# Seed registry
# ---------------------------------------------------------------------------


def _seed_servers() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    return [
        {
            "id": "mcp-seedream",
            "name": "mcp-seedream",
            "title": "Seedream Image Generation",
            "description": (
                "Turns a text prompt into an image through Volcengine Ark "
                "(Doubao Seedream). Returns image URLs by default and can inline "
                "base64 bytes when return_base64=true."
            ),
            "version": "1.0.0",
            "transport": "http",
            "protocol": "HTTP + JSON (FastAPI)",
            "base_url": "http://127.0.0.1:8000",
            "mcp_endpoint": None,
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
                    "description": (
                        "Generate an image from a text prompt with Doubao Seedream. "
                        "Image URLs are returned by default; set return_base64=true "
                        "to embed image bytes in the JSON response."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "prompt": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 4000,
                                "description": "Text description of the image to generate.",
                            },
                            "size": {
                                "type": "string",
                                "default": "2K",
                                "description": 'Output size: "1K", "2K", "4K" or "WIDTHxHEIGHT".',
                            },
                            "watermark": {
                                "type": "boolean",
                                "default": False,
                                "description": "Let the provider stamp a watermark.",
                            },
                            "return_base64": {
                                "type": "boolean",
                                "default": False,
                                "description": "Download the image server-side and include base64 data.",
                            },
                            "model": {
                                "type": "string",
                                "description": "Optional model override.",
                            },
                        },
                        "required": ["prompt"],
                    },
                    "endpoint": {
                        "method": "POST",
                        "url": "http://127.0.0.1:8000/generate",
                        "contentType": "application/json",
                    },
                }
            ],
            "notes": (
                "Requires ARK_API_KEY on the Seedream server. Image URLs expire; "
                "use return_base64=true (or download promptly) for persistence."
            ),
            "added_at": now,
            "updated_at": now,
        }
    ]


# ---------------------------------------------------------------------------
# Registry state
# ---------------------------------------------------------------------------

_servers: dict[str, McpServerEntry] = {}
_registry_lock = asyncio.Lock()
_registry_loaded = False
_http_client: httpx.AsyncClient | None = None


def _save_registry() -> None:
    payload = {
        "version": REGISTRY_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "servers": [
            entry.model_dump(mode="json", by_alias=True) for entry in _servers.values()
        ],
    }
    tmp_path = REGISTRY_PATH.with_suffix(REGISTRY_PATH.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, REGISTRY_PATH)


def _load_registry_locked() -> None:
    _servers.clear()

    if REGISTRY_PATH.exists():
        try:
            raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - keep serving with the seed
            logger.error(
                "cannot parse registry %s: %s; starting from the built-in seed",
                REGISTRY_PATH,
                exc,
            )
            raw = None

        if raw is not None:
            for item in raw.get("servers", []):
                entry = McpServerEntry.model_validate(item)
                _servers[entry.id] = entry
            logger.info("loaded %d MCP server(s) from %s", len(_servers), REGISTRY_PATH)
            return

        logger.warning("registry file exists but is unusable; not overwriting it")
        for item in _seed_servers():
            entry = McpServerEntry.model_validate(item)
            _servers[entry.id] = entry
        return

    for item in _seed_servers():
        entry = McpServerEntry.model_validate(item)
        _servers[entry.id] = entry
    _save_registry()
    logger.info("seeded new registry at %s", REGISTRY_PATH)


async def _ensure_loaded() -> None:
    global _registry_loaded
    if _registry_loaded:
        return

    async with _registry_lock:
        if _registry_loaded:
            return
        _load_registry_locked()
        _registry_loaded = True


async def _reload_registry() -> int:
    global _registry_loaded
    async with _registry_lock:
        _load_registry_locked()
        _registry_loaded = True
        return len(_servers)


def _summary(entry: McpServerEntry) -> McpServerSummary:
    return McpServerSummary(
        id=entry.id,
        name=entry.name,
        title=entry.title,
        description=entry.description,
        version=entry.version,
        transport=entry.transport,
        protocol=entry.protocol,
        base_url=entry.base_url,
        mcp_endpoint=entry.mcp_endpoint,
        health_endpoint=entry.health_endpoint,
        docs_url=entry.docs_url,
        tags=entry.tags,
        tool_names=[tool.name for tool in entry.tools],
        updated_at=entry.updated_at,
    )


def _matches(entry: McpServerEntry, query: str | None) -> bool:
    if not query:
        return True
    needle = query.strip().lower()
    parts = [
        entry.id,
        entry.name,
        entry.title or "",
        entry.description,
        entry.protocol,
        entry.transport,
        " ".join(entry.tags),
        " ".join(tool.name for tool in entry.tools),
        " ".join(tool.description for tool in entry.tools),
    ]
    return needle in " ".join(parts).lower()


def _find(mcp_id: str) -> McpServerEntry:
    entry = _servers.get(mcp_id)
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"MCP server '{mcp_id}' is not indexed."
        )
    return entry


# ---------------------------------------------------------------------------
# Markdown / readme helpers
# ---------------------------------------------------------------------------


def _live_index_markdown() -> str:
    lines = [
        "## 当前索引（实时生成）",
        "",
        f"共 **{len(_servers)}** 个 MCP 服务，注册表文件：`{REGISTRY_PATH}`。",
        "",
        "| id | 传输 | Base URL | 工具 | 文档 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in _servers.values():
        tools = ", ".join(f"`{tool.name}`" for tool in entry.tools) or "—"
        docs = entry.readme or entry.docs_url or "—"
        lines.append(
            f"| `{entry.id}` | {entry.transport} | {entry.base_url or '—'} | {tools} | {docs} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_markdown() -> str:
    try:
        base = DOC_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        base = "# MCP Index Server\n\nAgent documentation file is missing.\n"
    return base.rstrip() + "\n\n---\n\n" + _live_index_markdown()


def _read_readme(entry: McpServerEntry) -> str | None:
    if not entry.readme:
        return None
    if entry.readme.startswith(("http://", "https://")):
        return None

    candidate = (PROJECT_ROOT / entry.readme).resolve()
    if not candidate.is_relative_to(PROJECT_ROOT) or not candidate.is_file():
        return None
    return candidate.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Health probing
# ---------------------------------------------------------------------------


async def _probe(entry: McpServerEntry) -> HealthProbeResult:
    if not entry.health_endpoint:
        return HealthProbeResult(
            id=entry.id,
            status="unknown",
            detail="This entry does not declare a health_endpoint.",
        )

    started = time.perf_counter()
    client = _http_client
    transient = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=HEALTH_TIMEOUT, follow_redirects=True)

    try:
        response = await client.get(entry.health_endpoint, timeout=HEALTH_TIMEOUT)
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text[:MAX_HEALTH_BODY_CHARS]

        return HealthProbeResult(
            id=entry.id,
            status="ok" if response.is_success else "error",
            url=entry.health_endpoint,
            status_code=response.status_code,
            latency_ms=latency_ms,
            detail=None if response.is_success else "Upstream health check failed.",
            body=body,
        )
    except Exception as exc:  # noqa: BLE001 - surface every probe failure
        return HealthProbeResult(
            id=entry.id,
            status="unreachable",
            url=entry.health_endpoint,
            latency_ms=int((time.perf_counter() - started) * 1000),
            detail=str(exc),
        )
    finally:
        if transient:
            await client.aclose()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _http_client
    await _ensure_loaded()
    async with httpx.AsyncClient(
        timeout=HEALTH_TIMEOUT,
        follow_redirects=True,
    ) as http:
        _http_client = http
        try:
            yield
        finally:
            _http_client = None


app = FastAPI(
    title="MCP Index Server",
    version="1.0.0",
    description=(
        "An index / registry of MCP servers. Discover entries over REST or MCP "
        "JSON-RPC; agent documentation lives at GET /mcp.md."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
)


def require_write_access(request: Request) -> None:
    """Protect write endpoints when INDEX_WRITE_TOKEN is configured."""
    token = os.environ.get("INDEX_WRITE_TOKEN", "").strip()
    if not token:
        return

    supplied = ""
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    supplied = supplied or request.headers.get("x-index-token", "").strip()

    if not secrets.compare_digest(token, supplied):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid index write token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# REST surface
# ---------------------------------------------------------------------------


@app.get("/", summary="Service metadata")
async def index() -> dict:
    await _ensure_loaded()
    return {
        "name": "mcp-index",
        "version": app.version,
        "description": "MCP index / registry service.",
        "mcp_endpoint": "/mcp",
        "markdown": "/mcp.md",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "registry": str(REGISTRY_PATH),
        "indexed": {
            "count": len(_servers),
            "ids": list(_servers),
        },
        "rest": {
            "list": "GET /api/mcps",
            "detail": "GET /api/mcps/{id}",
            "tools": "GET /api/tools",
            "upsert": "POST /api/mcps",
            "delete": "DELETE /api/mcps/{id}",
            "health": "GET /api/mcps/{id}/health",
            "readme": "GET /api/mcps/{id}/readme",
            "reload": "POST /api/reload",
        },
    }


@app.get("/health", summary="Health check")
async def health() -> dict:
    await _ensure_loaded()
    return {
        "status": "ok",
        "servers": len(_servers),
        "registry_loaded": _registry_loaded,
    }


@app.get("/mcp.md", summary="Agent-facing markdown documentation")
async def markdown_docs() -> Response:
    await _ensure_loaded()
    return Response(
        content=_render_markdown(),
        media_type="text/markdown; charset=utf-8",
    )


@app.get(
    "/api/mcps",
    response_model=McpListResponse,
    summary="List indexed MCP servers",
)
async def list_mcps(
    q: str | None = Query(None, description="Free-text search across the registry."),
    tag: str | None = Query(None, description="Filter by exact tag."),
    transport: str | None = Query(None, description="Filter by transport."),
) -> McpListResponse:
    await _ensure_loaded()
    entries = [
        entry
        for entry in _servers.values()
        if _matches(entry, q)
        and (tag is None or tag in entry.tags)
        and (transport is None or entry.transport.lower() == transport.lower())
    ]
    return McpListResponse(
        count=len(entries),
        servers=[_summary(entry) for entry in entries],
    )


@app.get(
    "/api/mcps/{mcp_id}",
    response_model=McpServerEntry,
    response_model_by_alias=True,
    summary="Get one MCP server",
)
async def get_mcp(mcp_id: str) -> McpServerEntry:
    await _ensure_loaded()
    return _find(mcp_id)


@app.get(
    "/api/mcps/{mcp_id}/tools",
    response_model=ToolListResponse,
    summary="List tools exposed by an MCP server",
)
async def list_server_tools(mcp_id: str) -> ToolListResponse:
    await _ensure_loaded()
    entry = _find(mcp_id)
    return ToolListResponse(
        count=len(entry.tools),
        tools=[
            ToolRecord(server_id=entry.id, server_name=entry.name, tool=tool)
            for tool in entry.tools
        ],
    )


@app.get(
    "/api/tools",
    response_model=ToolListResponse,
    summary="List every indexed tool",
)
async def list_all_tools(
    q: str | None = Query(
        None, description="Free-text search across servers and tools."
    ),
    server_id: str | None = Query(None, description="Restrict to one MCP server."),
) -> ToolListResponse:
    await _ensure_loaded()
    records: list[ToolRecord] = []
    for entry in _servers.values():
        if server_id and entry.id != server_id:
            continue
        if not _matches(entry, q):
            continue
        for tool in entry.tools:
            records.append(
                ToolRecord(server_id=entry.id, server_name=entry.name, tool=tool)
            )
    return ToolListResponse(count=len(records), tools=records)


@app.get(
    "/api/mcps/{mcp_id}/health",
    response_model=HealthProbeResult,
    summary="Probe an upstream MCP health endpoint",
)
async def probe_health(mcp_id: str) -> HealthProbeResult:
    await _ensure_loaded()
    return await _probe(_find(mcp_id))


@app.get("/api/mcps/{mcp_id}/readme", summary="Read an entry's markdown documentation")
async def read_mcp_readme(mcp_id: str) -> Response:
    await _ensure_loaded()
    entry = _find(mcp_id)
    if not entry.readme:
        raise HTTPException(status_code=404, detail="This entry has no readme.")

    if entry.readme.startswith(("http://", "https://")):
        return RedirectResponse(entry.readme, status_code=302)

    text = _read_readme(entry)
    if text is None:
        raise HTTPException(
            status_code=404,
            detail=f"Readme not found at '{entry.readme}' under {PROJECT_ROOT}.",
        )
    return Response(content=text, media_type="text/markdown; charset=utf-8")


@app.post(
    "/api/mcps",
    response_model=McpServerEntry,
    response_model_by_alias=True,
    status_code=201,
    summary="Register or update an MCP server",
    dependencies=[Depends(require_write_access)],
)
async def upsert_mcp(payload: McpServerUpsert) -> McpServerEntry:
    await _ensure_loaded()
    return await _upsert_entry(payload)


@app.put(
    "/api/mcps/{mcp_id}",
    response_model=McpServerEntry,
    response_model_by_alias=True,
    summary="Replace an indexed MCP server",
    dependencies=[Depends(require_write_access)],
)
async def put_mcp(mcp_id: str, payload: McpServerUpsert) -> McpServerEntry:
    await _ensure_loaded()
    if payload.id != mcp_id:
        raise HTTPException(
            status_code=422,
            detail=f"Payload id '{payload.id}' does not match path id '{mcp_id}'.",
        )
    return await _upsert_entry(payload)


@app.delete(
    "/api/mcps/{mcp_id}",
    status_code=204,
    summary="Remove an MCP server from the index",
    dependencies=[Depends(require_write_access)],
)
async def delete_mcp(mcp_id: str) -> Response:
    await _ensure_loaded()
    async with _registry_lock:
        if mcp_id not in _servers:
            raise HTTPException(
                status_code=404,
                detail=f"MCP server '{mcp_id}' is not indexed.",
            )
        del _servers[mcp_id]
        _save_registry()
    logger.info("removed MCP server %s", mcp_id)
    return Response(status_code=204)


@app.post(
    "/api/reload",
    summary="Reload the registry from disk",
    dependencies=[Depends(require_write_access)],
)
async def reload_registry() -> dict:
    count = await _reload_registry()
    return {"status": "ok", "servers": count, "registry": str(REGISTRY_PATH)}


async def _upsert_entry(payload: McpServerUpsert) -> McpServerEntry:
    now = datetime.now(timezone.utc)
    async with _registry_lock:
        existing = _servers.get(payload.id)
        data = payload.model_dump()
        data["added_at"] = payload.added_at or (existing.added_at if existing else now)
        data["updated_at"] = payload.updated_at or now
        entry = McpServerEntry.model_validate(data)
        _servers[entry.id] = entry
        _save_registry()

    logger.info("registered MCP server %s (%d tools)", entry.id, len(entry.tools))
    return entry


# ---------------------------------------------------------------------------
# MCP surface (JSON-RPC 2.0 over HTTP)
# ---------------------------------------------------------------------------


MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_mcps",
        "title": "List MCP Servers",
        "description": "List indexed MCP servers, optionally filtered by tag or transport.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tag": {"type": "string", "description": "Exact tag filter."},
                "transport": {
                    "type": "string",
                    "description": 'Transport filter, e.g. "http" or "stdio".',
                },
            },
        },
    },
    {
        "name": "search_mcps",
        "title": "Search MCP Servers",
        "description": (
            "Search the index by id, name, description, tags, tool names or "
            "tool descriptions."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Free-text query.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_mcp",
        "title": "Get MCP Server",
        "description": "Return the full registry entry, including tool input schemas.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {
                    "type": "string",
                    "description": 'Registry id, e.g. "mcp-seedream".',
                }
            },
            "required": ["id"],
        },
    },
    {
        "name": "list_tools",
        "title": "List Tools",
        "description": "List tools across all indexed servers, optionally filtered.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "description": "Free-text query."},
                "server_id": {
                    "type": "string",
                    "description": "Restrict results to one server.",
                },
            },
        },
    },
    {
        "name": "get_mcp_markdown",
        "title": "Get MCP Markdown",
        "description": (
            "Return the agent-facing markdown documentation. Without id, returns "
            "this index service's own /mcp.md. With id, returns that server's "
            "readme when available."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Optional MCP server id.",
                }
            },
        },
    },
    {
        "name": "check_mcp_health",
        "title": "Check MCP Health",
        "description": "Probe an indexed server's health endpoint and report latency.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Registry id to probe.",
                }
            },
            "required": ["id"],
        },
    },
]


class _ToolError(Exception):
    pass


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _filtered_entries(tag: str | None, transport: str | None) -> list[McpServerEntry]:
    return [
        entry
        for entry in _servers.values()
        if (tag is None or tag in entry.tags)
        and (transport is None or entry.transport.lower() == transport.lower())
    ]


async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "list_mcps":
        tag = arguments.get("tag")
        transport = arguments.get("transport")
        entries = _filtered_entries(
            tag if isinstance(tag, str) else None,
            transport if isinstance(transport, str) else None,
        )
        return _text_result(
            _json_text(
                {
                    "count": len(entries),
                    "servers": [
                        _summary(entry).model_dump(mode="json") for entry in entries
                    ],
                }
            )
        )

    if name == "search_mcps":
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise _ToolError("'query' must be a non-empty string.")
        limit = arguments.get("limit", 20)
        if not isinstance(limit, int) or isinstance(limit, bool):
            limit = 20
        limit = max(1, min(100, limit))
        entries = [entry for entry in _servers.values() if _matches(entry, query)]
        return _text_result(
            _json_text(
                {
                    "query": query,
                    "count": len(entries),
                    "servers": [
                        _summary(entry).model_dump(mode="json")
                        for entry in entries[:limit]
                    ],
                }
            )
        )

    if name == "get_mcp":
        mcp_id = arguments.get("id")
        if not isinstance(mcp_id, str) or not mcp_id:
            raise _ToolError("'id' must be a non-empty string.")
        entry = _servers.get(mcp_id)
        if entry is None:
            raise _ToolError(f"MCP server '{mcp_id}' is not indexed.")
        return _text_result(_json_text(entry.model_dump(mode="json", by_alias=True)))

    if name == "list_tools":
        query = arguments.get("query")
        server_id = arguments.get("server_id")
        records: list[dict[str, Any]] = []
        for entry in _servers.values():
            if isinstance(server_id, str) and server_id and entry.id != server_id:
                continue
            if isinstance(query, str) and query and not _matches(entry, query):
                continue
            for tool in entry.tools:
                records.append(
                    {
                        "server_id": entry.id,
                        "server_name": entry.name,
                        "tool": tool.model_dump(mode="json", by_alias=True),
                    }
                )
        return _text_result(_json_text({"count": len(records), "tools": records}))

    if name == "get_mcp_markdown":
        mcp_id = arguments.get("id")
        if mcp_id is None or mcp_id == "":
            return _text_result(_render_markdown())
        if not isinstance(mcp_id, str):
            raise _ToolError("'id' must be a string when provided.")
        entry = _servers.get(mcp_id)
        if entry is None:
            raise _ToolError(f"MCP server '{mcp_id}' is not indexed.")
        text = _read_readme(entry)
        if text is None:
            if entry.readme and entry.readme.startswith(("http://", "https://")):
                return _text_result(f"Readme URL: {entry.readme}")
            raise _ToolError(f"No local markdown readme found for '{mcp_id}'.")
        return _text_result(text)

    if name == "check_mcp_health":
        mcp_id = arguments.get("id")
        if not isinstance(mcp_id, str) or not mcp_id:
            raise _ToolError("'id' must be a non-empty string.")
        entry = _servers.get(mcp_id)
        if entry is None:
            raise _ToolError(f"MCP server '{mcp_id}' is not indexed.")
        result = await _probe(entry)
        return _text_result(_json_text(result.model_dump(mode="json")))

    raise _ToolError(f"Unknown tool '{name}'.")


def _rpc_error(
    rpc_id: Any,
    code: int,
    message: str,
    *,
    status_code: int = 200,
) -> JSONResponse:
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": code, "message": message},
        },
        status_code=status_code,
    )


def _rpc_result(
    rpc_id: Any, result: Any, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": rpc_id, "result": result},
        headers=headers,
    )


@app.post("/mcp", summary="MCP JSON-RPC endpoint")
async def mcp_endpoint(request: Request) -> Response:
    await _ensure_loaded()

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - malformed JSON is a parse error
        return _rpc_error(
            None, -32700, "Parse error: request body is not JSON.", status_code=400
        )

    if not isinstance(payload, dict):
        return _rpc_error(None, -32600, "Invalid Request: expected a JSON object.")

    method = payload.get("method")
    rpc_id = payload.get("id")
    params = payload.get("params") or {}

    if not isinstance(method, str):
        return _rpc_error(rpc_id, -32600, "Invalid Request: missing method.")

    if method.startswith("notifications/"):
        logger.debug("MCP notification %s", method)
        return Response(status_code=202)

    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        protocol_version = (
            requested
            if isinstance(requested, str) and requested
            else DEFAULT_MCP_PROTOCOL_VERSION
        )
        result = {
            "protocolVersion": protocol_version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
            "serverInfo": {"name": "mcp-index", "version": app.version},
            "instructions": (
                "MCP index service. Use list_mcps/search_mcps to discover servers, "
                "get_mcp for full entries and tool schemas, list_tools to browse tools, "
                "check_mcp_health to probe upstreams, and get_mcp_markdown for the "
                "agent-facing documentation. HTTP docs: GET /mcp.md."
            ),
        }
        session_id = uuid.uuid4().hex
        return _rpc_result(rpc_id, result, headers={"Mcp-Session-Id": session_id})

    if method == "ping":
        return _rpc_result(rpc_id, {})

    if method == "tools/list":
        return _rpc_result(rpc_id, {"tools": MCP_TOOLS})

    if method == "tools/call":
        if not isinstance(params, dict):
            return _rpc_error(rpc_id, -32602, "Invalid params: expected an object.")
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            return _rpc_error(rpc_id, -32602, "Invalid params: missing tool name.")
        if not isinstance(arguments, dict):
            return _rpc_error(
                rpc_id, -32602, "Invalid params: arguments must be an object."
            )

        try:
            result = await _call_tool(name, arguments)
        except _ToolError as exc:
            return _rpc_result(rpc_id, _text_result(str(exc), is_error=True))
        except Exception as exc:
            logger.exception("tool %s failed", name)
            return _rpc_result(
                rpc_id,
                _text_result(f"Tool '{name}' failed: {exc}", is_error=True),
            )
        return _rpc_result(rpc_id, result)

    if method == "resources/list":
        return _rpc_result(
            rpc_id,
            {
                "resources": [
                    {
                        "uri": "mcp-index://docs",
                        "name": "mcp_index_docs",
                        "title": "MCP Index documentation",
                        "description": (
                            "Agent-facing markdown describing this index service, "
                            "its MCP tools and the current registry."
                        ),
                        "mimeType": "text/markdown",
                    }
                ]
            },
        )

    if method == "resources/read":
        uri = params.get("uri") if isinstance(params, dict) else None
        if uri != "mcp-index://docs":
            return _rpc_error(rpc_id, -32602, f"Unknown resource URI: {uri!r}")
        return _rpc_result(
            rpc_id,
            {
                "contents": [
                    {
                        "uri": "mcp-index://docs",
                        "mimeType": "text/markdown",
                        "text": _render_markdown(),
                    }
                ]
            },
        )

    return _rpc_error(rpc_id, -32601, f"Method not found: {method}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8100")),
    )
