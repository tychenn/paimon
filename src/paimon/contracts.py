"""Typed boundaries shared by the runtime, model and environment adapters."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Action(StrictModel):
    name: Literal[
        "read_note", "write_note", "click", "type_text", "open_url", "press_key", "scroll"
    ]
    target: str = Field(default="", max_length=2000)
    text: str = Field(default="", max_length=20000)


class GoalChange(StrictModel):
    id: str = ""
    title: str = Field(min_length=1, max_length=500)
    status: Literal["active", "completed", "paused"] = "active"
    reason: str = Field(default="", max_length=2000)


class Decision(StrictModel):
    kind: Literal["speak", "act", "wait"]
    rationale: str = Field(default="", max_length=2000)
    message: str = Field(default="", max_length=10000)
    action: Action | None = None
    memory_note: str = Field(default="", max_length=4000)
    goal: GoalChange | None = None
    wait_seconds: int = Field(default=10, ge=1, le=600)

    @model_validator(mode="after")
    def coherent(self) -> "Decision":
        if self.kind == "speak" and (not self.message.strip() or self.action is not None):
            raise ValueError("speak requires a message and cannot execute an action")
        if self.kind == "act" and (self.action is None or self.message):
            raise ValueError("act requires an action and cannot send a message")
        if self.kind == "wait" and (self.action is not None or self.message):
            raise ValueError("wait cannot execute an action or send a message")
        return self


class Observation(StrictModel):
    id: str
    environment_id: str
    kind: Literal["workspace", "macos_vm"]
    capabilities: list[str]
    content: dict[str, Any]


class ModelReply(StrictModel):
    decision: Decision
    raw: str
    model: str
    latency_ms: int
    attempts: int = 1
    usage: dict[str, Any] = Field(default_factory=dict)


class PaimonError(Exception):
    """An expected, user-readable failure."""
