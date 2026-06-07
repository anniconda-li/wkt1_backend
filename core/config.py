from __future__ import annotations

import asyncio
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency should be installed from requirements
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"


def configure_asyncio_for_windows() -> None:
    if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def load_project_env() -> None:
    if load_dotenv is not None:
        load_dotenv(ENV_PATH)


configure_asyncio_for_windows()
load_project_env()
