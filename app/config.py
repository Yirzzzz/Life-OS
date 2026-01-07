from __future__ import annotations

import os
from pathlib import Path

APP_VERSION = os.getenv("LIFEOS_VERSION", "v0.1")
DEVELOPER_ID = os.getenv("LIFEOS_DEVELOPER_ID", "????????????")

def load_env() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        if key not in os.environ:
            os.environ[key] = value.strip()
