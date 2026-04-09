from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .utils import safe_json_loads


@dataclass
class LLMConfig:
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = ""
    temperature: float = 0.7
    max_tokens: int = 1600
    timeout_seconds: int = 90


@dataclass
class WebSearchConfig:
    enabled: bool = False
    provider: str = "tavily"
    api_key: str = ""
    max_results: int = 5


@dataclass
class AgentConfig:
    name: str = "Volition"
    user_name: str = "User"
    hunger_max: int = 100
    hunger_penalty: int = 10
    recovery_max: int = 30
    warning_streak: int = 2
    auto_start: bool = True
    auto_continue: bool = True
    notebook_limit: int = 8


@dataclass
class AppConfig:
    llm: LLMConfig
    web_search: WebSearchConfig
    agent: AgentConfig


class ConfigError(RuntimeError):
    pass


def default_config() -> AppConfig:
    return AppConfig(llm=LLMConfig(), web_search=WebSearchConfig(), agent=AgentConfig())


def app_config_to_dict(config: AppConfig) -> dict[str, Any]:
    return {"llm": asdict(config.llm), "web_search": asdict(config.web_search), "agent": asdict(config.agent)}


def merge_dict(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def _validate_llm_config(config: LLMConfig) -> None:
    missing: list[str] = []
    if not str(config.api_base or "").strip():
        missing.append("llm.api_base")
    if not str(config.api_key or "").strip():
        missing.append("llm.api_key")
    if not str(config.model or "").strip():
        missing.append("llm.model")
    if missing:
        raise ConfigError(
            "Volition 现在必须配置 LLM 才能运行。\n"
            f"请先补全 config.json 里的这些字段：{', '.join(missing)}"
        )


def load_config(project_root: Path) -> AppConfig:
    config_path = project_root / "config.json"
    example_path = project_root / "config.example.json"
    base = app_config_to_dict(default_config())
    if example_path.exists():
        base = merge_dict(base, safe_json_loads(example_path.read_text(encoding="utf-8"), {}))
    if config_path.exists():
        raw = merge_dict(base, safe_json_loads(config_path.read_text(encoding="utf-8"), {}))
    else:
        raw = base
        config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    config = AppConfig(
        llm=LLMConfig(**raw.get("llm", {})),
        web_search=WebSearchConfig(**raw.get("web_search", {})),
        agent=AgentConfig(**raw.get("agent", {})),
    )
    _validate_llm_config(config.llm)
    return config
