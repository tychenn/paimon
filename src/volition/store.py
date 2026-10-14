"""Transactional experience log and recoverable runtime state."""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from volition.contracts import Decision, GoalChange, VolitionError


def now() -> str:
    return datetime.now(UTC).isoformat()


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(directory / "volition.sqlite3", timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise VolitionError(f"数据库版本 {version} 超出此版本支持范围。")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS state (
                id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                kind TEXT NOT NULL, operation_id TEXT, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, stage TEXT NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL,
                reply_to TEXT UNIQUE REFERENCES messages(id), event_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS goals (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
                reason TEXT NOT NULL, source TEXT NOT NULL, event_id INTEGER NOT NULL
            );
            PRAGMA user_version=1;
            """
        )
        self.db.execute(
            "INSERT OR IGNORE INTO state VALUES (1, ?)",
            (
                encode(
                    {
                        "phase": "RUNNING",
                        "pending_message_id": None,
                        "feedback_id": None,
                        "active_operation": None,
                        "last_error": None,
                        "next_tick_at": 0,
                    }
                ),
            ),
        )
        (directory / "volition.sqlite3").chmod(0o600)
        self.sync_goal_document()

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def state(self) -> dict:
        return json.loads(self.db.execute("SELECT data FROM state WHERE id=1").fetchone()[0])

    def _state(self, state: dict):
        self.db.execute("UPDATE state SET data=? WHERE id=1", (encode(state),))

    def _event(self, kind: str, payload: dict, operation_id: str | None = None) -> int:
        cursor = self.db.execute(
            "INSERT INTO events(at, kind, operation_id, payload) VALUES (?, ?, ?, ?)",
            (now(), kind, operation_id, encode(payload)),
        )
        return cursor.lastrowid

    def event(self, kind: str, payload: dict, operation_id: str | None = None) -> int:
        with self.transaction():
            return self._event(kind, payload, operation_id)

    def events(self, limit: int = 20) -> list[dict]:
        rows = self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in reversed(rows)]

    def export(self, path: Path):
        # Stream the log rather than limiting exports to the model context window.
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            for row in self.db.execute("SELECT * FROM events ORDER BY id"):
                stream.write(encode({**dict(row), "payload": json.loads(row["payload"])}) + "\n")

    def message(self, message_id: str | None) -> dict | None:
        row = self.db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return dict(row) if row else None

    def goals(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM goals ORDER BY event_id")]

    def sync_goal_document(self):
        """Readable derived file; SQLite and its event log remain authoritative."""
        lines = ["# Volition 长期目标", "", "由经历数据库生成；使用 goals 命令或模型决策更新。", ""]
        for goal in self.goals():
            lines.extend(
                [
                    f"## {goal['title']}",
                    "",
                    f"- ID：{goal['id']}",
                    f"- 状态：{goal['status']}",
                    f"- 来源：{goal['source']}",
                    f"- 来源事件：{goal['event_id']}",
                    f"- 理由：{goal['reason']}",
                    "",
                ]
            )
        temporary = self.directory / f".goals-{uuid4()}.tmp"
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write("\n".join(lines))
            os.replace(temporary, self.directory / "long_term_goals.md")
        except OSError:
            # A failed derived export must not turn a committed action into an ambiguous one.
            self.event("artifact.write_failed", {"artifact": "long_term_goals.md"})
        finally:
            temporary.unlink(missing_ok=True)

    def _goal(self, change: GoalChange, source: str, operation_id: str | None) -> str:
        goal_id = change.id or str(uuid4())
        self.validate_goal(change)
        event_id = self._event(
            "goal.updated", {**change.model_dump(), "id": goal_id, "source": source}, operation_id
        )
        self.db.execute(
            "INSERT INTO goals VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "title=excluded.title, status=excluded.status, reason=excluded.reason, "
            "source=excluded.source, event_id=excluded.event_id",
            (goal_id, change.title, change.status, change.reason, source, event_id),
        )
        return goal_id

    def validate_goal(self, change: GoalChange | None):
        if (
            change
            and change.id
            and not self.db.execute("SELECT 1 FROM goals WHERE id=?", (change.id,)).fetchone()
        ):
            raise VolitionError("目标不存在；新目标应使用空 id。")

    def add_goal(self, title: str, reason: str = "用户提出") -> str:
        change = GoalChange(title=title, reason=reason)
        with self.transaction():
            goal_id = self._goal(change, "user", None)
        self.sync_goal_document()
        return goal_id

    def begin(self) -> str:
        with self.transaction():
            state = self.state()
            if state["phase"] != "RUNNING" or state["active_operation"]:
                raise VolitionError("当前状态不能开始新的自主活动。")
            operation_id = str(uuid4())
            self.db.execute(
                "INSERT INTO operations VALUES (?, 'observing', 'started', ?, NULL)",
                (operation_id, now()),
            )
            state["active_operation"] = operation_id
            self._state(state)
            self._event("turn.started", {}, operation_id)
            return operation_id

    def stage(self, operation_id: str, stage: str, kind: str, payload: dict):
        with self.transaction():
            state = self.state()
            if state["active_operation"] != operation_id or state["phase"] != "RUNNING":
                raise VolitionError("运行状态已变化，停止本轮操作。")
            self.db.execute("UPDATE operations SET stage=? WHERE id=?", (stage, operation_id))
            self._event(kind, payload, operation_id)

    def finish(self, operation_id: str, decision: Decision, result: dict | None = None):
        with self.transaction():
            state = self.state()
            if state["active_operation"] != operation_id or state["phase"] != "RUNNING":
                raise VolitionError("运行状态已变化，无法提交本轮结果。")
            if decision.goal:
                self._goal(decision.goal, "model", operation_id)
            if decision.memory_note:
                self._event(
                    "memory.noted", {"text": decision.memory_note, "source": "model"}, operation_id
                )
            if decision.kind == "speak":
                message_id = str(uuid4())
                event_id = self._event(
                    "message.sent", {"id": message_id, "text": decision.message}, operation_id
                )
                self.db.execute(
                    "INSERT INTO messages VALUES (?, 'agent', ?, NULL, ?)",
                    (message_id, decision.message, event_id),
                )
                state.update(phase="AWAITING_FEEDBACK", pending_message_id=message_id)
            elif decision.kind == "act":
                self._event("action.result", result or {}, operation_id)
            else:
                state["next_tick_at"] = time.time() + decision.wait_seconds
            self.db.execute(
                "UPDATE operations SET status='completed', finished_at=? WHERE id=?",
                (now(), operation_id),
            )
            state.update(active_operation=None, last_error=None)
            self._state(state)
            self._event("turn.completed", {"phase": state["phase"]}, operation_id)
        if decision.goal:
            self.sync_goal_document()

    def reply(self, message_id: str, text: str) -> str:
        if not text.strip():
            raise VolitionError("回复不能为空。")
        with self.transaction():
            state = self.state()
            if state["phase"] != "AWAITING_FEEDBACK" or state["pending_message_id"] != message_id:
                raise VolitionError("这条消息已回复，或与当前等待的消息不匹配。")
            feedback_id = str(uuid4())
            event_id = self._event(
                "feedback.received",
                {"id": feedback_id, "reply_to": message_id, "text": text, "source": "user"},
            )
            self.db.execute(
                "INSERT INTO messages VALUES (?, 'user', ?, ?, ?)",
                (feedback_id, text, message_id, event_id),
            )
            state.update(phase="PROCESSING_FEEDBACK", feedback_id=feedback_id)
            self._state(state)
            return feedback_id

    def process_feedback(self):
        with self.transaction():
            state = self.state()
            if state["phase"] != "PROCESSING_FEEDBACK":
                raise VolitionError("当前没有待处理的反馈。")
            self._event(
                "feedback.available",
                {"id": state["feedback_id"], "meaning": "raw feedback added to context"},
            )
            state.update(phase="RUNNING", pending_message_id=None, feedback_id=None, next_tick_at=0)
            self._state(state)

    def fail(self, operation_id: str, reason: str):
        with self.transaction():
            state = self.state()
            row = self.db.execute(
                "SELECT stage FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
            self._event(
                "turn.failed",
                {
                    "reason": reason,
                    "stage": row[0] if row else "unknown",
                    "action_outcome": "unknown" if row and row[0] == "executing" else "not_started",
                },
                operation_id,
            )
            state.update(phase="PAUSED", last_error=reason)
            self._state(state)

    def recover(self) -> bool:
        state = self.state()
        if state["active_operation"] and state["phase"] != "PAUSED":
            self.fail(
                state["active_operation"], "上次执行中断；请检查经历记录，再使用 resume 恢复。"
            )
            return True
        return False

    def resume(self, note: str):
        with self.transaction():
            state = self.state()
            if state["phase"] != "PAUSED":
                raise VolitionError("只有暂停状态可以恢复；等待反馈时必须先回复。")
            if state["active_operation"]:
                self.db.execute(
                    "UPDATE operations SET status='interrupted', finished_at=? WHERE id=?",
                    (now(), state["active_operation"]),
                )
            self._event("runtime.resumed", {"note": note, "replay_action": False})
            state.update(phase="RUNNING", active_operation=None, last_error=None, next_tick_at=0)
            self._state(state)

    def context(self, limit: int) -> dict:
        # Keep raw records intact; bound only what is sent to the model.
        recent = []
        remaining = 14000
        for event in reversed(self.events(limit)):
            payload = encode(event["payload"])
            size = min(4000, remaining)
            if size <= 0:
                break
            recent.append({**event, "payload": payload[:size], "truncated": len(payload) > size})
            remaining -= min(len(payload), size)
        count = self.db.execute(
            "SELECT count(*) FROM events WHERE kind='model.decision'"
        ).fetchone()[0]
        # Memories and raw feedback remain available after routine events age out.
        memories = self.db.execute(
            "SELECT id, payload FROM events WHERE kind='memory.noted' ORDER BY id DESC LIMIT 5"
        ).fetchall()
        feedback = self.db.execute(
            "SELECT * FROM messages WHERE role='user' ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        latest_feedback = None
        if feedback:
            latest_feedback = dict(feedback)
            latest_feedback["content"] = feedback["content"][:6000]
            latest_feedback["truncated"] = len(feedback["content"]) > 6000
        goals = self.goals()
        active = [goal for goal in goals if goal["status"] == "active"]
        return {
            "goals": (active + [goal for goal in goals if goal["status"] != "active"])[:20],
            "goals_truncated": len(goals) > 20,
            "recent_events": list(reversed(recent)),
            "memories": [{"event_id": row["id"], **json.loads(row["payload"])} for row in memories],
            "latest_feedback": latest_feedback,
            "decision_count": count,
        }
