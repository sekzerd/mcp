"""FastAPI Doubao Voice audio-generation service.

POST a prompt to ``/generate``, the request is forwarded to Volcengine
Doubao Voice's audio generation API, and the result is returned as JSON.

The upstream API always returns the generated audio as base64 and also offers
a temporary URL. This service returns the URL by default to keep responses
small; set ``return_base64=true`` to include the base64 payload as well.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, model_validator


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

DEFAULT_BASE_URL = "https://openspeech.bytedance.com"
DEFAULT_MODEL = "seed-audio-1.0"
TTS_PATH = "/api/v3/tts/create"

DOUBAO_VOICE_API_KEY = os.environ.get("DOUBAO_VOICE_API_KEY", "").strip()
DOUBAO_VOICE_APP_ID = os.environ.get("DOUBAO_VOICE_APP_ID", "").strip()
DOUBAO_VOICE_ACCESS_KEY = os.environ.get("DOUBAO_VOICE_ACCESS_KEY", "").strip()
DOUBAO_VOICE_BASE_URL = (
    os.environ.get("DOUBAO_VOICE_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
)
DOUBAO_VOICE_MODEL = os.environ.get("DOUBAO_VOICE_MODEL", DEFAULT_MODEL).strip()
REQUEST_TIMEOUT = float(os.environ.get("DOUBAO_VOICE_TIMEOUT", "300"))

AUDIO_MIME_TYPES = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "pcm": "audio/pcm",
    "ogg_opus": "audio/ogg",
}

SAMPLE_RATES = {
    "wav": {8000, 16000, 24000, 32000, 40000, 44100, 48000},
    "pcm": {8000, 16000, 24000, 32000, 40000, 44100, 48000},
    "mp3": {8000, 16000, 24000, 32000, 44100, 48000},
    "ogg_opus": {48000},
}

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("doubao-voice")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    ) as http:
        app.state.http = http
        yield


app = FastAPI(
    title="Doubao Voice MCP Server",
    version="1.0.0",
    description=(
        "A small HTTP tool that generates audio from a text prompt via "
        "Volcengine Doubao Voice (seed-audio-1.0)."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class AudioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["wav", "mp3", "pcm", "ogg_opus"] = Field(
        "wav",
        description="Output format.",
    )
    sample_rate: int | None = Field(
        None,
        description=(
            "Output sample rate in Hz. The allowed values depend on the format; "
            "when omitted the upstream default is used."
        ),
    )
    speech_rate: int = Field(
        0,
        ge=-50,
        le=100,
        description="Speaking rate, -50 (0.5x) to 100 (2.0x).",
    )
    loudness_rate: int = Field(
        0,
        ge=-50,
        le=100,
        description="Loudness, -50 (0.5x) to 100 (2.0x).",
    )
    pitch_rate: int = Field(
        0,
        ge=-12,
        le=12,
        description="Pitch shift, -12 to 12 semitones.",
    )
    enable_subtitle: bool = Field(
        False,
        description="When true the upstream returns sentence/word timestamps.",
    )

    @model_validator(mode="after")
    def _validate_sample_rate(self) -> AudioConfig:
        allowed = SAMPLE_RATES[self.format]
        if self.sample_rate is not None and self.sample_rate not in allowed:
            raise ValueError(
                f"sample_rate {self.sample_rate} is not supported for "
                f"{self.format}; allowed values: {sorted(allowed)}."
            )
        return self


class WatermarkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aigc_watermark: bool = Field(
        False,
        description="Add an audible AIGC rhythm marker at the end of the audio.",
    )


class AigcMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enable: bool = Field(False, description="Enable the implicit audio watermark.")
    content_producer: str | None = Field(
        None,
        max_length=256,
        description="Name or code of the content producer.",
    )
    produce_id: str | None = Field(
        None,
        max_length=256,
        description="Content production identifier.",
    )
    content_propagator: str | None = Field(
        None,
        max_length=256,
        description="Name or code of the content propagator.",
    )
    propagate_id: str | None = Field(
        None,
        max_length=256,
        description="Content propagation identifier.",
    )


class AudioReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speaker: str | None = Field(
        None,
        max_length=256,
        description="Voice ID from Doubao TTS 2.0 or a cloned voice.",
    )
    audio_data: str | None = Field(
        None,
        description="Base64 encoded reference audio (wav/mp3/pcm/ogg_opus, <=10MB).",
    )
    audio_url: str | None = Field(
        None,
        max_length=2000,
        description="URL of a reference audio file.",
    )
    image_data: str | None = Field(
        None,
        description="Base64 encoded reference image (jpeg/png/webp, <=10MB).",
    )
    image_url: str | None = Field(
        None,
        max_length=2000,
        description="URL of a reference image.",
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> AudioReference:
        sources = [
            name
            for name in (
                "speaker",
                "audio_data",
                "audio_url",
                "image_data",
                "image_url",
            )
            if getattr(self, name)
        ]
        if len(sources) != 1:
            raise ValueError(
                "Each reference must set exactly one of speaker, audio_data, "
                "audio_url, image_data or image_url."
            )
        return self

    @property
    def is_image(self) -> bool:
        return bool(self.image_data or self.image_url)


class GenerateAudioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(
        None,
        max_length=128,
        description="Model override. Defaults to DOUBAO_VOICE_MODEL.",
    )
    text_prompt: str = Field(
        ...,
        min_length=1,
        max_length=3000,
        description=(
            "Prompt or text to synthesize. Use @音频N to reference the N-th "
            "audio in references, starting at 1."
        ),
    )
    references: list[AudioReference] = Field(
        default_factory=list,
        max_length=3,
        description=(
            "At most 3 audio references or 1 image reference. Audio and image "
            "references cannot be mixed."
        ),
    )
    audio_config: AudioConfig = Field(default_factory=AudioConfig)
    watermark: WatermarkConfig | None = None
    aigc_metadata: AigcMetadata | None = None
    return_base64: bool = Field(
        False,
        description=(
            "When true the base64 audio is included in the JSON response. "
            "Otherwise only the temporary URL is returned."
        ),
    )

    @model_validator(mode="after")
    def _validate_reference_mix(self) -> GenerateAudioRequest:
        audio_refs = [ref for ref in self.references if not ref.is_image]
        image_refs = [ref for ref in self.references if ref.is_image]

        if len(audio_refs) > 3:
            raise ValueError("At most 3 audio references are supported.")
        if len(image_refs) > 1:
            raise ValueError("At most 1 image reference is supported.")
        if audio_refs and image_refs:
            raise ValueError("Image references cannot be mixed with audio references.")
        return self


class SubtitleWord(BaseModel):
    model_config = ConfigDict(extra="allow")

    start_time: int
    end_time: int
    text: str


class SubtitleSentence(BaseModel):
    model_config = ConfigDict(extra="allow")

    start_time: int
    end_time: int
    text: str
    words: list[SubtitleWord] = Field(default_factory=list)


class Subtitle(BaseModel):
    model_config = ConfigDict(extra="allow")

    text: str = ""
    sentences: list[SubtitleSentence] = Field(default_factory=list)


class GenerateAudioResponse(BaseModel):
    request_id: str
    model: str
    created: int
    text_prompt: str
    elapsed_ms: int
    duration: float | None = None
    original_duration: float | None = None
    url: str | None = None
    audio: str | None = None
    mime_type: str | None = None
    subtitle: Subtitle | None = None
    logid: str | None = None


def _auth_headers() -> dict[str, str]:
    if DOUBAO_VOICE_API_KEY:
        return {"X-Api-Key": DOUBAO_VOICE_API_KEY}
    if DOUBAO_VOICE_APP_ID and DOUBAO_VOICE_ACCESS_KEY:
        return {
            "X-Api-App-Id": DOUBAO_VOICE_APP_ID,
            "X-Api-Access-Key": DOUBAO_VOICE_ACCESS_KEY,
        }
    raise HTTPException(
        status_code=500,
        detail=(
            "Doubao Voice credentials are not configured. Set "
            "DOUBAO_VOICE_API_KEY (recommended), or both DOUBAO_VOICE_APP_ID "
            "and DOUBAO_VOICE_ACCESS_KEY."
        ),
    )


def _auth_mode() -> str:
    if DOUBAO_VOICE_API_KEY:
        return "api_key"
    if DOUBAO_VOICE_APP_ID and DOUBAO_VOICE_ACCESS_KEY:
        return "app_id_access_key"
    return "missing"


def _upstream_error(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:500]}"

    if not isinstance(data, dict):
        return f"HTTP {response.status_code}: {response.text[:500]}"

    code = data.get("code")
    message = data.get("message") or data.get("error") or response.text[:500]
    if code is None:
        return f"HTTP {response.status_code}: {message}"
    return f"HTTP {response.status_code} code={code}: {message}"


def _is_error_code(code: Any) -> bool:
    return code is not None and code not in (0, 200, "0", "200")


@app.get("/", summary="Service metadata")
async def index() -> dict:
    return {
        "name": "mcp-doubao-voice",
        "version": app.version,
        "endpoint": "POST /generate",
        "model": DOUBAO_VOICE_MODEL,
        "docs": "/docs",
    }


@app.get("/health", summary="Health check")
async def health() -> dict:
    return {
        "status": "ok",
        "model": DOUBAO_VOICE_MODEL,
        "credentials_configured": _auth_mode() != "missing",
        "auth_mode": _auth_mode(),
    }


@app.post(
    "/generate",
    response_model=GenerateAudioResponse,
    response_model_exclude_none=True,
    summary="Generate audio from a text prompt",
)
async def generate_audio(
    payload: GenerateAudioRequest,
    request: Request,
) -> GenerateAudioResponse:
    auth = _auth_headers()
    request_id = uuid.uuid4().hex
    model = payload.model or DOUBAO_VOICE_MODEL
    started = time.perf_counter()

    upstream_body: dict[str, Any] = {
        "model": model,
        "text_prompt": payload.text_prompt,
        "audio_config": payload.audio_config.model_dump(exclude_none=True),
    }
    if payload.references:
        upstream_body["references"] = [
            ref.model_dump(exclude_none=True) for ref in payload.references
        ]
    if payload.watermark is not None:
        upstream_body["watermark"] = payload.watermark.model_dump(exclude_none=True)
    if payload.aigc_metadata is not None:
        upstream_body["aigc_metadata"] = payload.aigc_metadata.model_dump(
            exclude_none=True
        )

    headers = {
        "Content-Type": "application/json",
        "X-Api-Request-Id": request_id,
        **auth,
    }

    logger.info(
        "generate start request_id=%s model=%s format=%s references=%d "
        "text_len=%d return_base64=%s",
        request_id,
        model,
        payload.audio_config.format,
        len(payload.references),
        len(payload.text_prompt),
        payload.return_base64,
    )

    http: httpx.AsyncClient = request.app.state.http
    try:
        response = await http.post(
            f"{DOUBAO_VOICE_BASE_URL}{TTS_PATH}",
            json=upstream_body,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        logger.warning("generate timeout request_id=%s", request_id)
        raise HTTPException(
            status_code=504,
            detail="Audio generation timed out. Try a shorter prompt or retry.",
        ) from exc
    except httpx.RequestError as exc:
        logger.warning(
            "generate connection error request_id=%s error=%s", request_id, exc
        )
        raise HTTPException(
            status_code=502,
            detail=f"Cannot reach the Doubao Voice API: {exc}",
        ) from exc

    logid = response.headers.get("X-Tt-Logid")

    if not response.is_success:
        detail = _upstream_error(response)
        logger.warning(
            "generate upstream error request_id=%s status=%s detail=%s",
            request_id,
            response.status_code,
            detail,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Doubao Voice API error: {detail}",
        )

    try:
        data = response.json()
    except ValueError as exc:
        logger.warning("generate invalid json request_id=%s", request_id)
        raise HTTPException(
            status_code=502,
            detail="Doubao Voice API returned a non-JSON response.",
        ) from exc

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=502,
            detail="Doubao Voice API returned an unexpected response body.",
        )

    code = data.get("code")
    if _is_error_code(code):
        detail = _upstream_error(response)
        logger.warning(
            "generate upstream business error request_id=%s code=%s",
            request_id,
            code,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Doubao Voice API error: {detail}",
        )

    audio = data.get("audio")
    url = data.get("url")
    if not audio and not url:
        raise HTTPException(
            status_code=502,
            detail="Doubao Voice API returned neither audio nor a URL.",
        )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "generate done request_id=%s duration=%s elapsed_ms=%d",
        request_id,
        data.get("duration"),
        elapsed_ms,
    )

    subtitle = None
    if isinstance(data.get("subtitle"), dict):
        try:
            subtitle = Subtitle.model_validate(data["subtitle"])
        except Exception:  # noqa: BLE001 - keep the audio even if captions change
            logger.warning("cannot parse subtitle request_id=%s", request_id)
            subtitle = None

    return GenerateAudioResponse(
        request_id=request_id,
        model=model,
        created=int(time.time()),
        text_prompt=payload.text_prompt,
        elapsed_ms=elapsed_ms,
        duration=data.get("duration"),
        original_duration=data.get("original_duration"),
        url=url,
        audio=audio if payload.return_base64 else None,
        mime_type=AUDIO_MIME_TYPES.get(payload.audio_config.format),
        subtitle=subtitle,
        logid=logid,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8200")),
    )
