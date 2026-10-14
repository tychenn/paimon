"""Validated configuration; relative paths are relative to the config file."""

import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, field_validator

from volition.contracts import StrictModel, VolitionError


class RuntimeConfig(StrictModel):
    state_dir: str = "../runtime/live"
    tick_seconds: float = Field(default=3.0, ge=0.1, le=600)
    history_limit: int = Field(default=20, ge=1, le=100)


class ModelConfig(StrictModel):
    backend: str = "remote"
    base_url: str = "http://127.0.0.1:18000/v1"
    model_id: str = ""
    api_key_env: str = "VOLITION_MODEL_API_KEY"
    api_key_file: str = ""
    timeout_seconds: float = Field(default=90.0, gt=0, le=600)
    retries: int = Field(default=1, ge=0, le=3)
    max_tokens: int = Field(default=2048, ge=128, le=8192)
    # None preserves the server default for other model families.
    enable_thinking: bool | None = None
    script_path: str = ""

    @field_validator("backend")
    @classmethod
    def known_backend(cls, value: str) -> str:
        if value not in {"remote", "scripted"}:
            raise ValueError("model backend must be remote or scripted")
        return value

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("use an HTTP(S) base URL without credentials, query or fragment")
        return value.rstrip("/")


class DesktopConfig(StrictModel):
    backend: str = "workspace"
    workspace_dir: str = "../runtime/workspace"
    environment_id: str = "volition-workspace"
    bridge_url: str = "http://127.0.0.1:18765"
    api_key_env: str = "VOLITION_DESKTOP_API_KEY"

    @field_validator("backend")
    @classmethod
    def known_backend(cls, value: str) -> str:
        if value not in {"workspace", "macos_vm"}:
            raise ValueError("desktop backend must be workspace or macos_vm")
        return value

    @field_validator("bridge_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return ModelConfig.valid_url(value)


class LearningConfig(StrictModel):
    backend: str = "remote"
    enabled: bool = False


class Config(StrictModel):
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            with path.open("rb") as stream:
                config = cls.model_validate(tomllib.load(stream))
        except (OSError, ValueError, ValidationError) as exc:
            # Pydantic errors can include input values, including accidental secrets.
            raise VolitionError(f"配置无法读取或校验失败：{path}（{type(exc).__name__}）") from exc
        base = path.resolve().parent
        config.runtime.state_dir = str(resolve(base, config.runtime.state_dir))
        config.desktop.workspace_dir = str(resolve(base, config.desktop.workspace_dir))
        if config.model.api_key_file:
            config.model.api_key_file = str(resolve(base, config.model.api_key_file))
        if config.model.script_path:
            config.model.script_path = str(resolve(base, config.model.script_path))
        if config.learning.enabled:
            raise VolitionError("参数训练尚未接入，请保持 learning.enabled = false。")
        return config


def resolve(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()
