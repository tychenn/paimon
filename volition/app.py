from __future__ import annotations

import textwrap
from pathlib import Path

from .agent import VolitionAgent
from .config import ConfigError, load_config


class TerminalApp:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.config = load_config(project_root)
        self.agent = VolitionAgent(project_root, self.config)

    def _print_blocks(self, blocks: list[str]) -> None:
        for block in blocks:
            clean = str(block or "").strip()
            if not clean:
                continue
            print()
            print(clean)
            print()

    def run(self) -> None:
        banner = textwrap.dedent(
            f"""
            Volition Terminal
            project_root={self.project_root}
            model={self.config.llm.model}
            web_search={'on' if self.config.web_search.enabled else 'off'}
            输入 /help 查看命令。
            """.strip()
        )
        print(banner)
        self._print_blocks(self.agent.startup_messages())
        while True:
            try:
                raw = input(f"{self.config.agent.user_name}> ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n退出。")
                return
            if not raw:
                continue
            try:
                reply = self.agent.execute_command(raw) if raw.startswith("/") else self.agent.handle_user_text(raw)
            except SystemExit:
                print("退出。")
                return
            except Exception as exc:
                reply = [f"【错误】{exc}"]
            self._print_blocks(reply or [])
