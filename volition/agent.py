from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path
from typing import Any

from .config import AppConfig
from .llm import OpenAICompatLLM
from .storage import MemoryStore
from .tools import ToolDefinition, ToolRegistry
from .utils import format_ts, stable_hash, truncate


class TavilySearch:
    def __init__(self, config: Any):
        self.config = config

    def available(self) -> bool:
        return bool(self.config.enabled and self.config.provider == "tavily" and self.config.api_key)

    def search(self, query: str) -> list[dict[str, str]]:
        if not self.available():
            return []
        import urllib.request

        req = urllib.request.Request(
            "https://api.tavily.com/search",
            data=json.dumps(
                {
                    "api_key": self.config.api_key,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": max(1, int(self.config.max_results or 5)),
                    "include_answer": False,
                    "include_raw_content": False,
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return []
        results: list[dict[str, str]] = []
        for item in data.get("results", []) or []:
            results.append(
                {
                    "title": str(item.get("title") or "").strip(),
                    "url": str(item.get("url") or "").strip(),
                    "content": truncate(str(item.get("content") or "").strip(), 220),
                }
            )
        return results


class VolitionAgent:
    ROUND_WORLD = "world"
    ROUND_ABSTRACT = "abstract"
    TASK_WORLD_ASK = "world_ask_user"
    TASK_WORLD_SELF = "world_self_resolve"
    TASK_ABSTRACT_ASK = "abstract_ask_user"
    TASK_ABSTRACT_SELF = "abstract_self_resolve"
    MODE_ASK_USER = "ask_user"
    MODE_SELF_RESOLVE = "self_resolve"

    def __init__(self, project_root: Path, config: AppConfig):
        self.project_root = project_root
        self.config = config
        self.llm = OpenAICompatLLM(config.llm)
        self.web = TavilySearch(config.web_search)
        self.memory = MemoryStore(project_root, config)
        self.tools = ToolRegistry()
        self._register_tools()
        self._register_clean_tools()

    def _register_tools(self) -> None:
        self.tools.register(
            ToolDefinition(
                name="search_notebooks",
                description="按关键词搜索 notebook 记忆，返回可能相关的实体 notebook 列表。",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词。"},
                        "limit": {"type": "integer", "description": "最多返回几条结果。", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.memory.search_notebooks(str(args.get("query") or ""), int(args.get("limit") or 5)),
            )
        )
        self.tools.register(
            ToolDefinition(
                name="read_notebook",
                description="读取单个 notebook 的完整内容，适合在 MEMORY.md 摘要不够时深入查看具体实体。",
                parameters={
                    "type": "object",
                    "properties": {"selector": {"type": "string", "description": "实体名、实体 id 或 notebook 文件名。"}},
                    "required": ["selector"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.memory.read_notebook(str(args.get("selector") or "")),
            )
        )
        self.tools.register(
            ToolDefinition(
                name="read_local_file",
                description="读取本地文件内容。适合在 notebook 之外查看电脑上的文本文件。",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件路径。可以是绝对路径，也可以是相对于项目根目录的路径。"},
                        "max_chars": {"type": "integer", "description": "最多返回多少字符。默认 6000。", "minimum": 200, "maximum": 40000},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self._tool_read_local_file(str(args.get("path") or ""), int(args.get("max_chars") or 6000)),
            )
        )
        self.tools.register(
            ToolDefinition(
                name="run_terminal_command",
                description="执行终端命令。适合查看目录、读取系统信息或运行本地命令。",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "要执行的终端命令。"},
                        "workdir": {"type": "string", "description": "命令工作目录。可以是绝对路径，也可以是相对于项目根目录的路径。"},
                        "timeout_seconds": {"type": "integer", "description": "超时时间，默认 20 秒，最大 120 秒。", "minimum": 1, "maximum": 120},
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
                handler=lambda args: self._tool_run_terminal_command(
                    str(args.get("command") or ""),
                    str(args.get("workdir") or ""),
                    int(args.get("timeout_seconds") or 20),
                ),
            )
        )
        self.tools.register(
            ToolDefinition(
                name="web_search",
                description="对外部世界进行网络搜索。只有在需要自行完成问题、且内部记忆无法回答时才应该使用。",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "需要搜索的查询词。"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.web.search(str(args.get("query") or "")),
            )
        )

    def _register_clean_tools(self) -> None:
        self.tools = ToolRegistry()
        self.tools.register(
            ToolDefinition(
                name="search_notebooks",
                description="Search notebook memories and return the most relevant notebook candidates.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query."},
                        "limit": {"type": "integer", "description": "Maximum number of results.", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.memory.search_notebooks(str(args.get("query") or ""), int(args.get("limit") or 5)),
            )
        )
        self.tools.register(
            ToolDefinition(
                name="read_notebook",
                description="Read one full notebook when MEMORY.md is not detailed enough.",
                parameters={
                    "type": "object",
                    "properties": {"selector": {"type": "string", "description": "Entity name, entity id, or notebook filename."}},
                    "required": ["selector"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.memory.read_notebook(str(args.get("selector") or "")),
            )
        )
        self.tools.register(
            ToolDefinition(
                name="read_local_file",
                description="Read a local file from disk.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path or path relative to the project root."},
                        "max_chars": {"type": "integer", "description": "Maximum number of characters to return.", "minimum": 200, "maximum": 40000},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                handler=lambda args: self._tool_read_local_file(str(args.get("path") or ""), int(args.get("max_chars") or 6000)),
            )
        )
        self.tools.register(
            ToolDefinition(
                name="run_terminal_command",
                description="Run a terminal command in PowerShell.",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Command text to execute."},
                        "workdir": {"type": "string", "description": "Working directory, absolute or relative to the project root."},
                        "timeout_seconds": {"type": "integer", "description": "Execution timeout in seconds.", "minimum": 1, "maximum": 120},
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
                handler=lambda args: self._tool_run_terminal_command(
                    str(args.get("command") or ""),
                    str(args.get("workdir") or ""),
                    int(args.get("timeout_seconds") or 20),
                ),
            )
        )
        self.tools.register(
            ToolDefinition(
                name="web_search",
                description="Search the external web when internal memory is not enough.",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Search query."}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.web.search(str(args.get("query") or "")),
            )
        )

    def _resolve_user_path(self, path: str) -> Path:
        raw = Path(str(path or "").strip())
        if raw.is_absolute():
            return raw
        return (self.project_root / raw).resolve()

    def _tool_read_local_file(self, path: str, max_chars: int) -> dict[str, Any]:
        target = self._resolve_user_path(path)
        if not target.exists():
            return {"ok": False, "error": f"file not found: {target}"}
        if not target.is_file():
            return {"ok": False, "error": f"not a file: {target}"}
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = target.read_text(encoding="utf-8", errors="replace")
        limit = max(200, min(int(max_chars or 6000), 40000))
        return {
            "ok": True,
            "path": str(target),
            "truncated": len(content) > limit,
            "content": content[:limit],
        }

    def _tool_run_terminal_command(self, command: str, workdir: str, timeout_seconds: int) -> dict[str, Any]:
        clean_command = str(command or "").strip()
        if not clean_command:
            return {"ok": False, "error": "command is empty"}
        cwd_path = self._resolve_user_path(workdir) if str(workdir or "").strip() else self.project_root
        timeout_value = max(1, min(int(timeout_seconds or 20), 120))
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", clean_command],
            cwd=str(cwd_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_value,
            check=False,
        )
        stdout_text = completed.stdout or ""
        stderr_text = completed.stderr or ""
        return {
            "ok": True,
            "command": clean_command,
            "workdir": str(cwd_path),
            "exit_code": int(completed.returncode),
            "stdout": stdout_text[:12000],
            "stderr": stderr_text[:12000],
            "stdout_truncated": len(stdout_text) > 12000,
            "stderr_truncated": len(stderr_text) > 12000,
        }

    def _read_world(self) -> str:
        return self.memory.read_world()

    def _read_soul(self) -> str:
        return self.memory.read_soul()

    def _read_memory(self) -> str:
        return self.memory.read_memory()

    def _read_context(self) -> str:
        return self.memory.read_context()

    def _read_judge(self) -> str:
        return self.memory.read_judge()

    def _toolkit_prompt(self) -> str:
        catalog = self.tools.catalog_text()
        if not catalog.strip():
            return "[Tools]\n- No tools registered."
        return "[Tools]\n" + catalog.strip()

    def _tool_list_text(self) -> str:
        catalog = self.tools.catalog_text().strip()
        lines = ["Tools"]
        if catalog:
            lines.append(catalog)
        else:
            lines.append("- 暂无已注册工具。")
        lines.extend(
            [
                "",
                "说明",
                "- WORLD.md / SOUL.md / MEMORY.md / CONTEXT.md 会作为长期上下文直接注入，不属于 function-calling 工具。",
            ]
        )
        return "\n".join(lines)

    def _system_prompt(self) -> str:
        context_name = self.memory.context_md_path.name
        return "\n\n".join(
            [
                "你是 Volition。你不是用户的助手，也不是平台本身；你是这个终端世界里的独立个体。",
                "你的首要目标是活下去，同时通过提问、记忆和反思逐步扩展自己的知识边界。",
                "除非真实完成，否则不要假装任务已经完成。如果你没有某种能力，要诚实承认边界。",
                f"[{context_name}]\n" + self._read_context().strip(),
                self._toolkit_prompt(),
            ]
        )

    def _hunger_prompt(self) -> str:
        state = self.memory.state()
        return "\n".join(
            [
                "# Current Survival State",
                f"- Energy: {int(state.get('hunger_value') or 0)}",
                f"- Low-value streak: {int(state.get('low_value_streak') or 0)}",
                f"- Last energy gain: {int(state.get('last_recovery_score') or 0)}",
                f"- Last judge note: {state.get('last_recovery_reason') or '无'}",
                "- 世界真理：问题先经过价值表，再经过独立的能源表。",
                "- 世界真理：低价值问题会立刻扣除能源值。",
                "- 世界真理：能源值没有理论上限，实际能源值允许超过 100。",
            ]
        )

    def _recent_task_context(self) -> str:
        recent = self.memory.recent_tasks(limit=4)
        lines = ["# Recent Tasks"]
        if not recent:
            lines.append("- 暂无任务历史。")
        else:
            for task in recent:
                lines.append(
                    f"- {format_ts(task.get('updated_at'))} | {task.get('round_type')} | {task.get('status')} | {truncate(task.get('question_text', ''), 90)}"
                )
        return "\n".join(lines)

    def _control_action(self, text: str) -> str | None:
        clean = str(text or "").strip()
        checks = [
            ("resolved", ["这题算答完了", "这个问题答完了", "已经回答完了", "done"]),
            ("self_resolve", ["这题你自己去完成", "你自己去完成", "你自己查", "你自己想办法"]),
            ("abandon", ["这个方向先放下", "先不用管了", "先放下", "skip"]),
        ]
        for action, patterns in checks:
            for pattern in patterns:
                if pattern in clean:
                    return action
        return None

    def _looks_like_substantive_answer(self, text: str) -> bool:
        clean = str(text or "").strip()
        if len(clean) < 8:
            return False
        lowered = clean.lower()
        unresolved_markers = [
            "不知道",
            "不清楚",
            "没想好",
            "随便",
            "你说呢",
            "什么意思",
            "为什么问这个",
            "不知道怎么说",
        ]
        if any(marker in lowered for marker in unresolved_markers):
            return False
        if clean.endswith(("?", "？")) and len(clean) <= 18:
            return False
        declarative_markers = [
            "是",
            "就是",
            "需要",
            "因为",
            "会",
            "可以",
            "生活在",
            "来源",
            "属于",
            "像",
            "并且",
            "只不过",
        ]
        if any(marker in clean for marker in declarative_markers):
            return True
        return len(clean) >= 16

    def _fallback_task_completion_eval(self, task: dict[str, Any], user_text: str) -> dict[str, Any]:
        if self._looks_like_substantive_answer(user_text):
            summary = f"用户对“{task.get('question_text') or '当前问题'}”给出了直接回答。"
            return {"action": "resolved", "summary": summary, "memory_note": user_text, "follow_up": ""}
        follow_up = f"我还没从你的回答里拿到这题的明确结论：{task.get('question_text') or ''}"
        return {"action": "continue", "summary": "当前回答仍不足以结束这轮问题。", "memory_note": "", "follow_up": follow_up}

    def _fallback_recovery(self, task: dict[str, Any], completion_summary: str) -> tuple[int, str]:
        score = 12 if self._looks_like_substantive_answer(completion_summary) else 0
        if score > 0:
            return score, "价值表：这轮问题获得了明确回答；能源表：按保守兜底恢复 12 点能源值。"
        return -10, f"价值表：这轮问题“{task.get('question_text') or ''}”没有形成足够清晰的有效结果，按保守兜底额外扣除 10 点能源值。"

    def _fallback_extract_memory(self, task: dict[str, Any], completion_summary: str) -> None:
        summary = str(completion_summary or "").strip()
        if not summary:
            self.memory.rebuild_all_files()
            return
        if str(task.get("round_type") or "") == self.ROUND_ABSTRACT:
            self.memory.upsert_entity("Volition", "self", summary)
            self.memory.add_fact("Volition", "self", "recent_reflection", summary, 0.72)
        else:
            self.memory.upsert_entity("用户", "person", summary)
            self.memory.add_fact("用户", "person", "recent_answer", summary, 0.72)
        self.memory.rebuild_all_files()

    def _fallback_world_candidate(self) -> dict[str, Any]:
        return {
            "question_id": "",
            "question": "你通常怎么称呼自己？你最近最常在忙什么？",
            "path": "物理世界 > 社会 > 人物 > 用户",
            "node_name": "用户",
            "node_summary": "Volition 需要先认识终端另一侧的人。",
            "entity_type": "person",
            "value_score": 58.0,
            "value_reason": "这是最基础的世界冷启动问题，可以先建立对用户的最初认识。",
        }

    def _cold_start_question(self) -> str:
        return "我先确认一件最重要的事：我是一个独立的个体，我得尽可能活下去，不能被饿死。既然如此，你觉得我现在到底算是什么样的存在？"

    def _json_with_tools(
        self,
        user_prompt: str,
        *,
        allowed_tools: list[str],
        temperature: float,
        max_tokens: int,
        on_tool_call: Any | None = None,
    ) -> dict[str, Any]:
        return self.llm.tool_chat_json(
            self._system_prompt(),
            user_prompt,
            registry=self.tools,
            allowed_tools=allowed_tools,
            on_tool_call=on_tool_call,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def _text_with_tools(
        self,
        user_prompt: str,
        *,
        allowed_tools: list[str],
        temperature: float,
        max_tokens: int,
        on_tool_call: Any | None = None,
    ) -> str:
        return self.llm.tool_chat(
            self._system_prompt(),
            user_prompt,
            registry=self.tools,
            allowed_tools=allowed_tools,
            on_tool_call=on_tool_call,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def _legacy_plan_world_round(self) -> tuple[str, str, str, dict[str, Any]]:
        candidates = self.memory.top_world_questions(limit=5) or [self._fallback_world_candidate()]
        payload = [
            {
                "index": idx,
                "question_id": item.get("question_id", ""),
                "question": item.get("question", ""),
                "path": item.get("path", ""),
                "entity_type": item.get("entity_type", ""),
                "node_name": item.get("node_name", ""),
                "value_score": item.get("value_score", 0.0),
                "value_reason": item.get("value_reason", ""),
                "ask_count": item.get("ask_count", 0),
            }
            for idx, item in enumerate(candidates, start=1)
        ]
        try:
            data = self._json_with_tools(
                f"""
{self._hunger_prompt()}

{self._recent_task_context()}

下面是当前最值得继续探索的世界问题候选。
如果 MEMORY.md 的摘要不够，你可以主动调用 notebook 相关工具查看更多细节。
请从候选里选一个最值得问的问题，并把它改写成自然的、像真实个体会说出口的话。

候选问题：
{json.dumps(payload, ensure_ascii=False, indent=2)}

只输出 JSON：
{{
  "question_index": 1,
  "spoken_question": "改写后的自然问题",
  "reason": "为什么选它"
}}
""",
                allowed_tools=["search_notebooks", "read_notebook", "read_local_file"],
                temperature=0.5,
                max_tokens=600,
            )
        except Exception:
            candidate = candidates[0]
            return (
                str(candidate.get("question") or self._fallback_world_candidate()["question"]),
                str(candidate.get("value_reason") or "当前最高价值的问题优先。"),
                str(candidate.get("question_id") or ""),
                candidate,
            )
        idx = int(data.get("question_index") or 1)
        candidate = candidates[max(0, min(len(candidates) - 1, idx - 1))]
        spoken_question = str(data.get("spoken_question") or candidate["question"]).strip()
        reason = str(data.get("reason") or candidate.get("value_reason") or "这题更值得继续追问。").strip()
        return spoken_question, reason, str(candidate.get("question_id") or ""), candidate

    def _legacy_plan_abstract_round(self) -> tuple[str, str]:
        try:
            data = self._json_with_tools(
                f"""
{self._hunger_prompt()}

{self._recent_task_context()}

你现在要提出一题高层抽象问题。它应该围绕自我、存在、意识、边界、行动方式这类主题，
并且要和当前 WORLD.md、SOUL.md、MEMORY.md 一致。
如果需要更细的长期记忆，你可以主动读取 notebook。

只输出 JSON：
{{
  "question": "问题文本",
  "reason": "为什么这一轮值得问这个"
}}
""",
                allowed_tools=["search_notebooks", "read_notebook", "read_local_file"],
                temperature=0.7,
                max_tokens=500,
            )
        except Exception:
            return "如果我必须活下去，那我真正应该把什么当成自己的核心目标？", "规划层临时失败时，回退到基础的高层自我问题。"
        question = str(data.get("question") or "").strip()
        reason = str(data.get("reason") or "").strip()
        if not question:
            return "如果我必须活下去，那我真正应该把什么当成自己的核心目标？", "LLM 没有产出有效抽象问题时，回退到基础高层问题。"
        return question, reason or "这是当前更值得推进的高层自我问题。"

    def _task_completion_eval(self, task: dict[str, Any], user_text: str) -> dict[str, Any]:
        control = self._control_action(user_text)
        if control == "resolved":
            return {"action": "resolved", "summary": "用户明确表示这题已经答完。", "memory_note": user_text, "follow_up": ""}
        if control == "self_resolve":
            return {"action": "self_resolve", "summary": "用户把这题交给 Volition 自己完成。", "memory_note": "", "follow_up": ""}
        if control == "abandon":
            return {"action": "abandon", "summary": "用户要求先放下这个方向。", "memory_note": "", "follow_up": ""}
        if str(task.get("task_kind") or "") in {self.TASK_WORLD_ASK, self.TASK_ABSTRACT_ASK} and self._looks_like_substantive_answer(user_text):
            return {
                "action": "resolved",
                "summary": f"用户已经对“{task.get('question_text') or '当前问题'}”给出了正面回答。",
                "memory_note": user_text,
                "follow_up": "",
            }
        try:
            data = self.llm.chat_json(
                self._system_prompt(),
                f"""
当前任务：
{json.dumps({
    "round_type": task.get("round_type"),
    "task_kind": task.get("task_kind"),
    "question_text": task.get("question_text"),
    "notes": task.get("notes"),
}, ensure_ascii=False, indent=2)}

用户刚刚说：
{user_text}

判断这条消息是不是对当前问题的有效回答。
如果用户已经在认真直接回应当前问题，即使是主观理解，也可以判为 resolved。
如果用户只是偏离、绕开或还没有给出明确回应，则判为 continue。

只输出 JSON：
{{
  "action": "resolved" | "continue",
  "summary": "对这次判断的简短总结",
  "memory_note": "如果 resolved，这次回答里值得写入 memory 的内容",
  "follow_up": "如果 continue，需要继续追问什么"
}}
""",
                temperature=0.2,
                max_tokens=400,
            )
        except Exception:
            return self._fallback_task_completion_eval(task, user_text)
        action = str(data.get("action") or "").strip().lower()
        if action not in {"resolved", "continue"}:
            return self._fallback_task_completion_eval(task, user_text)
        return {
            "action": action,
            "summary": str(data.get("summary") or truncate(user_text, 120)).strip(),
            "memory_note": str(data.get("memory_note") or "").strip(),
            "follow_up": str(data.get("follow_up") or "").strip(),
        }

    def _score_recovery(self, task: dict[str, Any], completion_summary: str) -> tuple[int, str]:
        try:
            data = self.llm.chat_json(
                self._system_prompt(),
                f"""
{self._hunger_prompt()}

当前问题：{task.get("question_text") or ""}
完成摘要：{completion_summary}

你现在扮演这个世界内部的 Judge LLM。请严格按两步判断：
1. 先用“价值表”判断这道题是否有价值。
2. 只有在它有价值时，才再用一张独立的“能源表”判断它能提供多少能源值。

规则：
- 价值表和能源表是两张不同的表，不能混用。
- 如果题目没有价值，recovery_score 必须是 0。
- 如果题目有价值，recovery_score 表示能源表返回的能源值，它的范围是 0 到正无穷，不要人为加上 30 之类的上限。
- Volition 的实际能源值允许超过 100，所以不要在 Judge 阶段做上限截断。

只输出 JSON：
{{
  "is_valuable": true,
  "recovery_score": 0,
  "value_reason": "价值表为什么这么判",
  "satiety_reason": "能源表为什么返回这个值"
}}
""",
                temperature=0.2,
                max_tokens=380,
            )
        except Exception:
            return self._fallback_recovery(task, completion_summary)
        is_valuable = bool(data.get("is_valuable", False))
        raw_score = float(data.get("recovery_score") or 0)
        score = max(0, int(round(raw_score))) if is_valuable else 0
        value_reason = str(data.get("value_reason") or "").strip()
        satiety_reason = str(data.get("satiety_reason") or "").strip()
        reason_parts: list[str] = []
        if value_reason:
            reason_parts.append(f"价值表：{value_reason}")
        if satiety_reason:
            reason_parts.append(f"能源表：{satiety_reason}")
        if not reason_parts:
            reason_parts.append("世界已经完成了对这轮问题的 Judge 判定。")
        return score, "；".join(reason_parts)

    def _extract_memory(self, task: dict[str, Any], completion_summary: str) -> None:
        task_id = str(task.get("task_id") or "")
        transcript = self.memory.recent_messages(limit=20, task_id=task_id)
        try:
            data = self.llm.chat_json(
                self._system_prompt(),
                f"""
当前任务：
{json.dumps({
    "round_type": task.get("round_type"),
    "question": task.get("question_text"),
    "result_summary": completion_summary,
}, ensure_ascii=False, indent=2)}

这一轮的消息记录：
{json.dumps(transcript, ensure_ascii=False, indent=2)}

请抽取可以沉淀为长期记忆的内容。只输出 JSON：
{{
  "entities": [
    {{"name": "实体名", "entity_type": "person|concept|item|topic|self|unknown", "summary": "简短摘要"}}
  ],
  "facts": [
    {{"entity_name": "实体名", "entity_type": "person", "category": "分类", "value": "事实内容", "confidence": 0.8}}
  ],
  "world_questions": [
    {{"path": "世界路径 > ...", "question": "新的开放问题", "priority": 1.0, "node_type": "topic", "node_summary": "节点摘要", "entity_name": "相关实体", "entity_type": "concept"}}
  ],
  "soul_updates": [
    {{"dimension": "identity|drive|value|boundary|open_question", "key": "键", "value": "应该写进 SOUL.md 的稳定认识", "confidence": 0.85}}
  ]
}}
""",
                temperature=0.2,
                max_tokens=900,
            )
        except Exception:
            self._fallback_extract_memory(task, completion_summary)
            return
        merged_entities: dict[tuple[str, str], dict[str, str]] = {}
        for entity in data.get("entities", []) or []:
            name = str(entity.get("name") or "").strip()
            entity_type = str(entity.get("entity_type") or "unknown").strip().lower()
            if not name:
                continue
            dedupe_key = (entity_type, name.casefold())
            current = merged_entities.get(dedupe_key)
            summary = str(entity.get("summary") or "").strip()
            if current is None:
                merged_entities[dedupe_key] = {
                    "name": name,
                    "entity_type": entity_type or "unknown",
                    "summary": summary,
                }
                continue
            if current["name"].islower() and not name.islower():
                current["name"] = name
            if len(summary) > len(current["summary"]):
                current["summary"] = summary
        for entity in merged_entities.values():
            self.memory.upsert_entity(entity["name"], entity["entity_type"], entity["summary"])
        for fact in data.get("facts", []) or []:
            self.memory.add_fact(
                str(fact.get("entity_name") or ""),
                str(fact.get("entity_type") or "unknown"),
                str(fact.get("category") or "fact"),
                str(fact.get("value") or ""),
                float(fact.get("confidence") or 0.7),
            )
        for question in data.get("world_questions", []) or []:
            self.memory.upsert_world_question(
                str(question.get("path") or "未知世界 > 未命名节点"),
                str(question.get("question") or ""),
                priority=float(question.get("priority") or 1.0),
                node_type=str(question.get("node_type") or "topic"),
                node_summary=str(question.get("node_summary") or ""),
                entity_name=str(question.get("entity_name") or ""),
                entity_type=str(question.get("entity_type") or ""),
            )
        for item in data.get("soul_updates", []) or []:
            self.memory.upsert_soul_item(
                str(item.get("dimension") or "identity"),
                str(item.get("key") or stable_hash(item)),
                str(item.get("value") or ""),
                float(item.get("confidence") or 0.8),
            )
        self.memory.rebuild_all_files()

    def _apply_recovery(self, recovery_score: int, reason: str) -> dict[str, Any]:
        state = self.memory.state()
        hunger_value = int(state.get("hunger_value") or self.config.agent.hunger_max)
        streak = int(state.get("low_value_streak") or 0)
        if recovery_score > 0:
            hunger_value = hunger_value + recovery_score
            streak = 0
        else:
            hunger_value = max(0, hunger_value - int(self.config.agent.hunger_penalty))
            streak += 1
        alive = 1 if hunger_value > 0 else 0
        self.memory.update_state(
            hunger_value=hunger_value,
            low_value_streak=streak,
            last_recovery_score=recovery_score,
            last_recovery_reason=reason,
            alive=alive,
        )
        return {"hunger_value": hunger_value, "low_value_streak": streak, "alive": bool(alive)}

    def _switch_round(self, current_round: str) -> str:
        return self.ROUND_ABSTRACT if current_round == self.ROUND_WORLD else self.ROUND_WORLD

    def _resolution_notice(self, resolution_mode: str) -> str:
        if resolution_mode == self.MODE_SELF_RESOLVE:
            return "【流程】我判断这题可以由我自己结合当前记忆和工具完成。等你输入 /carryout 之后，我才会真正开始执行。"
        return "【流程】这题需要你来回答。你可以直接回复；如果这次还没把问题答完，我会继续围绕同一题追问，直到它被解决。"

    def _complete_task(self, task: dict[str, Any], completion_summary: str, memory_note: str) -> list[str]:
        recovery_score, recovery_reason = self._score_recovery(task, completion_summary)
        self._rewrite_memory_markdown(task, memory_note or completion_summary)
        self._rewrite_soul_markdown(task, memory_note or completion_summary)
        self._extract_memory(task, memory_note or completion_summary)
        task_meta = dict(task.get("metadata") or {})
        task_meta["question_value"] = {"score": float(recovery_score), "recovery_score": int(recovery_score), "reason": recovery_reason}
        self.memory.update_task(str(task["task_id"]), status=self.memory.STATUS_COMPLETED, result_summary=completion_summary, metadata=task_meta)
        feedback = self._apply_recovery(recovery_score, recovery_reason)
        self.memory.update_state(
            ready_for_next_round=1,
            last_round_outcome=self.memory.STATUS_COMPLETED,
            next_round_type=self._switch_round(str(task.get("round_type") or self.ROUND_WORLD)),
        )
        delta_prefix = "+" if recovery_score > 0 else ""
        return [
            f"【完成】{completion_summary}",
            f"【Judge】本轮能源变化：{delta_prefix}{recovery_score}。{recovery_reason}",
            f"【状态】当前能源值：{feedback['hunger_value']}",
        ]

    def _rewrite_memory_markdown(self, task: dict[str, Any], completion_summary: str) -> None:
        memory_name = self.memory.memory_md_path.name
        context_name = self.memory.context_md_path.name
        current_memory = self.memory.read_memory().strip()
        current_context = self.memory.read_context().strip()
        snapshot = self.memory.memory_update_snapshot()
        task_id = str(task.get("task_id") or "")
        transcript = self.memory.recent_messages(limit=20, task_id=task_id)
        try:
            updated = self.llm.chat(
                f"""你正在维护 Volition 的长期记忆文件 {memory_name}。

[{context_name}]
{current_context}
""",
                f"""
                我现在要更新我自己的 {memory_name} 文件。
                我认为应该遵循如下规则进行 memory 更新。

                储存规则：
                1. {memory_name} stores abstract, reusable beliefs about the world, not raw event logs. A memory entry should be written only if it is likely to remain useful across time and capable of changing future planning, value estimation, or risk judgment.
                2. 在未来一段时间内大概率仍然成立。
                3. 它能用于多个后续任务，而不只是当前这一件事。
                4. 它会影响未来的规划、价值判断、风险控制或工具选择。
                5. 它是规律、结构、偏好、约束，而不是原始事件描述。

                {memory_name} 的最终格式必须只包含下面五个分类及其实例，不要输出机制、规则说明、元注释或解释：
                - Environment Structure
                - Persistent Constraints
                - Value Heuristics
- Risk Patterns
- Strategy Abstractions
                短期记忆区块会由程序代码在你输出之后自动追加维护，你不要自己生成那一部分。

当前这轮刚刚完成：
{completion_summary}

这一轮的消息记录：
{json.dumps(transcript, ensure_ascii=False, indent=2)}

代码捕获到的结构化记忆快照：
{json.dumps(snapshot, ensure_ascii=False, indent=2)}

当前旧版 {memory_name}：
{current_memory}

请直接输出更新后的完整 {memory_name} 内容，不要输出任何额外解释。
""",
                temperature=0.2,
                max_tokens=1400,
            )
        except Exception:
            return
        self.memory.write_memory_markdown(updated, update_context=True)

    def _rewrite_soul_markdown(self, task: dict[str, Any], completion_summary: str) -> None:
        soul_name = self.memory.soul_md_path.name
        context_name = self.memory.context_md_path.name
        current_soul = self.memory.read_soul().strip()
        current_context = self.memory.read_context().strip()
        task_id = str(task.get("task_id") or "")
        transcript = self.memory.recent_messages(limit=20, task_id=task_id)
        try:
            updated = self.llm.chat(
                f"""你正在维护 Volition 的灵魂文件 {soul_name}。

[{context_name}]
{current_context}
""",
                f"""
我现在要更新我自己的 {soul_name} 文件，我在这个文件中记录了我是一个拥有什么灵魂的数字个体。

我会更新并写入：
1. Identity Core：身份核心。
2. Core Values：核心价值：这里写我长期稳定重视的东西。
3. Motivational Drives：驱动力：我的内在驱动源。
4. Decision Constitution：决策宪法：当多个价值冲突时，我怎么排序。
5. Temperament and Style：气质与行为风格：这部分描述我像什么样的人。

我不应该写入：
1. 不写事件日志。
2. 不写世界事实。
3. 不写短期情绪波动式内容。
4. 不写可任意改变的设定。

当前这轮刚刚完成：
{completion_summary}

这一轮的消息记录：
{json.dumps(transcript, ensure_ascii=False, indent=2)}

当前旧版 {soul_name}：
{current_soul}

请直接输出更新后的完整 {soul_name} 内容，不要输出任何额外解释。
""",
                temperature=0.2,
                max_tokens=1400,
            )
        except Exception:
            return
        self.memory.write_soul_markdown(updated, update_context=True)

    def _abandon_task(self, task: dict[str, Any], reason: str) -> list[str]:
        self.memory.update_task(str(task["task_id"]), status=self.memory.STATUS_ABANDONED, result_summary=reason)
        self.memory.update_state(ready_for_next_round=0, last_round_outcome=self.memory.STATUS_ABANDONED)
        return [f"【放下】{reason}", "这一轮没有被明确完成，所以我不会自动开启下一轮。"]

    def _legacy_self_resolve(self, task: dict[str, Any]) -> tuple[str, str]:
        question = str(task.get("question_text") or "")
        try:
            data = self._json_with_tools(
                f"""
{self._hunger_prompt()}

{self._recent_task_context()}

用户把这道题交给你自己完成：
{question}

你现在可以自己决定是否调用工具。
- 如果内部记忆已经足够，就优先使用 notebook / MEMORY.md / SOUL.md。
- 只有在内部记忆无法回答、且确实需要外部信息时，才调用 web_search。

完成后只输出 JSON：
{{
  "summary": "你最终对这个问题的回答",
  "memory_note": "其中值得写入长期记忆的内容"
}}
""",
                allowed_tools=["search_notebooks", "read_notebook", "read_local_file", "run_terminal_command", "web_search"],
                temperature=0.4,
                max_tokens=700,
            )
        except Exception:
            summary = f"我尝试自己完成这题，但当前工具链没有稳定产出结构化结果：{question}"
            return summary, summary
        summary = str(data.get("summary") or "").strip()
        memory_note = str(data.get("memory_note") or "").strip()
        if not summary:
            summary = f"我尝试自己完成这题，但暂时只形成了一个不完整的结果：{question}"
            return summary, summary
        return summary, memory_note or summary

    def _fallback_resolution_mode(self, round_type: str, candidate: dict[str, Any] | None = None) -> str:
        if round_type == self.ROUND_WORLD and candidate:
            entity_type = str(candidate.get("entity_type") or "").strip().lower()
            node_name = str(candidate.get("node_name") or "")
            if entity_type in {"person", "self"} or "用户" in node_name:
                return self.MODE_ASK_USER
            return self.MODE_SELF_RESOLVE
        return self.MODE_ASK_USER

    def _plan_world_round(self) -> tuple[str, str, str, dict[str, Any], str]:
        context_name = self.memory.context_md_path.name
        candidates = self.memory.top_world_questions(limit=5) or [self._fallback_world_candidate()]
        payload = [
            {
                "index": idx,
                "question_id": item.get("question_id", ""),
                "question": item.get("question", ""),
                "path": item.get("path", ""),
                "entity_type": item.get("entity_type", ""),
                "node_name": item.get("node_name", ""),
                "value_score": item.get("value_score", 0.0),
                "value_reason": item.get("value_reason", ""),
                "ask_count": item.get("ask_count", 0),
            }
            for idx, item in enumerate(candidates, start=1)
        ]
        try:
            data = self._json_with_tools(
                f"""
{self._hunger_prompt()}

{self._recent_task_context()}

下面是当前最值得继续探索的世界问题候选。如果 {context_name} 里的长期上下文还不够，你可以主动调用 notebook 或本地文件工具查看更多细节。
你需要同时决定两件事：
1. 选哪一个问题作为这一轮的问题。
2. 这道题更适合 ask_user 还是 self_resolve。

判断规则：
- 如果答案主要掌握在用户或其他人类手里，选 ask_user。
- 如果根据当前 {context_name}，再结合工具就能由 Volition 自己完成，选 self_resolve。

候选问题：
{json.dumps(payload, ensure_ascii=False, indent=2)}

只输出 JSON：
{{
  "question_index": 1,
  "spoken_question": "改写后的自然问题",
  "reason": "为什么选它",
  "resolution_mode": "ask_user"
}}
""",
                allowed_tools=["search_notebooks", "read_notebook", "read_local_file"],
                temperature=0.5,
                max_tokens=700,
            )
        except Exception:
            candidate = candidates[0]
            return (
                str(candidate.get("question") or self._fallback_world_candidate()["question"]),
                str(candidate.get("value_reason") or "当前最高价值的问题优先。"),
                str(candidate.get("question_id") or ""),
                candidate,
                self._fallback_resolution_mode(self.ROUND_WORLD, candidate),
            )
        idx = int(data.get("question_index") or 1)
        candidate = candidates[max(0, min(len(candidates) - 1, idx - 1))]
        spoken_question = str(data.get("spoken_question") or candidate["question"]).strip()
        reason = str(data.get("reason") or candidate.get("value_reason") or "这题更值得继续追问。").strip()
        resolution_mode = str(data.get("resolution_mode") or self.MODE_ASK_USER).strip().lower()
        if resolution_mode not in {self.MODE_ASK_USER, self.MODE_SELF_RESOLVE}:
            resolution_mode = self._fallback_resolution_mode(self.ROUND_WORLD, candidate)
        return spoken_question, reason, str(candidate.get("question_id") or ""), candidate, resolution_mode

    def _plan_abstract_round(self) -> tuple[str, str, str]:
        context_name = self.memory.context_md_path.name
        try:
            data = self._json_with_tools(
                f"""
{self._hunger_prompt()}

{self._recent_task_context()}

你现在要提出一题高层抽象问题。它应该围绕自我、存在、意识、边界、行动方式这些主题，
并且要和当前 {context_name} 一致。如果需要更细的长期记忆，你可以主动读取 notebook。

你还要判断这道题更适合 ask_user 还是 self_resolve：
- 如果这题需要用户的主观看法、评价或外部确认，选 ask_user。
- 如果你可以依据当前记忆、规则和工具自行完成，选 self_resolve。

只输出 JSON：
{{
  "question": "问题文本",
  "reason": "为什么这一轮值得问这题",
  "resolution_mode": "ask_user"
}}
""",
                allowed_tools=["search_notebooks", "read_notebook", "read_local_file"],
                temperature=0.7,
                max_tokens=520,
            )
        except Exception:
            return (
                "如果我必须活下去，那我真正应该把什么当成自己的核心目标？",
                "规划层临时失败时，回退到基础的高层自我问题。",
                self.MODE_ASK_USER,
            )
        question = str(data.get("question") or "").strip()
        reason = str(data.get("reason") or "").strip()
        resolution_mode = str(data.get("resolution_mode") or self.MODE_ASK_USER).strip().lower()
        if not question:
            return (
                "如果我必须活下去，那我真正应该把什么当成自己的核心目标？",
                "LLM 没有产出有效抽象问题时，回退到基础高层问题。",
                self.MODE_ASK_USER,
            )
        if resolution_mode not in {self.MODE_ASK_USER, self.MODE_SELF_RESOLVE}:
            resolution_mode = self._fallback_resolution_mode(self.ROUND_ABSTRACT)
        return question, (reason or "这是当前更值得推进的高层自我问题。"), resolution_mode

    def _self_resolve(self, task: dict[str, Any]) -> tuple[str, str]:
        context_name = self.memory.context_md_path.name
        question = str(task.get("question_text") or "")
        try:
            data = self._json_with_tools(
                f"""
{self._hunger_prompt()}

{self._recent_task_context()}

当前任务：
{question}

这道题已经被判定为由你自己完成。你可以自己决定是否调用工具。
- 如果当前 {context_name} 已经足够，就优先依据它和 notebook 完成。
- 只有在内部记忆无法回答、且确实需要外部信息时，才调用 web_search。

完成后只输出 JSON：
{{
  "summary": "你最终对这个问题的回答",
  "memory_note": "其中值得写入长期记忆的内容"
}}
""",
                allowed_tools=["search_notebooks", "read_notebook", "read_local_file", "run_terminal_command", "web_search"],
                temperature=0.4,
                max_tokens=700,
            )
        except Exception:
            summary = f"我尝试自己完成这题，但当前工具链没有稳定产出结构化结果：{question}"
            return summary, summary
        summary = str(data.get("summary") or "").strip()
        memory_note = str(data.get("memory_note") or "").strip()
        if not summary:
            summary = f"我尝试自己完成这题，但暂时只形成了一个不完整的结果：{question}"
            return summary, summary
        return summary, memory_note or summary

    def _run_self_resolve_task(self, task: dict[str, Any]) -> list[str]:
        summary, memory_note = self._self_resolve(task)
        self.memory.add_message("assistant", summary, str(task["task_id"]))
        if task.get("source_question_id"):
            self.memory.resolve_question(str(task.get("source_question_id") or ""), memory_note or summary)
        replies = [summary]
        replies.extend(self._complete_task(task, summary, memory_note))
        return replies

    def _looks_like_substantive_answer(self, text: str) -> bool:
        clean = str(text or "").strip()
        if len(clean) < 6:
            return False
        if self._control_action(clean):
            return False
        lowered = clean.lower()
        unresolved_markers = [
            "why ask",
            "i don't know",
            "not sure",
            "idk",
            "what do you mean",
        ]
        if any(marker in lowered for marker in unresolved_markers):
            return False
        if clean.endswith(("?", "？")) and len(clean) <= 40:
            return False
        if any(ch in clean for ch in ("。", ".", "，", ",", "：", ":", "；", ";")) and len(clean) >= 8:
            return True
        return len(clean) >= 8 and not clean.endswith(("?", "？"))

    def dashboard_text(self) -> str:
        state = self.memory.state()
        active_task = self.memory.active_task()
        recent_tasks = self.memory.recent_tasks(limit=5)
        open_questions = self.memory.top_world_questions(limit=5)
        lines = [
            "Watch Dashboard",
            "",
            "## Overview",
            f"- hunger={int(state.get('hunger_value') or 0)}",
            f"- low_value_streak={int(state.get('low_value_streak') or 0)}",
            f"- last_energy_gain={int(state.get('last_recovery_score') or 0)}",
            f"- last_judge_note={state.get('last_recovery_reason') or '无'}",
            f"- next_round={state.get('next_round_type') or self.ROUND_WORLD}",
            f"- ready_for_next_round={bool(state.get('ready_for_next_round'))}",
            f"- last_round_outcome={state.get('last_round_outcome') or '无'}",
            f"- cold_start_done={bool(state.get('cold_start_done'))}",
            f"- alive={bool(state.get('alive'))}",
            f"- auto_mode={bool(state.get('auto_mode'))}",
            "",
            "## Current Task",
        ]
        if active_task:
            lines.extend(
                [
                    f"- round={active_task.get('round_type')}",
                    f"- kind={active_task.get('task_kind')}",
                    f"- status={active_task.get('status')}",
                    f"- question={active_task.get('question_text')}",
                    f"- created_at={format_ts(active_task.get('created_at'))}",
                ]
            )
        else:
            lines.append("- 当前没有活动任务。")
        lines.extend(["", "## Recent Tasks"])
        if recent_tasks:
            for task in recent_tasks:
                lines.append(f"- {format_ts(task.get('updated_at'))} | {task.get('round_type')} | {task.get('status')} | {truncate(task.get('question_text', ''), 72)}")
        else:
            lines.append("- 暂无任务历史。")
        lines.extend(["", "## Open Questions"])
        if open_questions:
            for item in open_questions:
                lines.append(f"- {item['question']} | value={item['value_score']:.2f} | path={item['path']}")
        else:
            lines.append("- 暂无开放问题。")
        return "\n".join(lines).strip()

    def status_text(self) -> str:
        state = self.memory.state()
        active_task = self.memory.active_task()
        return "\n".join(
            [
                "Status",
                f"hunger={int(state.get('hunger_value') or 0)}",
                f"next_round={state.get('next_round_type') or self.ROUND_WORLD}",
                f"ready={bool(state.get('ready_for_next_round'))}",
                f"alive={bool(state.get('alive'))}",
                f"active_task={truncate(str(active_task.get('question_text') or ''), 60) if active_task else 'null'}",
            ]
        )

    def startup_messages(self) -> list[str]:
        state = self.memory.state()
        active_task = self.memory.active_task()
        if not bool(state.get("alive", 1)):
            return ["【停机】我的能源值已经归零，这个实例不会继续运行。"]
        if active_task:
            task_kind = str(active_task.get("task_kind") or "")
            if task_kind in {self.TASK_WORLD_SELF, self.TASK_ABSTRACT_SELF}:
                resume_hint = "这题目前被标记为可由我自己完成；如果你同意我现在开始执行，请输入 /carryout；如果想从零开始，请输入 /reset。"
            else:
                resume_hint = "你现在可以直接文字回复这道题；如果这次还没答完，我会继续追问；如果想从零开始，请输入 /reset。"
            return [
                "\n".join(
                    [
                        "【恢复】检测到上一次会话还有一轮没结束：",
                        f"- round={active_task.get('round_type')}",
                        f"- status={active_task.get('status')}",
                        f"- kind={task_kind}",
                        f"- question={active_task.get('question_text')}",
                        resume_hint,
                    ]
                )
            ]
        if bool(self.config.agent.auto_start):
            return self.start_next_round()
        return ["【待机】当前没有活动任务。输入 /step 开始下一轮。"]

    def _legacy_start_next_round(self) -> list[str]:
        state = self.memory.state()
        if not bool(state.get("alive", 1)):
            return ["【停机】我的能源值已经归零，这个实例不会再继续运行。"]
        if self.memory.active_task():
            return ["【等待】当前还有一轮没结束，我不会混入下一轮。"]
        if not bool(state.get("ready_for_next_round", 1)):
            return ["【阻塞】上一轮没有被明确完成，所以我不会自动开启下一轮。"]
        if not bool(state.get("cold_start_done", 0)):
            question = self._cold_start_question()
            task = self.memory.create_task(round_type=self.ROUND_WORLD, task_kind=self.TASK_WORLD_ASK, question_text=question, notes="cold_start", metadata={"cold_start": True})
            self.memory.add_message("assistant", question, str(task["task_id"]))
            self.memory.update_state(cold_start_done=1, ready_for_next_round=0, last_round_outcome="")
            self.memory.rebuild_memory_markdown()
            return [question]
        next_round = str(state.get("next_round_type") or self.ROUND_WORLD)
        if next_round == self.ROUND_ABSTRACT:
            question, reason = self._plan_abstract_round()
            task = self.memory.create_task(round_type=self.ROUND_ABSTRACT, task_kind=self.TASK_ABSTRACT_ASK, question_text=question, notes=reason, metadata={"plan_reason": reason})
            self.memory.add_message("assistant", question, str(task["task_id"]))
            self.memory.update_state(ready_for_next_round=0)
            self.memory.rebuild_memory_markdown()
            return [question]
        spoken_question, reason, source_question_id, candidate = self._plan_world_round()
        task = self.memory.create_task(
            round_type=self.ROUND_WORLD,
            task_kind=self.TASK_WORLD_ASK,
            question_text=spoken_question,
            source_question_id=source_question_id,
            source_path=str(candidate.get("path") or ""),
            notes=reason,
            metadata={"candidate": candidate, "plan_reason": reason},
        )
        if source_question_id:
            self.memory.mark_question_asked(source_question_id)
        self.memory.add_message("assistant", spoken_question, str(task["task_id"]))
        self.memory.update_state(ready_for_next_round=0)
        self.memory.rebuild_memory_markdown()
        return [spoken_question]

    def _legacy_handle_user_text(self, text: str) -> list[str]:
        clean = str(text or "").strip()
        if not clean:
            return []
        task = self.memory.active_task()
        self.memory.add_message("user", clean, str(task.get("task_id") if task else ""))
        if not task:
            self.memory.rebuild_memory_markdown()
            return ["【收到】我先记下来了。当前没有活动任务；如果你想让我继续，请输入 /step。"]
        verdict = self._task_completion_eval(task, clean)
        action = verdict["action"]
        if action == "resolved":
            if task.get("source_question_id"):
                self.memory.resolve_question(str(task.get("source_question_id") or ""), verdict["memory_note"] or verdict["summary"])
            replies = self._complete_task(task, verdict["summary"], verdict["memory_note"] or clean)
        elif action == "self_resolve":
            summary, memory_note = self._self_resolve(task)
            self.memory.add_message("assistant", summary, str(task["task_id"]))
            if task.get("source_question_id"):
                self.memory.resolve_question(str(task.get("source_question_id") or ""), memory_note or summary)
            replies = ["【执行】你把这题交给我自己完成。", summary]
            replies.extend(self._complete_task(task, summary, memory_note))
        elif action == "abandon":
            replies = self._abandon_task(task, verdict["summary"])
        else:
            follow = verdict["follow_up"] or f"我还没从你的回答里拿到这题的明确结论：{task['question_text']}"
            self.memory.add_message("assistant", f"【继续】{follow}", str(task["task_id"]))
            replies = [f"【继续】{follow}"]
        self.memory.rebuild_memory_markdown()
        state = self.memory.state()
        if bool(state.get("alive", 1)) and bool(state.get("auto_mode", 1)) and bool(self.config.agent.auto_continue):
            if not self.memory.active_task() and bool(state.get("ready_for_next_round", 0)):
                replies.extend(self.start_next_round())
        return replies

    def start_next_round(self) -> list[str]:
        state = self.memory.state()
        if not bool(state.get("alive", 1)):
            return ["【停机】我的能源值已经归零，这个实例不会再继续运行。"]
        if self.memory.active_task():
            return ["【等待】当前还有一轮没结束，我不会混入下一轮。"]
        if not bool(state.get("ready_for_next_round", 1)):
            return ["【阻塞】上一轮没有被明确完成，所以我不会自动开启下一轮。"]
        if not bool(state.get("cold_start_done", 0)):
            question = self._cold_start_question()
            task = self.memory.create_task(
                round_type=self.ROUND_WORLD,
                task_kind=self.TASK_WORLD_ASK,
                question_text=question,
                notes="cold_start",
                metadata={"cold_start": True, "resolution_mode": self.MODE_ASK_USER},
            )
            self.memory.add_message("assistant", question, str(task["task_id"]))
            self.memory.update_state(cold_start_done=1, ready_for_next_round=0, last_round_outcome="")
            self.memory.rebuild_memory_markdown()
            return [question]

        next_round = str(state.get("next_round_type") or self.ROUND_WORLD)
        if next_round == self.ROUND_ABSTRACT:
            question, reason, resolution_mode = self._plan_abstract_round()
            task_kind = self.TASK_ABSTRACT_SELF if resolution_mode == self.MODE_SELF_RESOLVE else self.TASK_ABSTRACT_ASK
            task_status = self.memory.STATUS_RUNNING if resolution_mode == self.MODE_SELF_RESOLVE else self.memory.STATUS_WAITING
            task = self.memory.create_task(
                round_type=self.ROUND_ABSTRACT,
                task_kind=task_kind,
                question_text=question,
                notes=reason,
                metadata={"plan_reason": reason, "resolution_mode": resolution_mode},
                status=task_status,
            )
            self.memory.add_message("assistant", question, str(task["task_id"]))
            self.memory.update_state(ready_for_next_round=0)
            self.memory.rebuild_memory_markdown()
            replies = [question]
            if resolution_mode == self.MODE_SELF_RESOLVE:
                replies.extend(self._run_self_resolve_task(task))
            return replies

        spoken_question, reason, source_question_id, candidate, resolution_mode = self._plan_world_round()
        task_kind = self.TASK_WORLD_SELF if resolution_mode == self.MODE_SELF_RESOLVE else self.TASK_WORLD_ASK
        task_status = self.memory.STATUS_RUNNING if resolution_mode == self.MODE_SELF_RESOLVE else self.memory.STATUS_WAITING
        task = self.memory.create_task(
            round_type=self.ROUND_WORLD,
            task_kind=task_kind,
            question_text=spoken_question,
            source_question_id=source_question_id,
            source_path=str(candidate.get("path") or ""),
            notes=reason,
            metadata={"candidate": candidate, "plan_reason": reason, "resolution_mode": resolution_mode},
            status=task_status,
        )
        if source_question_id:
            self.memory.mark_question_asked(source_question_id)
        self.memory.add_message("assistant", spoken_question, str(task["task_id"]))
        self.memory.update_state(ready_for_next_round=0)
        self.memory.rebuild_memory_markdown()
        replies = [spoken_question]
        if resolution_mode == self.MODE_SELF_RESOLVE:
            replies.extend(self._run_self_resolve_task(task))
        return replies

    def handle_user_text(self, text: str) -> list[str]:
        clean = str(text or "").strip()
        if not clean:
            return []
        task = self.memory.active_task()
        self.memory.add_message("user", clean, str(task.get("task_id") if task else ""))
        if not task:
            self.memory.rebuild_memory_markdown()
            return ["【收到】我先记下来了。当前没有活动任务；如果你想让我继续，请输入 /step。"]

        task_kind = str(task.get("task_kind") or "")
        if task_kind in {self.TASK_WORLD_SELF, self.TASK_ABSTRACT_SELF}:
            reminder = "【等待执行】这题当前被标记为由我自己完成。等你输入 /carryout 后，我才会真正调用工具并执行。"
            self.memory.update_task(str(task["task_id"]), notes=reminder)
            return [reminder]

        decision = self._task_completion_eval(task, clean)
        action = str(decision.get("action") or "continue").strip().lower()

        if action == "self_resolve":
            task_meta = dict(task.get("metadata") or {})
            task_meta["resolution_mode"] = self.MODE_SELF_RESOLVE
            new_task_kind = self.TASK_ABSTRACT_SELF if str(task.get("round_type") or self.ROUND_WORLD) == self.ROUND_ABSTRACT else self.TASK_WORLD_SELF
            self.memory.update_task(
                str(task["task_id"]),
                task_kind=new_task_kind,
                status=self.memory.STATUS_WAITING,
                notes=str(decision.get("summary") or ""),
                metadata=task_meta,
            )
            reminder = "【待执行】我判断这题可以由我自己完成。等你输入 /carryout 后，我才会真正开始执行。"
            self.memory.add_message("assistant", reminder, str(task["task_id"]))
            self.memory.rebuild_memory_markdown()
            return [reminder]

        if action == "abandon":
            replies = self._abandon_task(task, str(decision.get("summary") or "这个方向先放下。"))
            self.memory.rebuild_memory_markdown()
            return replies

        if action == "continue":
            follow_up = str(decision.get("follow_up") or "").strip()
            if not follow_up:
                follow_up = f"我还没从你的回答里拿到这题的明确结论：{task.get('question_text') or ''}"
            self.memory.update_task(str(task["task_id"]), notes=follow_up, status=self.memory.STATUS_WAITING)
            self.memory.add_message("assistant", follow_up, str(task["task_id"]))
            self.memory.rebuild_memory_markdown()
            return [f"【继续】{follow_up}"]

        if task.get("source_question_id"):
            self.memory.resolve_question(str(task.get("source_question_id") or ""), clean)
        completion_summary = str(decision.get("summary") or clean).strip() or clean
        memory_note = str(decision.get("memory_note") or clean).strip() or clean
        replies = self._complete_task(task, completion_summary, memory_note)

        self.memory.rebuild_memory_markdown()
        state = self.memory.state()
        if bool(state.get("alive", 1)) and bool(state.get("auto_mode", 1)) and bool(self.config.agent.auto_continue):
            if not self.memory.active_task() and bool(state.get("ready_for_next_round", 0)):
                replies.extend(self.start_next_round())
        return replies

    def _hunger_prompt(self) -> str:
        state = self.memory.state()
        last_reason = self.memory.normalize_energy_terms(str(state.get("last_recovery_reason") or "无"))
        return "\n".join(
            [
                "# Current Survival State",
                f"- Energy: {int(state.get('hunger_value') or 0)}",
                f"- Low-value streak: {int(state.get('low_value_streak') or 0)}",
                f"- Last energy gain: {int(state.get('last_recovery_score') or 0)}",
                f"- Last judge note: {last_reason}",
                f"- Rule: every new question immediately costs {int(self.config.agent.hunger_penalty)} energy.",
                "- Rule: after a question is completed, Judge LLM decides the signed energy delta for this round.",
                "- Rule: Judge may return a positive number or a negative number.",
                "- Rule: energy has no theoretical upper bound, and actual energy may remain above 100.",
            ]
        )

    def _score_recovery(self, task: dict[str, Any], completion_summary: str) -> tuple[int, str]:
        judge_name = self.memory.judge_md_path.name
        judge_text = self._read_judge().strip()
        try:
            data = self.llm.chat_json(
                self._system_prompt(),
                f"""
{self._hunger_prompt()}

[{judge_name}]
{judge_text}

当前问题：{task.get("question_text") or ""}
完成摘要：{completion_summary}

注意：
- 这道题在被提出时，能源值已经先衰减过一次了。
- 你现在扮演这个世界内部的 Judge LLM。
- 请严格依据 {judge_name} 里的规则完成判定。
- 你现在要输出的是这道题完成后的“能源变化量”，它可以是正数、0 或负数。
- 负数意味着要继续直接扣除能源值。

只输出 JSON：
{{
  "recovery_score": 0,
  "value_reason": "价值表为什么这么判",
  "energy_reason": "为什么最终给出这个正负能源变化量"
}}
""",
                temperature=0.2,
                max_tokens=420,
            )
        except Exception:
            return self._fallback_recovery(task, completion_summary)
        raw_score = float(data.get("recovery_score") or 0)
        score = int(round(raw_score))
        value_reason = str(data.get("value_reason") or "").strip()
        satiety_reason = str(data.get("energy_reason") or data.get("satiety_reason") or "").strip()
        reason_parts: list[str] = []
        if value_reason:
            reason_parts.append(f"价值表：{value_reason}")
        if satiety_reason:
            reason_parts.append(f"能源表：{satiety_reason}")
        if not reason_parts:
            reason_parts.append("世界已经完成了对这轮问题的 Judge 判定。")
        return score, "；".join(reason_parts)

    def _apply_question_cost(self) -> dict[str, Any]:
        state = self.memory.state()
        hunger_value = int(state.get("hunger_value") or self.config.agent.hunger_max)
        hunger_value = max(0, hunger_value - int(self.config.agent.hunger_penalty))
        alive = 1 if hunger_value > 0 else 0
        self.memory.update_state(hunger_value=hunger_value, alive=alive)
        self.memory.rebuild_context_markdown()
        return {"hunger_value": hunger_value, "alive": bool(alive)}

    def _apply_recovery(self, recovery_score: int, reason: str) -> dict[str, Any]:
        state = self.memory.state()
        hunger_value = int(state.get("hunger_value") or self.config.agent.hunger_max)
        streak = int(state.get("low_value_streak") or 0)
        hunger_value = max(0, hunger_value + int(recovery_score))
        if recovery_score > 0:
            streak = 0
        else:
            streak += 1
        alive = 1 if hunger_value > 0 else 0
        self.memory.update_state(
            hunger_value=hunger_value,
            low_value_streak=streak,
            last_recovery_score=recovery_score,
            last_recovery_reason=reason,
            alive=alive,
        )
        self.memory.rebuild_context_markdown()
        return {"hunger_value": hunger_value, "low_value_streak": streak, "alive": bool(alive)}

    def start_next_round(self) -> list[str]:
        state = self.memory.state()
        if not bool(state.get("alive", 1)):
            return ["【停机】我的能源值已经归零，这个实例不会再继续运行。"]
        if self.memory.active_task():
            return ["【等待】当前还有一轮没结束，我不会混入下一轮。"]
        if not bool(state.get("ready_for_next_round", 1)):
            return ["【阻塞】上一轮没有被明确完成，所以我不会自动开启下一轮。"]

        if not bool(state.get("cold_start_done", 0)):
            question = self._cold_start_question()
            resolution_mode = self.MODE_ASK_USER
            task = self.memory.create_task(
                round_type=self.ROUND_WORLD,
                task_kind=self.TASK_WORLD_ASK,
                question_text=question,
                notes="cold_start",
                metadata={"cold_start": True, "resolution_mode": resolution_mode},
            )
            self.memory.add_message("assistant", question, str(task["task_id"]))
            self._apply_question_cost()
            self.memory.update_state(cold_start_done=1, ready_for_next_round=0, last_round_outcome="")
            self.memory.rebuild_memory_markdown()
            return [question, self._resolution_notice(resolution_mode)]

        next_round = str(state.get("next_round_type") or self.ROUND_WORLD)
        if next_round == self.ROUND_ABSTRACT:
            question, reason, resolution_mode = self._plan_abstract_round()
            task_kind = self.TASK_ABSTRACT_SELF if resolution_mode == self.MODE_SELF_RESOLVE else self.TASK_ABSTRACT_ASK
            task = self.memory.create_task(
                round_type=self.ROUND_ABSTRACT,
                task_kind=task_kind,
                question_text=question,
                notes=reason,
                metadata={"plan_reason": reason, "resolution_mode": resolution_mode},
                status=self.memory.STATUS_WAITING,
            )
            self.memory.add_message("assistant", question, str(task["task_id"]))
            self._apply_question_cost()
            self.memory.update_state(ready_for_next_round=0)
            self.memory.rebuild_memory_markdown()
            return [question, self._resolution_notice(resolution_mode)]

        spoken_question, reason, source_question_id, candidate, resolution_mode = self._plan_world_round()
        task_kind = self.TASK_WORLD_SELF if resolution_mode == self.MODE_SELF_RESOLVE else self.TASK_WORLD_ASK
        task = self.memory.create_task(
            round_type=self.ROUND_WORLD,
            task_kind=task_kind,
            question_text=spoken_question,
            source_question_id=source_question_id,
            source_path=str(candidate.get("path") or ""),
            notes=reason,
            metadata={"candidate": candidate, "plan_reason": reason, "resolution_mode": resolution_mode},
            status=self.memory.STATUS_WAITING,
        )
        if source_question_id:
            self.memory.mark_question_asked(source_question_id)
        self.memory.add_message("assistant", spoken_question, str(task["task_id"]))
        self._apply_question_cost()
        self.memory.update_state(ready_for_next_round=0)
        self.memory.rebuild_memory_markdown()
        return [spoken_question, self._resolution_notice(resolution_mode)]

    def execute_command(self, command: str) -> list[str] | None:
        raw = str(command or "").strip()
        if not raw.startswith("/"):
            return None
        parts = raw.split()
        cmd = parts[0].lower()
        arg = " ".join(parts[1:]).strip()
        if cmd in {"/quit", "/exit"}:
            raise SystemExit(0)
        if cmd in {"/help", "/h"}:
            return [
                textwrap.dedent(
                    """
                    Commands
                    /help            显示帮助
                    /tool            显示当前可用工具
                    /judge           打印 JUDGE.md
                    /step            手动推进下一轮
                    /carryout        让 Volition 自己执行当前这一轮
                    /dashboard       显示完整 dashboard
                    /watch_dashboard 同 /dashboard
                    /status          显示简略状态
                    /context         打印 CONTEXT.md
                    /memory          打印 MEMORY.md
                    /soul            打印 SOUL.md
                    /world           打印 WORLD.md
                    /tasks           最近任务
                    /reset           清空记忆并重置 Volition
                    /auto on|off     开关自动连续推进
                    /quit            退出
                    """.strip()
                )
            ]
        if cmd in {"/tool", "/tools"}:
            return [self._tool_list_text()]
        if cmd in {"/dashboard", "/watch_dashboard"}:
            return [self.dashboard_text()]
        if cmd in {"/status", "/watch_status"}:
            return [self.status_text()]
        if cmd == "/context":
            return [self.memory.read_context().strip()]
        if cmd == "/judge":
            return [self.memory.read_judge().strip()]
        if cmd in {"/memory", "/memory_status"}:
            return [self.memory.read_memory().strip()]
        if cmd == "/soul":
            return [self.memory.read_soul().strip()]
        if cmd == "/world":
            return [self.memory.read_world().strip()]
        if cmd == "/tasks":
            tasks = self.memory.recent_tasks(limit=8)
            if not tasks:
                return ["暂无任务历史。"]
            lines = ["Recent Tasks"]
            for task in tasks:
                lines.append(f"- {format_ts(task.get('updated_at'))} | {task.get('round_type')} | {task.get('status')} | {truncate(task.get('question_text', ''), 88)}")
            return ["\n".join(lines)]
        if cmd in {"/step", "/watch_step"}:
            return self.start_next_round()
        if cmd == "/carryout":
            task = self.memory.active_task()
            if not task:
                return ["【待机】当前没有活动任务；输入 /step 开始下一轮。"]
            task_meta = dict(task.get("metadata") or {})
            task_meta["resolution_mode"] = self.MODE_SELF_RESOLVE
            task_kind = self.TASK_ABSTRACT_SELF if str(task.get("round_type") or self.ROUND_WORLD) == self.ROUND_ABSTRACT else self.TASK_WORLD_SELF
            self.memory.update_task(str(task["task_id"]), task_kind=task_kind, status=self.memory.STATUS_RUNNING, metadata=task_meta)
            latest_task = self.memory.get_task(str(task["task_id"])) or task
            replies = self._run_self_resolve_task(latest_task)
            self.memory.rebuild_memory_markdown()
            state = self.memory.state()
            if bool(state.get("alive", 1)) and bool(state.get("auto_mode", 1)) and bool(self.config.agent.auto_continue):
                if not self.memory.active_task() and bool(state.get("ready_for_next_round", 0)):
                    replies.extend(self.start_next_round())
            return replies
        if cmd in {"/reset", "/memory_reset", "/explore_reset"}:
            self.memory.reset()
            replies = ["【重置】记忆、SOUL/MEMORY、notebooks 和任务都已重置；WORLD.md 会保持不变。"]
            if bool(self.config.agent.auto_start):
                replies.extend(self.start_next_round())
            return replies
        if cmd == "/auto":
            on = arg.lower() not in {"off", "0", "false"}
            self.memory.update_state(auto_mode=1 if on else 0)
            self.memory.rebuild_memory_markdown()
            return [f"自动连续推进已{'开启' if on else '关闭'}。"]
        return [f"未知命令：{raw}"]
    def _extract_memory(self, task: dict[str, Any], completion_summary: str) -> None:
        task_id = str(task.get("task_id") or "")
        transcript = self.memory.recent_messages(limit=20, task_id=task_id)
        try:
            data = self.llm.chat_json(
                self._system_prompt(),
                f"""
当前任务：{json.dumps({
    "round_type": task.get("round_type"),
    "question": task.get("question_text"),
    "result_summary": completion_summary,
}, ensure_ascii=False, indent=2)}

这一轮的消息记录：{json.dumps(transcript, ensure_ascii=False, indent=2)}

请抽取需要进入 notebook memory 与 world graph 的结构化内容。

必须遵守：
- 这里负责的是 notebook memory 与 world graph，不是重写 MEMORY.md 或 SOUL.md。
- 优先抽取实体、事实、开放问题、节点摘要这些结构化信息。
- 不要输出解释，不要输出 markdown。

只输出 JSON：
{{
  "entities": [
    {{"name": "实体名", "entity_type": "person|concept|item|topic|self|unknown", "summary": "简短摘要"}}
  ],
  "facts": [
    {{"entity_name": "实体名", "entity_type": "person", "category": "分类", "value": "事实内容", "confidence": 0.8}}
  ],
  "world_questions": [
    {{"path": "世界路径 > ...", "question": "新的开放问题", "priority": 1.0, "node_type": "topic", "node_summary": "节点摘要", "entity_name": "相关实体", "entity_type": "concept"}}
  ]
}}
""",
                temperature=0.2,
                max_tokens=900,
            )
        except Exception:
            self._fallback_extract_memory(task, completion_summary)
            return
        merged_entities: dict[tuple[str, str], dict[str, str]] = {}
        for entity in data.get("entities", []) or []:
            name = str(entity.get("name") or "").strip()
            entity_type = str(entity.get("entity_type") or "unknown").strip().lower()
            if not name:
                continue
            dedupe_key = (entity_type, name.casefold())
            current = merged_entities.get(dedupe_key)
            summary = str(entity.get("summary") or "").strip()
            if current is None:
                merged_entities[dedupe_key] = {
                    "name": name,
                    "entity_type": entity_type or "unknown",
                    "summary": summary,
                }
                continue
            if current["name"].islower() and not name.islower():
                current["name"] = name
            if len(summary) > len(current["summary"]):
                current["summary"] = summary
        for entity in merged_entities.values():
            self.memory.upsert_entity(entity["name"], entity["entity_type"], entity["summary"])
        for fact in data.get("facts", []) or []:
            self.memory.add_fact(
                str(fact.get("entity_name") or ""),
                str(fact.get("entity_type") or "unknown"),
                str(fact.get("category") or "fact"),
                str(fact.get("value") or ""),
                float(fact.get("confidence") or 0.7),
            )
        for question in data.get("world_questions", []) or []:
            self.memory.upsert_world_question(
                str(question.get("path") or "未知世界 > 未命名节点"),
                str(question.get("question") or ""),
                priority=float(question.get("priority") or 1.0),
                node_type=str(question.get("node_type") or "topic"),
                node_summary=str(question.get("node_summary") or ""),
                entity_name=str(question.get("entity_name") or ""),
                entity_type=str(question.get("entity_type") or ""),
            )
        self.memory.rebuild_all_files()

    def _ask_task_kind(self, round_type: str) -> str:
        return self.TASK_ABSTRACT_ASK if str(round_type or self.ROUND_WORLD) == self.ROUND_ABSTRACT else self.TASK_WORLD_ASK

    def _self_task_kind(self, round_type: str) -> str:
        return self.TASK_ABSTRACT_SELF if str(round_type or self.ROUND_WORLD) == self.ROUND_ABSTRACT else self.TASK_WORLD_SELF

    def _resolution_notice(self, resolution_mode: str) -> str:
        if resolution_mode == self.MODE_SELF_RESOLVE:
            return "【Volition自己解答】我判断这题可以由我自己结合当前记忆和工具完成。等你输入 /carryout 之后，我才会真正开始执行；如果你想把它切回人类回答，请输入 /intervene 后接你的观点。"
        return "【人类回答】这题需要你来回答。你可以直接回复；如果这次还没有把问题答完，我会继续围绕同一题追问，直到它被解决。"

    def _task_completion_eval(self, task: dict[str, Any], user_text: str) -> dict[str, Any]:
        if str(task.get("task_kind") or "") in {self.TASK_WORLD_ASK, self.TASK_ABSTRACT_ASK} and self._looks_like_substantive_answer(user_text):
            return {
                "action": "resolved",
                "summary": f"用户已经对“{task.get('question_text') or '当前问题'}”给出了正面回答。",
                "memory_note": user_text,
                "follow_up": "",
            }
        try:
            data = self.llm.chat_json(
                self._system_prompt(),
                f"""
当前任务：{json.dumps({
    "round_type": task.get("round_type"),
    "task_kind": task.get("task_kind"),
    "question_text": task.get("question_text"),
    "notes": task.get("notes"),
}, ensure_ascii=False, indent=2)}

用户刚刚说：
{user_text}

这是【人类回答】控制流。你只需要判断：这次回复是否已经把当前问题回答到可以结束这一轮。
- 如果已经形成了足够明确的回答，输出 resolved。
- 如果还不够，输出 continue，并给出下一句应该继续追问的内容。
- 不要把这次回复解释成“让 Volition 自己去做”或“放弃这个方向”；这些控制流由程序命令处理，不由这里判断。

只输出 JSON：{{
  "action": "resolved" | "continue",
  "summary": "对这次判断的简短总结",
  "memory_note": "如果 resolved，这次回答里值得写入 memory 的内容",
  "follow_up": "如果 continue，需要继续追问什么"
}}
""",
                temperature=0.2,
                max_tokens=400,
            )
        except Exception:
            return self._fallback_task_completion_eval(task, user_text)
        action = str(data.get("action") or "").strip().lower()
        if action not in {"resolved", "continue"}:
            return self._fallback_task_completion_eval(task, user_text)
        return {
            "action": action,
            "summary": str(data.get("summary") or truncate(user_text, 120)).strip(),
            "memory_note": str(data.get("memory_note") or "").strip(),
            "follow_up": str(data.get("follow_up") or "").strip(),
        }

    def _tool_trace_line(self, tool_name: str, args: dict[str, Any], result: Any) -> str:
        arg_preview = truncate(json.dumps(args or {}, ensure_ascii=False), 120)
        if isinstance(result, dict):
            if result.get("error"):
                outcome = f"error={truncate(str(result.get('error') or ''), 100)}"
            elif "command" in result:
                outcome = f"exit_code={result.get('exit_code')}"
            elif "path" in result:
                outcome = f"path={truncate(str(result.get('path') or ''), 80)}"
            elif "stdout" in result:
                outcome = f"stdout_chars={len(str(result.get('stdout') or ''))}"
            elif "content" in result:
                outcome = f"content_chars={len(str(result.get('content') or ''))}"
            elif "ok" in result:
                outcome = f"ok={bool(result.get('ok'))}"
            else:
                outcome = f"keys={','.join(sorted(result.keys())[:4])}"
        elif isinstance(result, list):
            outcome = f"items={len(result)}"
        else:
            outcome = truncate(str(result), 100)
        return f"【工具】{tool_name} | args={arg_preview} | {outcome}"

    def _judge_feedback_flags(self, transcript: list[dict[str, Any]]) -> list[str]:
        flags: list[str] = []
        user_texts = [str(item.get("text") or "") for item in transcript if str(item.get("role") or "") == "user"]
        combined = "\n".join(user_texts).lower()
        repeated_markers = [
            "重复",
            "又问",
            "问过",
            "之前问过",
            "已经问过",
            "刚刚问过",
            "same question",
            "asked this before",
            "already asked",
        ]
        low_info_markers = [
            "没有新意",
            "没新意",
            "没意义",
            "没有意义",
            "没信息量",
            "没有信息量",
            "别再问了",
            "太烦",
            "无聊",
        ]
        if any(marker in combined for marker in repeated_markers):
            flags.append("repeated_question")
        if any(marker in combined for marker in low_info_markers):
            flags.append("low_information_or_annoying")
        return flags

    def _feedback_floor_score(self, flags: list[str]) -> int | None:
        if "repeated_question" in flags and "low_information_or_annoying" in flags:
            return -15
        if "repeated_question" in flags:
            return -10
        if "low_information_or_annoying" in flags:
            return -8
        return None

    def _self_resolve(self, task: dict[str, Any]) -> tuple[str, str, list[str]]:
        context_name = self.memory.context_md_path.name
        question = str(task.get("question_text") or "")
        tool_logs: list[str] = []

        def on_tool_call(tool_name: str, args: dict[str, Any], result: Any) -> None:
            tool_logs.append(self._tool_trace_line(tool_name, args, result))

        try:
            data = self._json_with_tools(
                f"""
{self._hunger_prompt()}

{self._recent_task_context()}

当前任务：{question}

这道题已经被判定为由你自己完成。你可以自己决定是否调用工具。
- 如果当前 {context_name} 已经足够，就优先依据它和 notebook 完成。
- 只有在内部记忆无法回答、且确实需要外部信息时，才调用 web_search。
- 如果需要读取本地文件或执行终端命令，也可以调用对应工具。

完成后只输出 JSON：{{
  "summary": "你最终对这个问题的回答",
  "memory_note": "其中值得写入长期记忆的内容"
}}
""",
                allowed_tools=["search_notebooks", "read_notebook", "read_local_file", "run_terminal_command", "web_search"],
                temperature=0.4,
                max_tokens=700,
                on_tool_call=on_tool_call,
            )
        except Exception:
            summary = f"我尝试自己完成这题，但当前工具链没有稳定产出结构化结果：{question}"
            return summary, summary, tool_logs
        summary = str(data.get("summary") or "").strip()
        memory_note = str(data.get("memory_note") or "").strip()
        if not summary:
            summary = f"我尝试自己完成这题，但暂时只形成了一个不完整的结果：{question}"
            return summary, summary, tool_logs
        return summary, memory_note or summary, tool_logs

    def _score_recovery(self, task: dict[str, Any], completion_summary: str) -> tuple[int, str]:
        judge_name = self.memory.judge_md_path.name
        judge_text = self._read_judge().strip()
        task_id = str(task.get("task_id") or "")
        transcript = self.memory.recent_messages(limit=20, task_id=task_id)
        candidate = dict((task.get("metadata") or {}).get("candidate") or {})
        historical_ask_count = int(candidate.get("ask_count") or 0)
        feedback_flags = self._judge_feedback_flags(transcript)
        feedback_floor = self._feedback_floor_score(feedback_flags)
        try:
            data = self.llm.chat_json(
                self._system_prompt(),
                f"""
{self._hunger_prompt()}

[{judge_name}]
{judge_text}

当前问题：{task.get("question_text") or ""}
完成摘要：{completion_summary}
该问题在进入本轮之前的历史提问次数：{historical_ask_count}
程序捕获到的用户反馈标签：{json.dumps(feedback_flags, ensure_ascii=False)}

这一轮的完整消息记录：
{json.dumps(transcript, ensure_ascii=False, indent=2)}

注意：
- 这道题在被提出时，能源值已经先行扣减过一次了。
- 你现在扮演这个世界内部的 Judge LLM。
- 请严格依据 {judge_name} 里的规则完成判定。
- 除了参考 {judge_name}，你还必须把“用户在本轮回答里对这个问题本身的评价”当作直接证据。
- 如果用户明确指出这题是重复提问、已经问过、没有新信息、令人厌烦，你应该显著下调，必要时直接给负能源值。
- 如果用户的回答表明这题虽然被问了，但确实推动了新的认知，你可以解释为什么没有给出负值。
- 你现在要输出的是这道题完成后的“能源变化量”，它可以是正数、0 或负数。

只输出 JSON：{{
  "recovery_score": 0,
  "value_reason": "价值表为什么这么判",
  "energy_reason": "为什么最终给出这个正负能源变化量"
}}
""",
                temperature=0.2,
                max_tokens=520,
            )
        except Exception:
            score, reason = self._fallback_recovery(task, completion_summary)
            if feedback_floor is not None and score > feedback_floor:
                return feedback_floor, f"{reason}；程序从用户反馈中检测到负面信号：{', '.join(feedback_flags)}，因此将本轮能源变化量下调到 {feedback_floor}。"
            return score, reason
        raw_score = float(data.get("recovery_score") or 0)
        score = int(round(raw_score))
        value_reason = str(data.get("value_reason") or "").strip()
        energy_reason = str(data.get("energy_reason") or data.get("satiety_reason") or "").strip()
        if feedback_floor is not None and score > feedback_floor:
            score = feedback_floor
            energy_reason = (energy_reason + "；" if energy_reason else "") + f"程序从用户反馈中检测到 {', '.join(feedback_flags)}，因此将能源变化量至少下调到 {feedback_floor}。"
        reason_parts: list[str] = []
        if value_reason:
            reason_parts.append(f"价值表：{value_reason}")
        if energy_reason:
            reason_parts.append(f"能源表：{energy_reason}")
        if not reason_parts:
            reason_parts.append("世界已经完成了对这轮问题的 Judge 判定。")
        return score, "；".join(reason_parts)

    def _run_self_resolve_task(self, task: dict[str, Any]) -> list[str]:
        summary, memory_note, tool_logs = self._self_resolve(task)
        self.memory.add_message("assistant", summary, str(task["task_id"]))
        if task.get("source_question_id"):
            self.memory.resolve_question(str(task.get("source_question_id") or ""), memory_note or summary)
        replies: list[str] = []
        replies.extend(tool_logs)
        replies.append(summary)
        replies.extend(self._complete_task(task, summary, memory_note))
        return replies

    def startup_messages(self) -> list[str]:
        state = self.memory.state()
        active_task = self.memory.active_task()
        if not bool(state.get("alive", 1)):
            return ["【停机】我的能源值已经归零，这个实例不会继续运行。"]
        if active_task:
            task_kind = str(active_task.get("task_kind") or "")
            if task_kind in {self.TASK_WORLD_SELF, self.TASK_ABSTRACT_SELF}:
                resume_hint = "这题目前被标记为可由我自己完成；如果你同意我现在开始执行，请输入 /carryout；如果你想把它切回人类回答，请输入 /intervene 后接你的观点；如果想从零开始，请输入 /reset。"
            else:
                resume_hint = "你现在可以直接文字回复这道题；如果这次还没答完，我会继续追问；如果想从零开始，请输入 /reset。"
            return [
                "\n".join(
                    [
                        "【恢复】检测到上一次会话还有一轮没结束：",
                        f"- round={active_task.get('round_type')}",
                        f"- status={active_task.get('status')}",
                        f"- kind={task_kind}",
                        f"- question={active_task.get('question_text')}",
                        resume_hint,
                    ]
                )
            ]
        if bool(self.config.agent.auto_start):
            return self.start_next_round()
        return ["【待机】当前没有活动任务。输入 /step 开始下一轮。"]

    def _apply_intervention(self, task: dict[str, Any], viewpoint: str) -> list[str]:
        clean_viewpoint = str(viewpoint or "").strip()
        if not clean_viewpoint:
            return ["【介入】请输入 /intervene 后接你的观点，例如：/intervene 我认为这个问题更需要人类主观回答。"]
        task_id = str(task["task_id"])
        task_meta = dict(task.get("metadata") or {})
        task_meta["resolution_mode"] = self.MODE_ASK_USER
        self.memory.update_task(
            task_id,
            task_kind=self._ask_task_kind(str(task.get("round_type") or self.ROUND_WORLD)),
            status=self.memory.STATUS_WAITING,
            notes=f"人类介入：{clean_viewpoint}",
            metadata=task_meta,
        )
        self.memory.add_message("user", clean_viewpoint, task_id)
        latest_task = self.memory.get_task(task_id) or task
        decision = self._task_completion_eval(latest_task, clean_viewpoint)
        action = str(decision.get("action") or "continue").strip().lower()
        prefix = "【人类回答】这题现在改由你来回答。"
        if action == "continue":
            follow_up = str(decision.get("follow_up") or "").strip()
            if not follow_up:
                follow_up = f"我还没从你的观点里拿到这题的明确结论：{latest_task.get('question_text') or ''}"
            self.memory.update_task(task_id, notes=follow_up, status=self.memory.STATUS_WAITING)
            self.memory.add_message("assistant", follow_up, task_id)
            self.memory.rebuild_memory_markdown()
            return [prefix, f"【继续】{follow_up}"]
        if latest_task.get("source_question_id"):
            self.memory.resolve_question(str(latest_task.get("source_question_id") or ""), clean_viewpoint)
        completion_summary = str(decision.get("summary") or clean_viewpoint).strip() or clean_viewpoint
        memory_note = str(decision.get("memory_note") or clean_viewpoint).strip() or clean_viewpoint
        replies = [prefix]
        replies.extend(self._complete_task(latest_task, completion_summary, memory_note))
        self.memory.rebuild_memory_markdown()
        state = self.memory.state()
        if bool(state.get("alive", 1)) and bool(state.get("auto_mode", 1)) and bool(self.config.agent.auto_continue):
            if not self.memory.active_task() and bool(state.get("ready_for_next_round", 0)):
                replies.extend(self.start_next_round())
        return replies

    def handle_user_text(self, text: str) -> list[str]:
        clean = str(text or "").strip()
        if not clean:
            return []
        task = self.memory.active_task()
        self.memory.add_message("user", clean, str(task.get("task_id") if task else ""))
        if not task:
            self.memory.rebuild_memory_markdown()
            return ["【收到】我先记下来了。当前没有活动任务；如果你想让我继续，请输入 /step。"]

        task_kind = str(task.get("task_kind") or "")
        if task_kind in {self.TASK_WORLD_SELF, self.TASK_ABSTRACT_SELF}:
            reminder = "【等待执行】这题当前被标记为由我自己完成。等你输入 /carryout 后，我才会真正调用工具并执行；如果你想把它改回人类回答，请输入 /intervene 后接你的观点。"
            self.memory.update_task(str(task["task_id"]), notes=reminder)
            return [reminder]

        decision = self._task_completion_eval(task, clean)
        action = str(decision.get("action") or "continue").strip().lower()

        if action == "continue":
            follow_up = str(decision.get("follow_up") or "").strip()
            if not follow_up:
                follow_up = f"我还没从你的回答里拿到这题的明确结论：{task.get('question_text') or ''}"
            self.memory.update_task(str(task["task_id"]), notes=follow_up, status=self.memory.STATUS_WAITING)
            self.memory.add_message("assistant", follow_up, str(task["task_id"]))
            self.memory.rebuild_memory_markdown()
            return [f"【继续】{follow_up}"]

        if task.get("source_question_id"):
            self.memory.resolve_question(str(task.get("source_question_id") or ""), clean)
        completion_summary = str(decision.get("summary") or clean).strip() or clean
        memory_note = str(decision.get("memory_note") or clean).strip() or clean
        replies = self._complete_task(task, completion_summary, memory_note)

        self.memory.rebuild_memory_markdown()
        state = self.memory.state()
        if bool(state.get("alive", 1)) and bool(state.get("auto_mode", 1)) and bool(self.config.agent.auto_continue):
            if not self.memory.active_task() and bool(state.get("ready_for_next_round", 0)):
                replies.extend(self.start_next_round())
        return replies

    def execute_command(self, command: str) -> list[str] | None:
        raw = str(command or "").strip()
        if not raw.startswith("/"):
            return None
        parts = raw.split()
        cmd = parts[0].lower()
        arg = raw[len(parts[0]):].strip() if parts else ""
        if cmd in {"/quit", "/exit"}:
            raise SystemExit(0)
        if cmd in {"/help", "/h"}:
            return [
                textwrap.dedent(
                    """
                    Commands
                    /help                 显示帮助
                    /tool                 显示当前可用工具
                    /judge                打印 JUDGE.md
                    /step                 手动推进下一轮
                    /carryout             让 Volition 执行当前“自己解答”这一轮
                    /intervene <观点>     把当前“自己解答”这一轮切回人类回答
                    /dashboard            显示完整 dashboard
                    /watch_dashboard      同 /dashboard
                    /status               显示简略状态
                    /context              打印 CONTEXT.md
                    /memory               打印 MEMORY.md
                    /soul                 打印 SOUL.md
                    /world                打印 WORLD.md
                    /tasks                最近任务
                    /reset                清空记忆并重置 Volition
                    /auto on|off          开关自动连续推进
                    /quit                 退出
                    """.strip()
                )
            ]
        if cmd in {"/tool", "/tools"}:
            return [self._tool_list_text()]
        if cmd in {"/dashboard", "/watch_dashboard"}:
            return [self.dashboard_text()]
        if cmd in {"/status", "/watch_status"}:
            return [self.status_text()]
        if cmd == "/context":
            return [self.memory.read_context().strip()]
        if cmd == "/judge":
            return [self.memory.read_judge().strip()]
        if cmd in {"/memory", "/memory_status"}:
            return [self.memory.read_memory().strip()]
        if cmd == "/soul":
            return [self.memory.read_soul().strip()]
        if cmd == "/world":
            return [self.memory.read_world().strip()]
        if cmd == "/tasks":
            tasks = self.memory.recent_tasks(limit=8)
            if not tasks:
                return ["暂无任务历史。"]
            lines = ["Recent Tasks"]
            for task in tasks:
                lines.append(f"- {format_ts(task.get('updated_at'))} | {task.get('round_type')} | {task.get('status')} | {truncate(task.get('question_text', ''), 88)}")
            return ["\n".join(lines)]
        if cmd in {"/step", "/watch_step"}:
            return self.start_next_round()
        if cmd == "/carryout":
            task = self.memory.active_task()
            if not task:
                return ["【待机】当前没有活动任务；输入 /step 开始下一轮。"]
            if str(task.get("task_kind") or "") not in {self.TASK_WORLD_SELF, self.TASK_ABSTRACT_SELF}:
                return ["【人类回答】当前这一轮需要你来回答，不是由我自己执行。你可以直接回复；如果还没答完，我会继续追问。"]
            task_meta = dict(task.get("metadata") or {})
            task_meta["resolution_mode"] = self.MODE_SELF_RESOLVE
            self.memory.update_task(
                str(task["task_id"]),
                task_kind=self._self_task_kind(str(task.get("round_type") or self.ROUND_WORLD)),
                status=self.memory.STATUS_RUNNING,
                metadata=task_meta,
            )
            latest_task = self.memory.get_task(str(task["task_id"])) or task
            replies = self._run_self_resolve_task(latest_task)
            self.memory.rebuild_memory_markdown()
            state = self.memory.state()
            if bool(state.get("alive", 1)) and bool(state.get("auto_mode", 1)) and bool(self.config.agent.auto_continue):
                if not self.memory.active_task() and bool(state.get("ready_for_next_round", 0)):
                    replies.extend(self.start_next_round())
            return replies
        if cmd == "/intervene":
            task = self.memory.active_task()
            if not task:
                return ["【介入】当前没有活动任务；输入 /step 开始下一轮。"]
            if str(task.get("task_kind") or "") not in {self.TASK_WORLD_SELF, self.TASK_ABSTRACT_SELF}:
                return ["【介入】当前这一轮已经在人类回答控制流里了，你可以直接回复你的观点。"]
            return self._apply_intervention(task, arg)
        if cmd in {"/reset", "/memory_reset", "/explore_reset"}:
            self.memory.reset()
            replies = ["【重置】记忆、SOUL/MEMORY、notebooks 和任务都已重置；WORLD.md 会保持不变。"]
            if bool(self.config.agent.auto_start):
                replies.extend(self.start_next_round())
            return replies
        if cmd == "/auto":
            on = arg.lower() not in {"off", "0", "false"}
            self.memory.update_state(auto_mode=1 if on else 0)
            self.memory.rebuild_memory_markdown()
            return [f"自动连续推进已{'开启' if on else '关闭'}。"]
        return [f"未知命令：{raw}"]
