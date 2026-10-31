"""One-command launcher for the MCP Index service."""

from __future__ import annotations

import argparse
import importlib.util
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MCP_INDEX_DIR = ROOT / "mcp_index"
ENV_FILE = MCP_INDEX_DIR / ".env"
REQUIREMENTS = MCP_INDEX_DIR / "requirements.txt"
REQUIRED_MODULES = ("fastapi", "uvicorn", "httpx", "pydantic")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8100


def load_dotenv(path: Path) -> None:
    """Load mcp_index/.env without overwriting existing environment variables."""
    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def missing_modules() -> list[str]:
    return [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]


def port_in_use(host: str, port: int) -> bool:
    if port <= 0:
        return False
    probe_host = "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host
    try:
        with socket.create_connection((probe_host, port), timeout=0.5):
            return True
    except OSError:
        return False


def registry_path() -> Path:
    raw = os.environ.get("INDEX_REGISTRY", "").strip()
    path = Path(raw).expanduser() if raw else MCP_INDEX_DIR / "registry.json"
    if not path.is_absolute():
        path = MCP_INDEX_DIR / path
    return path.resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start the MCP Index FastAPI service with one command.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST") or DEFAULT_HOST,
        help="Bind address.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=env_int("PORT", DEFAULT_PORT),
        help="Bind port.",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Reload when source files change (development).",
    )
    parser.add_argument(
        "--log-level",
        default=(os.environ.get("LOG_LEVEL") or "info").lower(),
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        help="Uvicorn log level.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ENV_FILE)
    args = build_parser().parse_args(argv)

    missing = missing_modules()
    if missing:
        print(
            "Missing dependencies: " + ", ".join(missing) + "\n"
            "Install them with:\n"
            f"  {sys.executable} -m pip install -r {REQUIREMENTS}",
            file=sys.stderr,
        )
        return 1

    if port_in_use(args.host, args.port):
        print(
            f"Port {args.port} on {args.host} is already in use.\n"
            "Stop the existing service or choose another port with --port.",
            file=sys.stderr,
        )
        return 1

    display_host = "127.0.0.1" if args.host in {"", "0.0.0.0", "::"} else args.host
    base_url = f"http://{display_host}:{args.port}"

    print("MCP Index")
    print(f"  Service:   {base_url}")
    print(f"  Agent doc: {base_url}/mcp.md")
    print(f"  Swagger:   {base_url}/docs")
    print(f"  Registry:  {registry_path()}")
    print("Press Ctrl+C to stop.")
    sys.stdout.flush()

    if str(MCP_INDEX_DIR) not in sys.path:
        sys.path.insert(0, str(MCP_INDEX_DIR))

    import uvicorn

    uvicorn.run(
        "mcp_index_server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(MCP_INDEX_DIR)] if args.reload else None,
        log_level=args.log_level,
        app_dir=str(MCP_INDEX_DIR),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
