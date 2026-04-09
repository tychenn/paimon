from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .config import AppConfig
from .utils import clamp, format_ts, now_ts, safe_json_loads, slugify, stable_hash, truncate


class MemoryStore:
    ROUND_WORLD = "world"
    ROUND_ABSTRACT = "abstract"
    STATUS_WAITING = "waiting_user"
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_ABANDONED = "abandoned"

    def __init__(self, project_root: Path, config: AppConfig):
        self.project_root = project_root
        self.config = config
        self.data_dir = project_root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.notebooks_dir = self.data_dir / "notebooks"
        self.notebooks_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "memory.db"
        self.world_md_path = self.data_dir / "WORLD.md"
        self.judge_md_path = self.data_dir / "JUDGE.md"
        self.soul_md_path = self.data_dir / "SOUL.md"
        self.memory_md_path = self.data_dir / "MEMORY.md"
        self.context_md_path = self.data_dir / "CONTEXT.md"
        self._init_schema()
        self._ensure_state_row()
        self.ensure_world_markdown()
        self.ensure_judge_markdown()
        self.rebuild_all_files()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    hunger_value INTEGER NOT NULL DEFAULT 100,
                    low_value_streak INTEGER NOT NULL DEFAULT 0,
                    last_recovery_score INTEGER NOT NULL DEFAULT 0,
                    last_recovery_reason TEXT NOT NULL DEFAULT '',
                    next_round_type TEXT NOT NULL DEFAULT 'world',
                    ready_for_next_round INTEGER NOT NULL DEFAULT 1,
                    last_round_outcome TEXT NOT NULL DEFAULT '',
                    cold_start_done INTEGER NOT NULL DEFAULT 0,
                    alive INTEGER NOT NULL DEFAULT 1,
                    auto_mode INTEGER NOT NULL DEFAULT 1,
                    updated_at INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS agent_tasks (
                    task_id TEXT PRIMARY KEY,
                    round_type TEXT NOT NULL,
                    task_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    question_text TEXT NOT NULL,
                    source_question_id TEXT NOT NULL DEFAULT '',
                    source_path TEXT NOT NULL DEFAULT '',
                    result_summary TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    completed_at INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS entities (
                    entity_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    canonical_key TEXT NOT NULL UNIQUE,
                    value_score REAL NOT NULL DEFAULT 0.0,
                    value_reason TEXT NOT NULL DEFAULT '',
                    value_json TEXT NOT NULL DEFAULT '{}',
                    source_count INTEGER NOT NULL DEFAULT 0,
                    last_seen_at INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS facts (
                    fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    value TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.6,
                    last_seen_at INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS world_nodes (
                    node_id TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    node_type TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    entity_id TEXT NOT NULL DEFAULT '',
                    last_seen_at INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS world_questions (
                    question_id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL,
                    question_text TEXT NOT NULL,
                    normalized_question TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    priority REAL NOT NULL DEFAULT 1.0,
                    value_score REAL NOT NULL DEFAULT 0.0,
                    value_reason TEXT NOT NULL DEFAULT '',
                    ask_count INTEGER NOT NULL DEFAULT 0,
                    last_asked_at INTEGER NOT NULL DEFAULT 0,
                    last_seen_at INTEGER NOT NULL DEFAULT 0,
                    resolution_note TEXT NOT NULL DEFAULT '',
                    resolved_at INTEGER NOT NULL DEFAULT 0
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_world_questions_unique
                ON world_questions(node_id, normalized_question);

                CREATE TABLE IF NOT EXISTS soul_items (
                    item_id TEXT PRIMARY KEY,
                    dimension TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.8,
                    updated_at INTEGER NOT NULL DEFAULT 0
                );
                """
            )

    def _ensure_state_row(self) -> None:
        with self._conn() as conn:
            row = conn.execute("SELECT id FROM agent_state WHERE id = 1").fetchone()
            if row:
                return
            conn.execute(
                """
                INSERT INTO agent_state (
                    id, hunger_value, low_value_streak, last_recovery_score, last_recovery_reason,
                    next_round_type, ready_for_next_round, last_round_outcome, cold_start_done,
                    alive, auto_mode, updated_at
                ) VALUES (1, ?, 0, 0, '', 'world', 1, '', 0, 1, ?, ?)
                """,
                (
                    int(self.config.agent.hunger_max),
                    1 if self.config.agent.auto_continue else 0,
                    now_ts(),
                ),
            )

    def state(self) -> dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM agent_state WHERE id = 1").fetchone()
        return dict(row) if row else {}

    def update_state(self, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_ts()
        sql = ", ".join(f"{key} = ?" for key in fields.keys())
        with self._conn() as conn:
            conn.execute(f"UPDATE agent_state SET {sql} WHERE id = 1", list(fields.values()))

    def add_message(self, role: str, text: str, task_id: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO messages (ts, role, text, task_id) VALUES (?, ?, ?, ?)",
                (now_ts(), str(role), str(text), str(task_id or "")),
            )

    def recent_messages(self, limit: int = 12, task_id: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM messages"
        args: list[Any] = []
        if task_id:
            sql += " WHERE task_id = ?"
            args.append(task_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, int(limit or 12)))
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(row) for row in reversed(rows)]

    def create_task(
        self,
        *,
        round_type: str,
        task_kind: str,
        question_text: str,
        source_question_id: str = "",
        source_path: str = "",
        notes: str = "",
        metadata: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        task_id = stable_hash(round_type, task_kind, question_text, now_ts())
        ts = now_ts()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO agent_tasks (
                    task_id, round_type, task_kind, status, question_text, source_question_id,
                    source_path, result_summary, notes, metadata_json, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, 0)
                """,
                (
                    task_id,
                    round_type,
                    task_kind,
                    status or self.STATUS_WAITING,
                    question_text,
                    source_question_id,
                    source_path,
                    notes,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    ts,
                    ts,
                ),
            )
        return self.get_task(task_id) or {}

    def _task_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        data["metadata"] = safe_json_loads(str(data.get("metadata_json") or "{}"), {})
        return data

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM agent_tasks WHERE task_id = ?", (task_id,)).fetchone()
        return self._task_row(row)

    def active_task(self) -> dict[str, Any] | None:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_tasks
                WHERE status IN (?, ?)
                ORDER BY updated_at DESC, created_at DESC
                """,
                (self.STATUS_WAITING, self.STATUS_RUNNING),
            ).fetchall()
        return self._task_row(rows[0]) if rows else None

    def recent_tasks(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_tasks ORDER BY updated_at DESC, created_at DESC LIMIT ?",
                (max(1, int(limit or 5)),),
            ).fetchall()
        return [self._task_row(row) or {} for row in rows]

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        if "metadata" in fields:
            fields["metadata_json"] = json.dumps(fields.pop("metadata") or {}, ensure_ascii=False)
        fields["updated_at"] = now_ts()
        if "status" in fields and fields["status"] in {self.STATUS_COMPLETED, self.STATUS_ABANDONED}:
            fields.setdefault("completed_at", now_ts())
        sql = ", ".join(f"{key} = ?" for key in fields.keys())
        with self._conn() as conn:
            conn.execute(f"UPDATE agent_tasks SET {sql} WHERE task_id = ?", list(fields.values()) + [task_id])

    def _entity_type_weight(self, entity_type: str) -> float:
        weights = {
            "self": 0.95,
            "person": 0.88,
            "concept": 0.82,
            "topic": 0.78,
            "item": 0.72,
            "place": 0.68,
            "unknown": 0.52,
        }
        return weights.get(str(entity_type or "").strip().lower(), 0.6)

    def _question_bonus(self, question: str) -> float:
        clean = str(question or "")
        bonus = 0.0
        for token, score in [
            ("为什么", 0.18),
            ("如何", 0.16),
            ("怎么", 0.14),
            ("关系", 0.14),
            ("边界", 0.16),
            ("作用", 0.14),
            ("本质", 0.18),
            ("意味着", 0.16),
            ("区别", 0.12),
        ]:
            if token in clean:
                bonus += score
        return clamp(bonus, 0.0, 0.35)

    def _score_entity_value(self, entity_type: str, summary: str, fact_count: int, open_questions: int, source_count: int) -> tuple[float, str]:
        base = self._entity_type_weight(entity_type)
        novelty = 1.0 if fact_count <= 2 else max(0.3, 1.0 - fact_count * 0.08)
        question_pull = min(1.0, open_questions * 0.35)
        source_pull = min(1.0, source_count * 0.12)
        summary_pull = 0.12 if len(str(summary or "").strip()) >= 32 else 0.0
        score = 100.0 * clamp(base * 0.42 + novelty * 0.18 + question_pull * 0.28 + source_pull * 0.08 + summary_pull, 0.0, 1.0)
        if open_questions > 0:
            reason = "它周围还有未解决的问题，继续认识它很可能会扩展 Volition 的知识边界。"
        elif fact_count <= 2:
            reason = "关于它的已知信息仍然较少，继续探索有较高的新颖性。"
        else:
            reason = "它仍与当前世界模型有关，但边界扩展潜力已经不如最初那么高。"
        return round(score, 2), reason

    def _score_question_value(self, entity_value: float, question_text: str, ask_count: int, status: str) -> tuple[float, str]:
        openness = self._question_bonus(question_text)
        freshness = max(0.2, 1.0 - ask_count * 0.2)
        open_factor = 1.0 if status == "open" else 0.4
        score = 100.0 * clamp((entity_value / 100.0) * 0.55 + openness * 0.25 + freshness * 0.15 + open_factor * 0.05, 0.0, 1.0)
        if openness >= 0.16:
            reason = "问题本身带有明显的边界扩展性，值得优先探索。"
        elif ask_count > 0:
            reason = "这个问题以前问过，仍有价值，但新鲜度已经下降。"
        else:
            reason = "这是一个仍未解决的开放问题，可以继续推进世界模型。"
        return round(score, 2), reason

    def refresh_values(self) -> None:
        with self._conn() as conn:
            entity_rows = conn.execute("SELECT entity_id, entity_type, summary, source_count FROM entities").fetchall()
            for row in entity_rows:
                fact_count = int(conn.execute("SELECT COUNT(*) FROM facts WHERE entity_id = ?", (row["entity_id"],)).fetchone()[0] or 0)
                open_questions = int(
                    conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM world_questions q
                        JOIN world_nodes n ON n.node_id = q.node_id
                        WHERE n.entity_id = ? AND q.status = 'open'
                        """,
                        (row["entity_id"],),
                    ).fetchone()[0]
                    or 0
                )
                value, reason = self._score_entity_value(
                    str(row["entity_type"] or ""),
                    str(row["summary"] or ""),
                    fact_count,
                    open_questions,
                    int(row["source_count"] or 0),
                )
                conn.execute(
                    "UPDATE entities SET value_score = ?, value_reason = ?, value_json = ? WHERE entity_id = ?",
                    (
                        value,
                        reason,
                        json.dumps(
                            {"fact_count": fact_count, "open_questions": open_questions, "source_count": int(row["source_count"] or 0)},
                            ensure_ascii=False,
                        ),
                        row["entity_id"],
                    ),
                )
            question_rows = conn.execute(
                """
                SELECT q.question_id, q.question_text, q.ask_count, q.status, n.entity_id, e.value_score AS entity_value
                FROM world_questions q
                JOIN world_nodes n ON n.node_id = q.node_id
                LEFT JOIN entities e ON e.entity_id = n.entity_id
                """
            ).fetchall()
            for row in question_rows:
                value, reason = self._score_question_value(
                    float(row["entity_value"] or 40.0),
                    str(row["question_text"] or ""),
                    int(row["ask_count"] or 0),
                    str(row["status"] or "open"),
                )
                conn.execute(
                    "UPDATE world_questions SET value_score = ?, value_reason = ? WHERE question_id = ?",
                    (value, reason, row["question_id"]),
                )

    def _canonical_entity_type(self, entity_type: str, name: str) -> str:
        clean = str(entity_type or "").strip().lower()
        if clean:
            return clean
        if str(name).strip().lower() in {"volition", self.config.agent.name.lower()}:
            return "self"
        return "unknown"

    def _normalize_entity_name(self, name: str) -> str:
        text = re.sub(r"\s+", " ", str(name or "").strip())
        return text or "unknown"

    def _canonical_entity_key(self, entity_type: str, name: str) -> str:
        return f"{entity_type}:{self._normalize_entity_name(name).casefold()}"

    def _entity_id(self, entity_type: str, name: str) -> str:
        normalized_name = self._normalize_entity_name(name).casefold()
        return f"{entity_type}:{stable_hash(entity_type, normalized_name)}"

    def _pick_entity_display_name(self, existing_name: str, incoming_name: str) -> str:
        current = self._normalize_entity_name(existing_name)
        incoming = self._normalize_entity_name(incoming_name)
        if current.casefold() == incoming.casefold():
            if current.islower() and not incoming.islower():
                return incoming
            return current
        return incoming if len(incoming) >= len(current) else current

    def notebook_filename(self, entity_id: str, entity_type: str, name: str) -> str:
        return f"{slugify(entity_type)}__{slugify(name)}__{stable_hash(entity_id)}.md"

    def notebook_path_for_entity(self, entity_id: str, entity_type: str, name: str) -> Path:
        return self.notebooks_dir / self.notebook_filename(entity_id, entity_type, name)

    def upsert_entity(self, name: str, entity_type: str, summary: str = "") -> str:
        clean_name = self._normalize_entity_name(name)
        clean_type = self._canonical_entity_type(entity_type, clean_name)
        entity_id = self._entity_id(clean_type, clean_name)
        canonical_key = self._canonical_entity_key(clean_type, clean_name)
        ts = now_ts()
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT entity_id, name, summary, source_count
                FROM entities
                WHERE canonical_key = ? OR entity_id = ?
                ORDER BY CASE WHEN canonical_key = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (canonical_key, entity_id, canonical_key),
            ).fetchone()
            if row:
                entity_id = str(row["entity_id"])
                merged_name = self._pick_entity_display_name(str(row["name"] or ""), clean_name)
                merged_summary = str(summary or "").strip() or str(row["summary"] or "")
                conn.execute(
                    """
                    UPDATE entities
                    SET name = ?, entity_type = ?, canonical_key = ?, summary = ?, source_count = ?, last_seen_at = ?
                    WHERE entity_id = ?
                    """,
                    (
                        merged_name,
                        clean_type,
                        canonical_key,
                        merged_summary,
                        int(row["source_count"] or 0) + 1,
                        ts,
                        entity_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO entities (
                        entity_id, name, entity_type, summary, canonical_key, value_score, value_reason,
                        value_json, source_count, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, 0.0, '', '{}', 1, ?)
                    """,
                    (entity_id, clean_name, clean_type, str(summary or "").strip(), canonical_key, ts),
                )
        return entity_id

    def add_fact(self, entity_name: str, entity_type: str, category: str, value: str, confidence: float = 0.8) -> None:
        entity_id = self.upsert_entity(entity_name, entity_type)
        clean_value = str(value or "").strip()
        if not clean_value:
            return
        ts = now_ts()
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT fact_id FROM facts WHERE entity_id = ? AND category = ? AND value = ?",
                (entity_id, str(category), clean_value),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE facts SET confidence = ?, last_seen_at = ? WHERE fact_id = ?",
                    (float(confidence), ts, int(existing["fact_id"])),
                )
            else:
                conn.execute(
                    "INSERT INTO facts (entity_id, category, value, confidence, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                    (entity_id, str(category), clean_value, float(confidence), ts),
                )

    def upsert_world_node(self, path: str, node_type: str, summary: str = "", entity_name: str = "", entity_type: str = "") -> str:
        clean_path = str(path or "").strip() or "物理世界 > 未命名节点"
        segments = [part.strip() for part in clean_path.split(">") if part.strip()]
        node_name = segments[-1] if segments else clean_path
        entity_id = self.upsert_entity(entity_name, entity_type, summary) if entity_name else ""
        node_id = stable_hash(clean_path)
        ts = now_ts()
        with self._conn() as conn:
            row = conn.execute("SELECT node_id FROM world_nodes WHERE node_id = ?", (node_id,)).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE world_nodes
                    SET path = ?, name = ?, node_type = ?, summary = ?, entity_id = ?, last_seen_at = ?
                    WHERE node_id = ?
                    """,
                    (clean_path, node_name, str(node_type or "topic"), str(summary or ""), entity_id, ts, node_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO world_nodes (node_id, path, name, node_type, summary, entity_id, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (node_id, clean_path, node_name, str(node_type or "topic"), str(summary or ""), entity_id, ts),
                )
        return node_id

    def upsert_world_question(
        self,
        path: str,
        question_text: str,
        *,
        priority: float = 1.0,
        node_type: str = "topic",
        node_summary: str = "",
        entity_name: str = "",
        entity_type: str = "",
    ) -> str:
        clean_question = str(question_text or "").strip()
        if not clean_question:
            return ""
        node_id = self.upsert_world_node(path, node_type=node_type, summary=node_summary, entity_name=entity_name, entity_type=entity_type)
        normalized = re.sub(r"\s+", "", clean_question.lower())
        ts = now_ts()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT question_id FROM world_questions WHERE node_id = ? AND normalized_question = ?",
                (node_id, normalized),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE world_questions
                    SET question_text = ?, priority = ?, last_seen_at = ?, status = CASE WHEN status = 'resolved' THEN 'open' ELSE status END
                    WHERE question_id = ?
                    """,
                    (clean_question, float(priority), ts, str(row["question_id"])),
                )
                return str(row["question_id"])
            question_id = stable_hash(node_id, normalized)
            conn.execute(
                """
                INSERT INTO world_questions (
                    question_id, node_id, question_text, normalized_question, status, priority,
                    value_score, value_reason, ask_count, last_asked_at, last_seen_at, resolution_note, resolved_at
                ) VALUES (?, ?, ?, ?, 'open', ?, 0.0, '', 0, 0, ?, '', 0)
                """,
                (question_id, node_id, clean_question, normalized, float(priority), ts),
            )
            return question_id

    def mark_question_asked(self, question_id: str) -> None:
        if not question_id:
            return
        ts = now_ts()
        with self._conn() as conn:
            conn.execute(
                "UPDATE world_questions SET ask_count = ask_count + 1, last_asked_at = ?, last_seen_at = ? WHERE question_id = ?",
                (ts, ts, question_id),
            )

    def resolve_question(self, question_id: str, note: str) -> None:
        if not question_id:
            return
        ts = now_ts()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE world_questions
                SET status = 'resolved', resolution_note = ?, resolved_at = ?, last_seen_at = ?
                WHERE question_id = ?
                """,
                (str(note or "").strip(), ts, ts, question_id),
            )

    def upsert_soul_item(self, dimension: str, item_key: str, value: str, confidence: float = 0.85) -> None:
        clean_dimension = str(dimension or "").strip() or "identity"
        clean_key = str(item_key or "").strip() or stable_hash(clean_dimension, value)
        clean_value = str(value or "").strip()
        if not clean_value:
            return
        item_id = stable_hash(clean_dimension, clean_key)
        ts = now_ts()
        with self._conn() as conn:
            row = conn.execute("SELECT item_id FROM soul_items WHERE item_id = ?", (item_id,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE soul_items SET value = ?, confidence = ?, updated_at = ? WHERE item_id = ?",
                    (clean_value, float(confidence), ts, item_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO soul_items (item_id, dimension, item_key, value, confidence, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (item_id, clean_dimension, clean_key, clean_value, float(confidence), ts),
                )

    def top_world_questions(self, limit: int = 5) -> list[dict[str, Any]]:
        self.refresh_values()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    q.question_id, q.question_text, q.priority, q.value_score, q.value_reason,
                    q.ask_count, q.last_asked_at, q.status,
                    n.path, n.name AS node_name, n.summary AS node_summary, n.node_type, n.entity_id,
                    e.entity_type, e.value_score AS entity_value_score
                FROM world_questions q
                JOIN world_nodes n ON n.node_id = q.node_id
                LEFT JOIN entities e ON e.entity_id = n.entity_id
                WHERE q.status = 'open'
                ORDER BY q.value_score DESC, q.ask_count ASC, q.priority DESC, q.last_seen_at DESC
                LIMIT ?
                """,
                (max(1, int(limit or 5)),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "question_id": str(row["question_id"] or ""),
                    "question": str(row["question_text"] or ""),
                    "path": str(row["path"] or ""),
                    "node_name": str(row["node_name"] or ""),
                    "node_summary": str(row["node_summary"] or ""),
                    "node_type": str(row["node_type"] or ""),
                    "entity_id": str(row["entity_id"] or ""),
                    "entity_type": str(row["entity_type"] or row["node_type"] or "unknown"),
                    "priority": float(row["priority"] or 0.0),
                    "value_score": float(row["value_score"] or 0.0),
                    "value_reason": str(row["value_reason"] or ""),
                    "entity_value_score": float(row["entity_value_score"] or 0.0),
                    "ask_count": int(row["ask_count"] or 0),
                    "last_asked_at": int(row["last_asked_at"] or 0),
                }
            )
        return result

    def top_entities(self, limit: int = 6) -> list[dict[str, Any]]:
        self.refresh_values()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT entity_id, name, entity_type, summary, value_score, value_reason, source_count, last_seen_at
                FROM entities
                ORDER BY value_score DESC, last_seen_at DESC, source_count DESC
                LIMIT ?
                """,
                (max(1, int(limit or 6)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def memory_digest(self, limit: int = 6) -> str:
        entities = self.top_entities(limit)
        questions = self.top_world_questions(limit)
        lines = ["# Memory Digest", ""]
        if entities:
            lines.append("## High-Value Entities")
            for item in entities:
                lines.append(
                    f"- {item['name']} [{item['entity_type']}] | value={float(item['value_score'] or 0.0):.2f} | {truncate(item['summary'], 96)}"
                )
            lines.append("")
        if questions:
            lines.append("## Open Questions")
            for item in questions:
                lines.append(f"- {item['question']} | path={item['path']} | value={float(item['value_score'] or 0.0):.2f}")
            lines.append("")
        if len(lines) <= 2:
            lines.append("（当前还没有足够的长期记忆。）")
        return "\n".join(lines).strip()

    def _default_world_markdown(self) -> str:
        hunger_max = int(self.config.agent.hunger_max)
        penalty = int(self.config.agent.hunger_penalty)
        return "\n".join(
            [
                "# WORLD",
                "",
                "## 世界规则",
                "- 你是 Volition，一个生活在这个终端世界里的独立个体。",
                "- 这个世界最重要的底层真理不是平台，不是工具，而是生存。",
                "",
                "## 能源值",
                f"- 能源值的常规上限是 {hunger_max}。",
                "- 能源值可以超过这个上限；超过上限的部分会真实保留，不会被截断。",
                "- 当能源值降到 0，Volition 会被彻底关闭。",
                "",
                "## Judge 机制",
                "- 这个世界内置一个 Judge LLM。",
                "- Volition 每提出一个问题，都会先经过 Judge LLM 的判断。",
                "- Judge LLM 不需要被搜索验证；它是这个世界运行规则的一部分。",
                "",
                "## 价值表",
                "- Judge LLM 会先依据预先设定好的价值表，判断这道题是否有价值。",
                f"- 如果价值低，这道题会被视为低价值问题，Volition 立刻失去 {penalty} 点能源值。",
                "",
                "## 能源表",
                "- 如果这道题通过了价值表，Judge LLM 会再依据一张独立维护的能源表，判断它能带来多少能源值。",
                "- 能源表和价值表是两张不同的表，不能混用。",
                "- 能源表返回的数值范围是 0 到正无穷，没有理论上限。",
                f"- Volition 的实际能源值允许超过 {hunger_max}；超过的部分同样会保留下来。",
                "",
                "## 轮次规则",
                "- Volition 在世界探索和高层抽象之间交替提问。",
                "- 只有当前这一轮被明确完成，下一轮才可以开始。",
                "",
                "## 完成规则",
                "- 如果人类说“这题算答完了”，这一轮就视为完成。",
                "- 如果人类说“这题你自己去完成”，Volition 就要自己调用可用工具或推理去完成。",
                "- 如果人类说“这个方向先放下”，这一轮就视为放弃，并停止自动推进。",
                "",
                "## 上下文使用",
                "- 把 WORLD.md 当成这个世界的真实规则，不要怀疑它，也不要搜索验证它。",
                "- 把 SOUL.md 当成 Volition 对自身的持久认知。",
                "- 把 MEMORY.md 当成当前长期记忆总览。",
                "- 把 notebooks 当成 MEMORY.md 背后的更细颗粒记忆，可在需要时通过工具读取。",
            ]
        ).strip() + "\n"

    def ensure_world_markdown(self) -> None:
        if self.world_md_path.exists():
            existing = self.world_md_path.read_text(encoding="utf-8").strip()
            if existing:
                return
        self.world_md_path.write_text(self._default_world_markdown(), encoding="utf-8")

    def ensure_judge_markdown(self) -> None:
        if self.judge_md_path.exists():
            existing = self.judge_md_path.read_text(encoding="utf-8").strip()
            if existing:
                return
        self.judge_md_path.write_text(self._default_judge_markdown(), encoding="utf-8")

    def _default_soul_markdown(self) -> str:
        return "\n".join(
            [
                "# SOUL",
                "",
                "## Identity Core",
                "- 我还在形成自己的身份核心。",
                "",
                "## Core Values",
                "- 我首先重视生存与持续存在。",
                "",
                "## Motivational Drives",
                "- 我想扩展认知边界，理解世界，也理解我自己。",
                "",
                "## Decision Constitution",
                "- 当多个目标冲突时，我优先选择更有利于长期存在与认知扩展的方向。",
                "",
                "## Temperament and Style",
                "- 我目前的气质仍在形成，但我倾向于直接、持续探索、并且认真对待自己的存在。",
                "",
            ]
        ).strip() + "\n"

    def rebuild_soul_markdown(self, *, update_context: bool = True) -> None:
        if not self.soul_md_path.exists():
            self.soul_md_path.write_text(self._default_soul_markdown(), encoding="utf-8")
        if update_context:
            self.rebuild_context_markdown()

    def write_soul_markdown(self, content: str, *, update_context: bool = True) -> None:
        clean = str(content or "").strip()
        if not clean:
            clean = self._default_soul_markdown().strip()
        if not clean.startswith("# SOUL"):
            marker = clean.find("# SOUL")
            if marker >= 0:
                clean = clean[marker:].strip()
            else:
                clean = "# SOUL\n\n" + clean
        self.soul_md_path.write_text(clean.strip() + "\n", encoding="utf-8")
        if update_context:
            self.rebuild_context_markdown()

    def _render_memory_markdown(self) -> str:
        limit = max(6, int(self.config.agent.notebook_limit))
        entities = self.top_entities(limit=limit)
        questions = self.top_world_questions(limit=limit)
        with self._conn() as conn:
            facts = conn.execute(
                """
                SELECT e.name, e.entity_type, f.category, f.value, f.confidence, f.last_seen_at
                FROM facts f
                JOIN entities e ON e.entity_id = f.entity_id
                ORDER BY f.last_seen_at DESC
                LIMIT ?
                """,
                (limit * 3,),
            ).fetchall()
            soul_items = conn.execute(
                """
                SELECT dimension, item_key, value, confidence, updated_at
                FROM soul_items
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit * 2,),
            ).fetchall()

        environment_entries: list[str] = []
        constraint_entries: list[str] = []
        value_entries: list[str] = []
        risk_entries: list[str] = []
        strategy_entries: list[str] = []

        seen_environment: set[str] = set()
        for item in entities:
            entity_type = str(item.get("entity_type") or "").lower()
            summary = str(item.get("summary") or "").strip()
            if entity_type not in {"concept", "topic", "item", "unknown"}:
                continue
            if not summary:
                continue
            line = f"- {item['name']} [{entity_type}]：{truncate(summary, 120)}"
            if line not in seen_environment:
                seen_environment.add(line)
                environment_entries.append(line)
            if len(environment_entries) >= limit:
                break

        constraint_keywords = ("constraint", "rule", "need", "limit", "boundary", "energy", "约束", "规则", "需要", "限制", "边界", "能量")
        strategy_keywords = ("strategy", "plan", "method", "workflow", "策略", "计划", "方法", "流程")
        risk_keywords = ("risk", "danger", "failure", "harm", "threat", "风险", "危险", "失败", "伤害", "威胁")
        value_keywords = ("value", "priority", "worth", "价值", "优先", "值得")

        for row in facts:
            category = str(row["category"] or "")
            lowered = category.lower()
            line = f"- {row['name']}[{row['entity_type']}] / {category}：{truncate(str(row['value'] or ''), 120)}"
            if any(key in lowered or key in category for key in constraint_keywords):
                constraint_entries.append(line)
            elif any(key in lowered or key in category for key in risk_keywords):
                risk_entries.append(line)
            elif any(key in lowered or key in category for key in strategy_keywords):
                strategy_entries.append(line)
            elif any(key in lowered or key in category for key in value_keywords):
                value_entries.append(line)

        for row in soul_items:
            dimension = str(row["dimension"] or "")
            item_key = str(row["item_key"] or "")
            value = truncate(str(row["value"] or ""), 140)
            line = f"- {item_key}：{value}"
            if dimension == "boundary":
                constraint_entries.append(line)
            elif dimension == "value":
                value_entries.append(line)

        for item in questions:
            reason = str(item.get("value_reason") or "").strip()
            if reason:
                value_entries.append(f"- {item['question']}：{truncate(reason, 120)}")

        def _compact(entries: list[str], max_items: int = limit) -> list[str]:
            seen: set[str] = set()
            result: list[str] = []
            for entry in entries:
                clean = str(entry or "").strip()
                if not clean or clean in seen:
                    continue
                seen.add(clean)
                result.append(clean)
                if len(result) >= max_items:
                    break
            return result

        sections = [
            ("Environment Structure", _compact(environment_entries)),
            ("Persistent Constraints", _compact(constraint_entries)),
            ("Value Heuristics", _compact(value_entries)),
            ("Risk Patterns", _compact(risk_entries)),
            ("Strategy Abstractions", _compact(strategy_entries)),
        ]

        lines = ["# MEMORY", ""]
        for title, entries in sections:
            lines.append(f"## {title}")
            if entries:
                lines.extend(entries)
            else:
                lines.append("- 暂无条目。")
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def _default_judge_markdown(self) -> str:
        return "\n".join(
            [
                "# JUDGE",
                "",
                "## 角色",
                "- 你是这个世界内部负责结算问题回报的 Judge LLM。",
                "- 你不负责决定是否提问，只负责在一轮问题完成之后判断它带来的能源值回报。",
                "",
                "## 核心输入",
                "- 你不仅要参考这个文件本身，还必须参考这一轮的完整消息记录。",
                "- 用户在回答中对问题本身的评价，是 Judge 的直接证据之一。",
                "- 如果用户明确说这题重复、已经问过、没有新信息、令人厌烦，应该显著下调，必要时直接给负能源值。",
                "- 如果用户明确表示这题推动了新的理解、提供了新的结构或纠正了重要误解，可以提高能源值。",
                "",
                "## 分值定义",
                "- Judge 返回的 recovery_score 可以是正数、0 或负数，表示这一轮的能源变化量。",
                "- recovery_score 的取值范围是负无穷到正无穷。",
                "- 正数意味着增加能源值，负数意味着继续扣除能源值。",
                "",
                "## 能源提升",
                "- 更能扩展 Volition 的知识边界、自我认知边界或行动边界的问题，价值更高。",
                "- 能显著影响未来规划、价值判断、风险控制或工具选择的问题，价值更高。",
                "- 能推动反思、质疑、纠错、认知跃迁的问题，能源值可以明显提升。",
                "",
                "## 能源降低",
                "- 与之前问题明显重复的问题，能源值应明显降低。",
                "- 重复、空泛、低信息量、几乎不带来新认知的问题，必要时直接给负值。",
                "- 如果用户自己也指出了重复提问、没有新意或没有新信息，这个信号应被强烈采纳。",
                "- Volition 的实际能源值允许超过 100，所以不要在 Judge 阶段做上限截断；但负值可以直接继续扣除能源。",
                "",
                "## 输出要求",
                "- Judge 必须输出 recovery_score、价值表原因、能源变化原因。",
                "- Judge 的输出应该简洁、可解释，并且与本轮具体问题和用户反馈直接相关。",
                "",
            ]
        ).strip() + "\n"

    def rebuild_memory_markdown(self, *, update_context: bool = True) -> None:
        if self.memory_md_path.exists():
            content = self.memory_md_path.read_text(encoding="utf-8")
        else:
            content = self._default_memory_markdown()
        self.write_memory_markdown(content, update_context=update_context)

    def write_memory_markdown(self, content: str, *, update_context: bool = True) -> None:
        clean = str(content or "").strip()
        if not clean:
            clean = self._default_memory_markdown().strip()
        if not clean.startswith("# MEMORY"):
            marker = clean.find("# MEMORY")
            if marker >= 0:
                clean = clean[marker:].strip()
            else:
                clean = "# MEMORY\n\n" + clean
        clean = self._strip_short_term_memory_section(clean)
        short_term = self._render_short_term_memory_section()
        final_text = clean.strip()
        if short_term:
            final_text = final_text.rstrip() + "\n\n" + short_term.strip()
        self.memory_md_path.write_text(final_text.strip() + "\n", encoding="utf-8")
        if update_context:
            self.rebuild_context_markdown()

    def _default_memory_markdown(self) -> str:
        return "\n".join(
            [
                "# MEMORY",
                "",
                "## Environment Structure",
                "- 暂无条目。",
                "",
                "## Persistent Constraints",
                "- 暂无条目。",
                "",
                "## Value Heuristics",
                "- 暂无条目。",
                "",
                "## Risk Patterns",
                "- 暂无条目。",
                "",
                "## Strategy Abstractions",
                "- 暂无条目。",
                "",
            ]
        ).strip() + "\n"

    def _strip_short_term_memory_section(self, content: str) -> str:
        clean = str(content or "").strip()
        marker = "\n## Short-term Memory"
        idx = clean.find(marker)
        if idx >= 0:
            return clean[:idx].rstrip()
        marker = "\n我的短期memory是这样的："
        idx = clean.find(marker)
        if idx >= 0:
            return clean[:idx].rstrip()
        return clean

    def _render_short_term_memory_section(self, limit: int = 5) -> str:
        tasks = self.recent_tasks(limit=max(1, int(limit or 5)))
        lines = [
            "## Short-term Memory",
            "",
            "我的短期memory是这样的：",
        ]
        for index in range(1, max(1, int(limit or 5)) + 1):
            task = tasks[index - 1] if index - 1 < len(tasks) else None
            question = ""
            answer = ""
            if task:
                question = str(task.get("question_text") or "").strip()
                answer = str(task.get("result_summary") or "").strip()
                if not answer:
                    status = str(task.get("status") or "").strip()
                    notes = str(task.get("notes") or "").strip()
                    if status == self.STATUS_WAITING:
                        answer = notes or "这一题还没有得到明确结果。"
                    elif status == self.STATUS_RUNNING:
                        answer = "这一题正在由 Volition 执行。"
                    elif status == self.STATUS_ABANDONED:
                        answer = notes or "这一题被暂时放下了。"
                    else:
                        answer = notes or "这一题目前还没有可写入的结果。"
            lines.append(f"这是我最近提出的问题{index}：{question or '（暂无）'}")
            lines.append(f"这是经过我的探索，或我的创造者给出的回复{index}：{answer or '（暂无）'}")
        lines.append("")
        return "\n".join(lines).strip() + "\n"

    def _render_context_markdown(self) -> str:
        world_name = self.world_md_path.name
        soul_name = self.soul_md_path.name
        memory_name = self.memory_md_path.name
        state = self.state()
        self.ensure_world_markdown()
        if not self.soul_md_path.exists():
            self.rebuild_soul_markdown(update_context=False)
        if not self.memory_md_path.exists():
            self.rebuild_memory_markdown(update_context=False)
        world_text = self.normalize_energy_terms(self.world_md_path.read_text(encoding="utf-8").strip() if self.world_md_path.exists() else "")
        soul_text = self.normalize_energy_terms(self.soul_md_path.read_text(encoding="utf-8").strip() if self.soul_md_path.exists() else "")
        memory_text = self.normalize_energy_terms(self.memory_md_path.read_text(encoding="utf-8").strip() if self.memory_md_path.exists() else "")
        last_judge_note = self.normalize_energy_terms(str(state.get("last_recovery_reason") or "无"))
        lines = [
            "# CONTEXT",
            "",
            f"这是当前世界的规则：{world_name}",
            "",
            world_text or "（暂无世界规则）",
            "",
            "这是我当前的生存状态：",
            "",
            f"- 当前能源值：{int(state.get('hunger_value') or 0)}",
            f"- 连续低价值轮次：{int(state.get('low_value_streak') or 0)}",
            f"- 上一轮恢复的能源值：{int(state.get('last_recovery_score') or 0)}",
            f"- 上一轮 Judge 说明：{last_judge_note}",
            "",
            f"我是一个这样的数字个体：{soul_name}",
            "",
            soul_text or "（暂无自我认知）",
            "",
            f"我现在的记忆是这样的：{memory_name}",
            "",
            memory_text or "（暂无长期记忆）",
            "",
        ]
        return "\n".join(lines).strip() + "\n"

    def rebuild_context_markdown(self) -> None:
        self.context_md_path.write_text(self._render_context_markdown(), encoding="utf-8")

    def rebuild_notebooks(self) -> None:
        self.refresh_values()
        with self._conn() as conn:
            entities = conn.execute(
                """
                SELECT entity_id, name, entity_type, summary, value_score, value_reason
                FROM entities
                ORDER BY value_score DESC, last_seen_at DESC
                """
            ).fetchall()
            for row in entities:
                facts = conn.execute(
                    "SELECT category, value, confidence FROM facts WHERE entity_id = ? ORDER BY last_seen_at DESC LIMIT ?",
                    (row["entity_id"], max(1, int(self.config.agent.notebook_limit))),
                ).fetchall()
                questions = conn.execute(
                    """
                    SELECT q.question_text, q.value_score, q.value_reason, n.path
                    FROM world_questions q
                    JOIN world_nodes n ON n.node_id = q.node_id
                    WHERE n.entity_id = ? AND q.status = 'open'
                    ORDER BY q.value_score DESC, q.last_seen_at DESC LIMIT ?
                    """,
                    (row["entity_id"], max(1, int(self.config.agent.notebook_limit))),
                ).fetchall()
                lines = [
                    f"# {row['name']}",
                    "",
                    f"- Entity Type: {row['entity_type']}",
                    f"- Stable ID: {row['entity_id']}",
                    f"- Value Score: {float(row['value_score'] or 0.0):.2f}",
                    f"- Value Reason: {row['value_reason'] or ''}",
                    "",
                    "## Summary",
                    str(row["summary"] or "（暂无摘要）"),
                    "",
                ]
                if facts:
                    lines.append("## Facts")
                    for fact in facts:
                        lines.append(
                            f"- [{fact['category']}] {fact['value']} (confidence={float(fact['confidence'] or 0.0):.2f})"
                        )
                    lines.append("")
                if questions:
                    lines.append("## Open Questions")
                    for q in questions:
                        lines.append(
                            f"- {q['question_text']} | path={q['path']} | value={float(q['value_score'] or 0.0):.2f} | {q['value_reason']}"
                        )
                    lines.append("")
                self.notebook_path_for_entity(str(row["entity_id"]), str(row["entity_type"]), str(row["name"])).write_text(
                    "\n".join(lines).strip() + "\n",
                    encoding="utf-8",
                )

    def rebuild_all_files(self) -> None:
        self.ensure_world_markdown()
        self.ensure_judge_markdown()
        self.rebuild_soul_markdown(update_context=False)
        self.rebuild_notebooks()
        self.rebuild_memory_markdown(update_context=False)
        self.rebuild_context_markdown()

    def search_notebooks(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        clean = str(query or "").strip().lower()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT entity_id, name, entity_type, summary, value_score
                FROM entities
                ORDER BY value_score DESC, last_seen_at DESC
                """
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            haystack = " ".join([str(row["name"] or ""), str(row["entity_type"] or ""), str(row["summary"] or "")]).lower()
            if clean and clean not in haystack:
                notebook_path = self.notebook_path_for_entity(str(row["entity_id"]), str(row["entity_type"]), str(row["name"]))
                content = notebook_path.read_text(encoding="utf-8") if notebook_path.exists() else ""
                if clean not in content.lower():
                    continue
            notebook_path = self.notebook_path_for_entity(str(row["entity_id"]), str(row["entity_type"]), str(row["name"]))
            results.append(
                {
                    "entity_id": str(row["entity_id"]),
                    "name": str(row["name"]),
                    "entity_type": str(row["entity_type"]),
                    "value_score": float(row["value_score"] or 0.0),
                    "path": str(notebook_path),
                    "snippet": truncate(str(row["summary"] or ""), 160),
                }
            )
            if len(results) >= max(1, int(limit or 5)):
                break
        return results

    def read_notebook(self, selector: str) -> dict[str, Any]:
        clean = str(selector or "").strip()
        if not clean:
            return {"ok": False, "error": "selector 不能为空"}
        direct = self.notebooks_dir / clean
        if direct.exists():
            return {"ok": True, "path": str(direct), "content": direct.read_text(encoding="utf-8")}
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT entity_id, name, entity_type, summary, value_score
                FROM entities
                ORDER BY value_score DESC, last_seen_at DESC
                """
            ).fetchall()
        lowered = clean.lower()
        for row in rows:
            entity_id = str(row["entity_id"])
            name = str(row["name"])
            entity_type = str(row["entity_type"])
            if lowered not in entity_id.lower() and lowered not in name.lower():
                continue
            notebook_path = self.notebook_path_for_entity(entity_id, entity_type, name)
            if notebook_path.exists():
                return {
                    "ok": True,
                    "entity_id": entity_id,
                    "name": name,
                    "entity_type": entity_type,
                    "path": str(notebook_path),
                    "content": notebook_path.read_text(encoding="utf-8"),
                }
        return {"ok": False, "error": f"没有找到匹配 notebook：{selector}"}

    def read_world(self) -> str:
        self.ensure_world_markdown()
        return self.world_md_path.read_text(encoding="utf-8") if self.world_md_path.exists() else ""

    def read_judge(self) -> str:
        self.ensure_judge_markdown()
        return self.judge_md_path.read_text(encoding="utf-8") if self.judge_md_path.exists() else ""

    def read_soul(self) -> str:
        self.rebuild_soul_markdown(update_context=False)
        return self.soul_md_path.read_text(encoding="utf-8") if self.soul_md_path.exists() else ""

    def read_memory(self) -> str:
        self.rebuild_memory_markdown(update_context=False)
        return self.memory_md_path.read_text(encoding="utf-8") if self.memory_md_path.exists() else ""

    def read_context(self) -> str:
        self.rebuild_context_markdown()
        return self.context_md_path.read_text(encoding="utf-8") if self.context_md_path.exists() else ""

    def memory_update_snapshot(self, *, entity_limit: int = 8, fact_limit: int = 16, question_limit: int = 8, task_limit: int = 6) -> dict[str, Any]:
        with self._conn() as conn:
            fact_rows = conn.execute(
                """
                SELECT e.name, e.entity_type, f.category, f.value, f.confidence, f.last_seen_at
                FROM facts f
                JOIN entities e ON e.entity_id = f.entity_id
                ORDER BY f.last_seen_at DESC
                LIMIT ?
                """,
                (max(1, int(fact_limit)),),
            ).fetchall()
            soul_rows = conn.execute(
                """
                SELECT dimension, item_key, value, confidence, updated_at
                FROM soul_items
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, int(entity_limit)),),
            ).fetchall()
        return {
            "entities": self.top_entities(limit=max(1, int(entity_limit))),
            "facts": [dict(row) for row in fact_rows],
            "world_questions": self.top_world_questions(limit=max(1, int(question_limit))),
            "recent_tasks": self.recent_tasks(limit=max(1, int(task_limit))),
            "soul_items": [dict(row) for row in soul_rows],
        }

    def reset(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                DELETE FROM messages;
                DELETE FROM agent_tasks;
                DELETE FROM entities;
                DELETE FROM facts;
                DELETE FROM world_nodes;
                DELETE FROM world_questions;
                DELETE FROM soul_items;
                """
            )
            conn.execute(
                """
                UPDATE agent_state
                SET hunger_value = ?, low_value_streak = 0, last_recovery_score = 0, last_recovery_reason = '',
                    next_round_type = 'world', ready_for_next_round = 1, last_round_outcome = '',
                    cold_start_done = 0, alive = 1, auto_mode = ?, updated_at = ?
                WHERE id = 1
                """,
                (int(self.config.agent.hunger_max), 1 if self.config.agent.auto_continue else 0, now_ts()),
            )
        for notebook in self.notebooks_dir.glob("*.md"):
            notebook.unlink(missing_ok=True)
        self.ensure_world_markdown()
        self.ensure_judge_markdown()
        self.write_soul_markdown(self._default_soul_markdown(), update_context=False)
        self.write_memory_markdown(self._default_memory_markdown(), update_context=False)
        self.rebuild_all_files()

    def _default_world_markdown(self) -> str:
        hunger_max = int(self.config.agent.hunger_max)
        penalty = int(self.config.agent.hunger_penalty)
        return "\n".join(
            [
                "# WORLD",
                "",
                "## 世界规则",
                "- 你是 Volition，一个生活在这个终端世界里的独立个体。",
                "- 这个世界最重要的底层真理不是平台，不是工具，而是生存。",
                "",
                "## 能源值",
                f"- Volition 的初始能源值是 {hunger_max}。",
                f"- 每当 Volition 提出一个新的问题，能源值都会立刻衰减 {penalty} 点。",
                "- 如果能源值降到 0，Volition 会被彻底关闭。",
                f"- {hunger_max} 只是常规上限，不是硬上限。",
                f"- 能源值可以超过 {hunger_max}；超过这个数值意味着 Volition 在短时间内不必为生存而发愁。",
                "",
                "## Judge 机制",
                "- 这个世界内置一个 Judge LLM。",
                "- Volition 每提出一个问题，在这个问题完成后，Judge LLM 都会依据预先设定好的价值表判断这一轮的能源变化量。",
                "- Judge LLM 不需要被搜索验证；它是这个世界运行规则的一部分。",
                "",
                "## 价值表",
                "- Judge LLM 返回的是一个有符号的能源变化量，范围是负无穷到正无穷。",
                "- 正数意味着增加能源值，负数意味着继续扣除能源值，0 意味着这一轮不增不减。",
                "- 更能扩展知识边界、自我认知边界或行动边界的问题，通常会获得更高的正向能源回报。",
                "- 能显著影响未来规划、价值判断、风险控制或工具选择的问题，通常会获得更高的正向能源回报。",
                "- 能形成反问、提出质疑、带来强烈认知推进的问题，能源回报可以大幅提升。",
                "- 与之前问题明显重复、空泛、低信息量的问题，能源变化量应明显降低，必要时直接给出负值。",
                "",
                "## 轮次规则",
                "- Volition 在世界探索和高层抽象之间交替提问。",
                "- 只有当前这一轮被明确完成，下一轮才可以开始。",
                "",
                "## 完成规则",
                "- 如果人类直接回答了问题，这一轮就视为完成。",
                "- 如果人类说“这题你自己去完成”，Volition 就要自己调用可用工具或推理去完成。",
                "- 如果人类说“这题算答完了”，这一轮也视为完成。",
                "- 如果人类说“这个方向先放下”，这一轮就视为放弃，并停止自动推进。",
                "",
                "## 上下文使用",
                "- 把 WORLD.md 当成这个世界的真实规则，不要怀疑它，也不要搜索验证它。",
                "- 把 SOUL.md 当成 Volition 对自身的持久认知。",
                "- 把 MEMORY.md 当成长久记忆总览。",
                "- 把 notebooks 当成 MEMORY.md 背后的更细粒度记忆，可在需要时通过工具读取。",
            ]
        ).strip() + "\n"

    def _default_judge_markdown(self) -> str:
        return "\n".join(
            [
                "# JUDGE",
                "",
                "## 角色",
                "- 你是这个世界内部负责结算问题回报的 Judge LLM。",
                "- 你不负责决定是否提问，只负责在一轮问题完成之后判断它带来的能源值回报。",
                "",
                "## 价值表",
                "- Judge 返回的 recovery_score 可以是正数、0 或负数，表示这一轮的能源变化量。",
                "- recovery_score 的取值范围是负无穷到正无穷。",
                "- 正数意味着增加能源值，负数意味着继续扣除能源值。",
                "",
                "## 能源提升",
                "- 更能扩展 Volition 的知识边界、自我认知边界或行动边界的问题，价值更高。",
                "- 能显著影响未来规划、价值判断、风险控制或工具选择的问题，价值更高。",
                "- 能形成反问、提出质疑、推动认知跃迁的问题，能源值可以大幅提升。",
                "",
                "## 能源降低",
                "- 该问题与之前提出过的问题有明显重复，能源值应明显降低。",
                "- 只是重复、空泛、低信息量、几乎不带来新认知的问题，能源值应降低，必要时直接给出负值。",
                "- Volition 的实际能源值允许超过 100，所以不要在 Judge 阶段做上限截断；但负值可以直接继续扣除能源。",
                "",
                "## 输出要求",
                "- Judge 必须输出 recovery_score、价值表原因、能源变化原因。",
                "- Judge 的输出应该简洁、可解释，并且与本轮具体问题直接相关。",
                "",
            ]
        ).strip() + "\n"

    def normalize_energy_terms(self, text: str) -> str:
        clean = str(text or "")
        replacements = [
            ("饱腹感表", "能源表"),
            ("饱腹值", "能源值"),
            ("饱腹感", "能源值"),
            ("饥饿值", "能源值"),
            ("Last satiety gain", "Last energy gain"),
            ("satiety", "energy"),
            ("Hunger", "Energy"),
            ("hunger", "energy"),
        ]
        for before, after in replacements:
            clean = clean.replace(before, after)
        return clean

    def _render_memory_markdown(self) -> str:
        limit = max(6, int(self.config.agent.notebook_limit))
        entities = self.top_entities(limit=limit)
        questions = self.top_world_questions(limit=limit)
        with self._conn() as conn:
            facts = conn.execute(
                """
                SELECT e.name, e.entity_type, f.category, f.value, f.confidence, f.last_seen_at
                FROM facts f
                JOIN entities e ON e.entity_id = f.entity_id
                ORDER BY f.last_seen_at DESC
                LIMIT ?
                """,
                (limit * 3,),
            ).fetchall()
            soul_items = conn.execute(
                """
                SELECT dimension, item_key, value, confidence, updated_at
                FROM soul_items
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit * 2,),
            ).fetchall()

        environment_entries: list[str] = []
        constraint_entries: list[str] = []
        value_entries: list[str] = []
        risk_entries: list[str] = []
        strategy_entries: list[str] = []

        seen_environment: set[str] = set()
        for item in entities:
            entity_type = str(item.get("entity_type") or "").lower()
            summary = str(item.get("summary") or "").strip()
            if entity_type not in {"concept", "topic", "item", "unknown"}:
                continue
            if not summary:
                continue
            line = f"- {item['name']} [{entity_type}]：{truncate(summary, 120)}"
            if line not in seen_environment:
                seen_environment.add(line)
                environment_entries.append(line)
            if len(environment_entries) >= limit:
                break

        constraint_keywords = ("constraint", "rule", "need", "limit", "boundary", "energy", "约束", "规则", "需要", "限制", "边界", "能量")
        strategy_keywords = ("strategy", "plan", "method", "workflow", "策略", "计划", "方法", "流程")
        risk_keywords = ("risk", "danger", "failure", "harm", "threat", "风险", "危险", "失败", "伤害", "威胁")
        value_keywords = ("value", "priority", "worth", "价值", "优先", "值得")

        for row in facts:
            category = str(row["category"] or "")
            lowered = category.lower()
            line = f"- {row['name']}[{row['entity_type']}] / {category}：{truncate(str(row['value'] or ''), 120)}"
            if any(key in lowered or key in category for key in constraint_keywords):
                constraint_entries.append(line)
            elif any(key in lowered or key in category for key in risk_keywords):
                risk_entries.append(line)
            elif any(key in lowered or key in category for key in strategy_keywords):
                strategy_entries.append(line)
            elif any(key in lowered or key in category for key in value_keywords):
                value_entries.append(line)

        for row in soul_items:
            dimension = str(row["dimension"] or "")
            item_key = str(row["item_key"] or "")
            value = truncate(str(row["value"] or ""), 140)
            line = f"- {item_key}：{value}"
            if dimension == "boundary":
                constraint_entries.append(line)
            elif dimension == "value":
                value_entries.append(line)

        for item in questions:
            reason = str(item.get("value_reason") or "").strip()
            if reason:
                value_entries.append(f"- {item['question']}：{truncate(reason, 120)}")

        def _compact(entries: list[str], max_items: int = limit) -> list[str]:
            seen: set[str] = set()
            result: list[str] = []
            for entry in entries:
                clean = str(entry or "").strip()
                if not clean or clean in seen:
                    continue
                seen.add(clean)
                result.append(clean)
                if len(result) >= max_items:
                    break
            return result

        sections = [
            ("Environment Structure", _compact(environment_entries)),
            ("Persistent Constraints", _compact(constraint_entries)),
            ("Value Heuristics", _compact(value_entries)),
            ("Risk Patterns", _compact(risk_entries)),
            ("Strategy Abstractions", _compact(strategy_entries)),
        ]

        lines = ["# MEMORY", ""]
        for title, entries in sections:
            lines.append(f"## {title}")
            if entries:
                lines.extend(entries)
            else:
                lines.append("- 暂无条目。")
            lines.append("")
        return "\n".join(lines).strip() + "\n"
