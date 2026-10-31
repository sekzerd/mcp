"""FastAPI Seedream image-generation service.

POST a prompt to `/generate`, the prompt is forwarded to Volcengine Ark's
image generation API (Doubao Seedream), and the result is returned as JSON.

The upstream call always uses ``response_format="url"`` because that is the
fastest path (no image bytes need to travel through the API response).
Clients that need the bytes inline can set ``return_base64=true``; the server
then downloads the URL and embeds a base64 payload in the JSON.
"""

from __future__ import annotations

import base64
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)
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

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "doubao-seedream-5-0-pro-260628"
MAX_IMAGE_BYTES = 32 * 1024 * 1024

ARK_API_KEY = os.environ.get("ARK_API_KEY", "").strip()
ARK_BASE_URL = os.environ.get("ARK_BASE_URL", DEFAULT_BASE_URL).strip()
ARK_IMAGE_MODEL = os.environ.get("ARK_IMAGE_MODEL", DEFAULT_MODEL).strip()
REQUEST_TIMEOUT = float(os.environ.get("ARK_TIMEOUT", "180"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("seedream-mcp")

client = (
    AsyncOpenAI(
        base_url=ARK_BASE_URL,
        api_key=ARK_API_KEY,
        timeout=REQUEST_TIMEOUT,
        max_retries=1,
    )
    if ARK_API_KEY
    else None
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as http:
        app.state.http = http
        yield


app = FastAPI(
    title="Seedream MCP Server",
    version="1.0.0",
    description=(
        "A small HTTP tool that turns a text prompt into an image via "
        "Volcengine Ark (Doubao Seedream)."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class GenerateImageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="Text description of the image to generate.",
    )
    size: str = Field(
        "2K",
        max_length=24,
        description='Output size, e.g. "1K", "2K", "4K" or "2048x2048".',
    )
    watermark: bool = Field(
        False,
        description="Whether the upstream provider should stamp a watermark.",
    )
    return_base64: bool = Field(
        False,
        description=(
            "When true the server downloads the generated image and embeds it "
            "as base64 in the JSON response."
        ),
    )
    model: str | None = Field(
        None,
        max_length=128,
        description="Optional model override. Defaults to ARK_IMAGE_MODEL.",
    )


class GeneratedImage(BaseModel):
    url: str | None = None
    b64_json: str | None = None
    mime_type: str | None = None
    revised_prompt: str | None = None
    error: str | None = None


class GenerateImageResponse(BaseModel):
    request_id: str
    model: str
    created: int
    prompt: str
    elapsed_ms: int
    images: list[GeneratedImage]


@app.get("/", summary="Service metadata")
async def index() -> dict:
    return {
        "name": "mcp-seedream",
        "version": app.version,
        "endpoint": "POST /generate",
        "docs": "/docs",
    }


@app.get("/health", summary="Health check")
async def health() -> dict:
    return {
        "status": "ok",
        "model": ARK_IMAGE_MODEL,
        "api_key_configured": bool(ARK_API_KEY),
    }


@app.post(
    "/generate",
    response_model=GenerateImageResponse,
    response_model_exclude_none=True,
    summary="Generate an image from a prompt",
)
async def generate_image(
    payload: GenerateImageRequest,
    request: Request,
) -> GenerateImageResponse:
    if client is None:
        raise HTTPException(
            status_code=500,
            detail="ARK_API_KEY is not configured on the server.",
        )

    request_id = uuid.uuid4().hex
    model = payload.model or ARK_IMAGE_MODEL
    started = time.perf_counter()

    logger.info(
        "generate start request_id=%s model=%s size=%s return_base64=%s prompt_len=%d",
        request_id,
        model,
        payload.size,
        payload.return_base64,
        len(payload.prompt),
    )

    try:
        result = await client.images.generate(
            model=model,
            prompt=payload.prompt,
            size=payload.size,
            response_format="url",
            extra_body={"watermark": payload.watermark},
        )
    except APITimeoutError as exc:
        logger.warning("generate timeout request_id=%s", request_id)
        raise HTTPException(
            status_code=504,
            detail="Image generation timed out. Try again or use a smaller size.",
        ) from exc
    except APIStatusError as exc:
        logger.warning(
            "generate upstream error request_id=%s status=%s message=%s",
            request_id,
            exc.status_code,
            exc.message,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Image API error ({exc.status_code}): {exc.message}",
        ) from exc
    except APIConnectionError as exc:
        logger.warning(
            "generate connection error request_id=%s error=%s", request_id, exc
        )
        raise HTTPException(
            status_code=502,
            detail=f"Cannot reach the image API: {exc}",
        ) from exc
    except APIError as exc:
        logger.warning("generate api error request_id=%s error=%s", request_id, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Image API error: {exc}",
        ) from exc

    images: list[GeneratedImage] = []
    for item in result.data or []:
        image = GeneratedImage(
            url=item.url,
            b64_json=item.b64_json,
            revised_prompt=item.revised_prompt,
        )

        if image.url is None and image.b64_json is None:
            continue

        if payload.return_base64 and image.b64_json is None and image.url is not None:
            try:
                image.b64_json, image.mime_type = await _download_as_base64(
                    request.app.state.http,
                    image.url,
                )
            except Exception as exc:  # noqa: BLE001 - report download issues in the payload
                logger.warning(
                    "base64 download failed request_id=%s error=%s",
                    request_id,
                    exc,
                )
                image.error = f"Failed to download image for base64 encoding: {exc}"

        images.append(image)

    if not images:
        raise HTTPException(status_code=502, detail="Image API returned no images.")

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "generate done request_id=%s images=%d elapsed_ms=%d",
        request_id,
        len(images),
        elapsed_ms,
    )

    return GenerateImageResponse(
        request_id=request_id,
        model=model,
        created=int(result.created or time.time()),
        prompt=payload.prompt,
        elapsed_ms=elapsed_ms,
        images=images,
    )


async def _download_as_base64(
    http: httpx.AsyncClient,
    url: str,
) -> tuple[str, str]:
    response = await http.get(url)
    response.raise_for_status()

    content = response.content
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError(f"image is larger than {MAX_IMAGE_BYTES} bytes")

    content_type = response.headers.get("content-type", "image/png")
    mime_type = content_type.split(";")[0].strip() or "image/png"
    encoded = base64.b64encode(content).decode("ascii")
    return encoded, mime_type


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
