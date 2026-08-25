"""配置加载：config/.env 里放密钥，config/config.yaml 放可调参数。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / "config" / ".env"
CONFIG_PATH = ROOT / "config" / "config.yaml"
DB_PATH = ROOT / "data.db"


def _load_env() -> dict[str, str]:
    """读 config/.env，但已存在的真实环境变量优先（方便临时覆盖）。"""
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    env.update({k: v for k, v in os.environ.items() if k in env})
    return env


@dataclass(frozen=True)
class Secrets:
    telegram_bot_token: str
    telegram_chat_id: str
    alpaca_key_id: str
    alpaca_secret: str
    anthropic_api_key: str
    fred_api_key: str = ""

    def require(self, *names: str) -> None:
        """在真正需要某个凭据时才报错，而不是一启动就拦住。"""
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise RuntimeError(
                f"config/.env 缺少：{', '.join(missing)}"
            )


@lru_cache(maxsize=1)
def secrets() -> Secrets:
    env = _load_env()
    return Secrets(
        telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=env.get("TELEGRAM_CHAT_ID", ""),
        alpaca_key_id=env.get("ALPACA_API_KEY_ID", ""),
        alpaca_secret=env.get("ALPACA_API_SECRET", ""),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", ""),
        fred_api_key=env.get("FRED_API_KEY", ""),
    )


@lru_cache(maxsize=1)
def config() -> dict[str, Any]:
    return yaml.safe_load(CONFIG_PATH.read_text())


def watchlists() -> dict[str, dict[str, Any]]:
    return config()["watchlists"]


def all_symbols() -> list[str]:
    """全部标的，去重且保持组内顺序。"""
    seen: dict[str, None] = {}
    for group in watchlists().values():
        for sym in group["symbols"]:
            seen.setdefault(sym, None)
    return list(seen)


def group_of(symbol: str) -> str:
    for name, group in watchlists().items():
        if symbol in group["symbols"]:
            return name
    return "unknown"


def alert_tiers(symbol: str) -> list[float]:
    """该标的所属组的告警阈值（百分比，从小到大）。"""
    group = watchlists().get(group_of(symbol))
    if not group:
        return []
    return sorted(float(p) for p in group["alert"]["pct"])
