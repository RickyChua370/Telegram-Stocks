"""Configuration: environment variables (secrets) plus YAML files in /config."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

load_dotenv(ROOT / ".env")


def load_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass
class Settings:
    raw: dict
    telegram_token: str = ""
    telegram_chat_id: str = ""
    gemini_key: str = ""
    groq_key: str = ""
    openai_key: str = ""
    provider_order: list[str] = field(default_factory=lambda: ["gemini", "groq", "openai"])
    gemini_fast: str = "gemini-2.5-flash-lite"
    gemini_smart: str = "gemini-2.5-flash"
    groq_model: str = "openai/gpt-oss-120b"
    openai_model: str = "gpt-4o-mini"
    state_path: Path = ROOT / "state" / "state.json"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.raw.get("timezone", "Asia/Kuala_Lumpur"))

    def get(self, *path, default=None):
        node = self.raw
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def load_settings() -> Settings:
    order = [p.strip().lower() for p in _env("LLM_PROVIDER_ORDER", "gemini,groq,openai").split(",") if p.strip()]
    state_path = Path(_env("STATE_PATH", "state/state.json"))
    if not state_path.is_absolute():
        state_path = ROOT / state_path
    return Settings(
        raw=load_yaml("settings.yaml"),
        telegram_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
        gemini_key=_env("GEMINI_API_KEY"),
        groq_key=_env("GROQ_API_KEY"),
        openai_key=_env("OPENAI_API_KEY"),
        provider_order=order,
        gemini_fast=_env("GEMINI_MODEL_FAST", "gemini-2.5-flash-lite"),
        gemini_smart=_env("GEMINI_MODEL_SMART", "gemini-2.5-flash"),
        groq_model=_env("GROQ_MODEL", "openai/gpt-oss-120b"),
        openai_model=_env("OPENAI_MODEL", "gpt-4o-mini"),
        state_path=state_path,
    )


def load_watchlist() -> list[str]:
    data = load_yaml("watchlist.yaml")
    out = []
    for row in data.get("watchlist") or []:
        code = str(row.get("code") if isinstance(row, dict) else row).strip()
        if code:
            out.append(code.zfill(4) if code.isdigit() and len(code) < 4 else code)
    return out
