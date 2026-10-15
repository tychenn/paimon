"""A real isolated notes workspace and an explicit macOS guest bridge contract."""

import hashlib
import os
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import httpx

from paimon.config import DesktopConfig
from paimon.contracts import Action, Observation, PaimonError


class Environment(Protocol):
    def observe(self) -> Observation: ...
    def execute(self, action: Action, observation: Observation, operation_id: str) -> dict: ...


class WorkspaceEnvironment:
    def __init__(self, directory: Path, environment_id: str):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory.resolve()
        self.environment_id = environment_id

    def target(self, name: str) -> Path:
        path = self.directory / name
        if (
            not name
            or Path(name).name != name
            or name.startswith(".")
            or path.suffix not in {".md", ".txt"}
            or path.is_symlink()
        ):
            raise PaimonError("笔记文件必须是工作区内的单层 .md 或 .txt 文件，不能是符号链接。")
        return path

    def observe(self) -> Observation:
        files = []
        for path in sorted(self.directory.iterdir()):
            if path.is_file() and not path.is_symlink() and path.suffix in {".md", ".txt"}:
                files.append({"name": path.name, "bytes": path.stat().st_size})
        return Observation(
            id=str(uuid4()),
            environment_id=self.environment_id,
            kind="workspace",
            capabilities=["read_note", "write_note"],
            content={
                "files": files[:100],
                "truncated": len(files) > 100,
                "description": "独立笔记文件工作区；尚未连接 macOS 虚拟机桌面。",
            },
        )

    def execute(self, action: Action, observation: Observation, operation_id: str) -> dict:
        if observation.environment_id != self.environment_id:
            raise PaimonError("观察来自另一个环境。")
        path = self.target(action.target)
        if action.name == "read_note":
            # O_NOFOLLOW also closes the symlink swap window between validation and opening.
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                content = stream.read(20001)
            return {"name": path.name, "text": content[:20000], "truncated": len(content) > 20000}
        if action.name != "write_note":
            raise PaimonError("此环境不支持该动作。")
        temporary = self.directory / f".{operation_id}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(action.text)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "name": path.name,
            "bytes": len(action.text.encode()),
            "sha256": hashlib.sha256(action.text.encode()).hexdigest(),
            "written": True,
        }


class MacOSBridge:
    """Client only. A separately installed guest service must implement this contract."""

    def __init__(self, config: DesktopConfig, client: httpx.Client | None = None):
        self.config = config
        self.client = client or httpx.Client(timeout=30, trust_env=False, follow_redirects=False)
        self.owns_client = client is None

    def close(self):
        if self.owns_client:
            self.client.close()

    def headers(self) -> dict:
        key = os.environ.get(self.config.api_key_env)
        if not key:
            raise PaimonError("请先设置虚拟机桥接服务的认证环境变量。")
        return {"Authorization": f"Bearer {key}"}

    def observe(self) -> Observation:
        try:
            response = self.client.get(
                self.config.bridge_url + "/v1/observation", headers=self.headers()
            )
            response.raise_for_status()
            observation = Observation.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise PaimonError("虚拟机观察失败；请确认 guest bridge 已启动。") from exc
        if (
            observation.kind != "macos_vm"
            or observation.environment_id != self.config.environment_id
        ):
            raise PaimonError("虚拟机环境身份不匹配，已停止操作。")
        return observation

    def execute(self, action: Action, observation: Observation, operation_id: str) -> dict:
        if observation.environment_id != self.config.environment_id:
            raise PaimonError("动作与观察的环境身份不匹配。")
        try:
            response = self.client.post(
                self.config.bridge_url + "/v1/actions",
                headers=self.headers(),
                json={
                    "operation_id": operation_id,
                    "environment_id": self.config.environment_id,
                    "observation_id": observation.id,
                    "action": action.model_dump(),
                },
            )
            response.raise_for_status()
            result = response.json()
            if result.get("operation_id") != operation_id or result.get("status") != "completed":
                raise PaimonError("动作结果未确认；不会自动重放。")
            return result
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise PaimonError("桌面动作结果未知；请检查虚拟机，恢复前不会自动重放。") from exc
