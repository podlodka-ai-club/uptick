from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from pathlib import Path
from time import monotonic
from typing import cast
from uuid import uuid4

from uptick_agent.core.memory_models import (
    ConsolidationBatch,
    ConsolidationQuery,
    EpisodeRecord,
    LessonRecord,
    MemoryCommit,
    MemoryPacket,
    MemoryQuery,
    MemoryRecordKind,
    MemoryView,
    MemoryViewRequest,
    RecalledMemory,
    RetrievalDiagnostics,
)
from uptick_agent.core.models import MemoryBrief, RecalledLesson

_SCHEMA_VERSION = 4
_TOKEN = re.compile(r"[A-Za-z0-9_]{2,}")
_EPISODE_SUMMARY_LIMIT = 600
_PER_EVIDENCE_GROUP_LIMIT = 2
_LESSON_RECALL_LIMIT = 2


class SQLiteMemoryError(RuntimeError):
    """The durable memory database violates the canonical storage contract."""


class MemoryRevisionConflict(SQLiteMemoryError):
    """A writer attempted to commit against a stale active revision."""


class SQLiteMemory:
    """Single-writer, revisioned experiential memory with deterministic retrieval."""

    def __init__(self, path: str | Path, *, monotonic_fn=monotonic) -> None:
        self.path = Path(path)
        self._monotonic = monotonic_fn
        self._initialized = False
        self._initialization_lock = asyncio.Lock()

    async def resolve_view(self, request: MemoryViewRequest) -> MemoryView:
        await self._ensure_initialized()
        return await asyncio.to_thread(self._resolve_view, request)

    async def recall(self, query: MemoryQuery) -> MemoryPacket:
        await self._ensure_initialized()
        started = self._monotonic()
        packet = await asyncio.to_thread(self._recall, query)
        duration = max(0.0, self._monotonic() - started)
        diagnostics = packet.diagnostics
        if diagnostics is not None:
            packet = packet.model_copy(
                update={
                    "diagnostics": diagnostics.model_copy(update={"duration_seconds": duration})
                },
                deep=True,
            )
        return packet

    async def load_consolidation_batch(
        self,
        query: ConsolidationQuery,
    ) -> ConsolidationBatch:
        await self._ensure_initialized()
        return await asyncio.to_thread(self._load_consolidation_batch, query)

    async def record_episode(
        self,
        episode: EpisodeRecord,
        base_revision: int,
    ) -> MemoryCommit:
        await self._ensure_initialized()
        return await asyncio.to_thread(
            self._activate_record,
            "episode",
            episode,
            base_revision,
        )

    async def activate_lesson(
        self,
        lesson: LessonRecord,
        base_revision: int,
    ) -> MemoryCommit:
        await self._ensure_initialized()
        return await asyncio.to_thread(
            self._activate_record,
            "lesson",
            lesson,
            base_revision,
        )

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._initialization_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize)
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            has_tables = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
            ).fetchone()
            if version not in {0, _SCHEMA_VERSION}:
                raise SQLiteMemoryError(f"unsupported SQLiteMemory schema version {version}")
            if version == 0 and has_tables is not None:
                raise SQLiteMemoryError("unversioned SQLiteMemory database is not supported")
            if version == 0:
                self._create_schema(connection)
            self._validate_schema(connection)

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        database_id = f"memory-{uuid4()}"
        try:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE memory_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                ) STRICT;
                CREATE TABLE memory_heads (
                    environment_id TEXT NOT NULL,
                    profile_version TEXT NOT NULL,
                    current_revision INTEGER NOT NULL CHECK (current_revision >= 0),
                    PRIMARY KEY (environment_id, profile_version)
                ) STRICT;
                CREATE TABLE memory_revisions (
                    environment_id TEXT NOT NULL,
                    profile_version TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 0),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (environment_id, profile_version, revision)
                ) STRICT;
                CREATE TABLE memory_records (
                    record_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('episode', 'lesson')),
                    environment_id TEXT NOT NULL,
                    profile_version TEXT NOT NULL,
                    canonical_json TEXT NOT NULL,
                    search_summary TEXT NOT NULL,
                    verification_status TEXT,
                    evidence_group_id TEXT,
                    run_id TEXT,
                    step INTEGER,
                    activated_revision INTEGER NOT NULL CHECK (activated_revision >= 1)
                ) STRICT;
                CREATE TABLE active_membership (
                    record_id TEXT NOT NULL REFERENCES memory_records(record_id),
                    environment_id TEXT NOT NULL,
                    profile_version TEXT NOT NULL,
                    start_revision INTEGER NOT NULL CHECK (start_revision >= 1),
                    end_revision INTEGER CHECK (end_revision > start_revision),
                    PRIMARY KEY (record_id, start_revision)
                ) STRICT;
                CREATE TABLE memory_revision_changes (
                    environment_id TEXT NOT NULL,
                    profile_version TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    record_id TEXT NOT NULL REFERENCES memory_records(record_id),
                    change_kind TEXT NOT NULL CHECK (change_kind IN ('activate', 'deactivate')),
                    PRIMARY KEY (environment_id, profile_version, revision, record_id, change_kind),
                    FOREIGN KEY (environment_id, profile_version, revision)
                        REFERENCES memory_revisions(environment_id, profile_version, revision)
                ) STRICT;
                CREATE TABLE capability_links (
                    record_id TEXT NOT NULL REFERENCES memory_records(record_id),
                    capability_name TEXT NOT NULL,
                    PRIMARY KEY (record_id, capability_name)
                ) STRICT;
                CREATE TABLE lesson_episode_links (
                    lesson_id TEXT NOT NULL REFERENCES memory_records(record_id),
                    episode_id TEXT NOT NULL REFERENCES memory_records(record_id),
                    relation TEXT NOT NULL CHECK (relation IN ('evidence', 'contradiction')),
                    PRIMARY KEY (lesson_id, episode_id, relation)
                ) STRICT;
                CREATE TABLE lesson_supersedes (
                    lesson_id TEXT NOT NULL REFERENCES memory_records(record_id),
                    superseded_lesson_id TEXT NOT NULL REFERENCES memory_records(record_id),
                    PRIMARY KEY (lesson_id, superseded_lesson_id)
                ) STRICT;
                CREATE INDEX membership_view_idx ON active_membership (
                    environment_id, profile_version, start_revision, end_revision
                );
                CREATE INDEX records_scope_idx ON memory_records (
                    environment_id, profile_version, kind, activated_revision
                );
                CREATE INDEX records_run_idx ON memory_records (run_id, step);
                CREATE VIRTUAL TABLE memory_fts USING fts5(
                    record_id UNINDEXED,
                    search_summary,
                    tokenize='unicode61'
                );
                """
            )
            connection.execute(
                "INSERT INTO memory_metadata(key, value) VALUES ('database_id', ?)",
                (database_id,),
            )
            connection.execute(
                "INSERT INTO memory_metadata(key, value) VALUES ('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            connection.execute("COMMIT")
        except sqlite3.DatabaseError as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise SQLiteMemoryError(f"cannot initialize SQLiteMemory: {error}") from error

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        required = {
            "memory_metadata",
            "memory_heads",
            "memory_revisions",
            "memory_records",
            "active_membership",
            "memory_revision_changes",
            "capability_links",
            "lesson_episode_links",
            "lesson_supersedes",
            "memory_fts",
        }
        present = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        missing = sorted(required - present)
        if missing:
            raise SQLiteMemoryError("SQLiteMemory schema is incomplete: " + ", ".join(missing))
        metadata = dict(connection.execute("SELECT key, value FROM memory_metadata"))
        if metadata.get("schema_version") != str(_SCHEMA_VERSION):
            raise SQLiteMemoryError("SQLiteMemory metadata schema version mismatch")
        if not metadata.get("database_id"):
            raise SQLiteMemoryError("SQLiteMemory database identity is missing")
        try:
            connection.execute(
                "SELECT rowid FROM memory_fts WHERE memory_fts MATCH 'availability' LIMIT 1"
            ).fetchall()
        except sqlite3.DatabaseError as error:
            raise SQLiteMemoryError("SQLite FTS5 is unavailable") from error

    def _resolve_view(self, request: MemoryViewRequest) -> MemoryView:
        with self._connect() as connection:
            database_id = self._database_id(connection)
            if (
                request.expected_database_id is not None
                and request.expected_database_id != database_id
            ):
                raise SQLiteMemoryError("requested memory database identity does not match")
            connection.execute("BEGIN IMMEDIATE")
            try:
                head = connection.execute(
                    """
                    SELECT current_revision FROM memory_heads
                    WHERE environment_id = ? AND profile_version = ?
                    """,
                    (request.environment_id, request.environment_profile_version),
                ).fetchone()
                if head is None:
                    connection.execute(
                        """
                        INSERT INTO memory_heads(environment_id, profile_version, current_revision)
                        VALUES (?, ?, 0)
                        """,
                        (request.environment_id, request.environment_profile_version),
                    )
                    connection.execute(
                        """
                        INSERT INTO memory_revisions(environment_id, profile_version, revision)
                        VALUES (?, ?, 0)
                        """,
                        (request.environment_id, request.environment_profile_version),
                    )
                    current_revision = 0
                else:
                    current_revision = int(head[0])
                revision = (
                    request.expected_revision
                    if request.expected_revision is not None
                    else current_revision
                )
                exists = connection.execute(
                    """
                    SELECT 1 FROM memory_revisions
                    WHERE environment_id = ? AND profile_version = ? AND revision = ?
                    """,
                    (request.environment_id, request.environment_profile_version, revision),
                ).fetchone()
                if exists is None:
                    raise SQLiteMemoryError("requested memory revision is unavailable")
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return MemoryView(database_id=database_id, revision=revision)

    def _recall(self, query: MemoryQuery) -> MemoryPacket:
        with self._connect() as connection:
            revision = self._validate_view(
                connection,
                query.view,
                environment_id=query.environment_id,
                profile_version=query.environment_profile_version,
            )
            if query.limit == 0:
                return MemoryPacket(
                    view=query.view,
                    diagnostics=RetrievalDiagnostics(),
                )
            lesson_candidates = self._candidate_rows(
                connection,
                query,
                revision,
                kind="lesson",
            )
            episode_candidates = self._candidate_rows(
                connection,
                query,
                revision,
                kind="episode",
            )
            ranked_lessons = sorted(lesson_candidates.values(), key=_candidate_sort_key)
            ranked_episodes = sorted(episode_candidates.values(), key=_candidate_sort_key)
            selected = _select_candidates(
                lessons=ranked_lessons,
                episodes=ranked_episodes,
                limit=query.limit,
            )
            records = [
                RecalledMemory(
                    record_id=row.record_id,
                    kind=cast(MemoryRecordKind, row.kind),
                    summary=row.summary,
                    score=1.0 / (index + 1),
                )
                for index, row in enumerate(selected)
            ]
            brief = self._pack_brief(connection, selected, revision)
            selected_ids = [row.record_id for row in selected]
            ranked = [*ranked_lessons, *ranked_episodes]
            candidate_ids = list(
                dict.fromkeys([*selected_ids, *(row.record_id for row in ranked)])
            )[:64]
            return MemoryPacket(
                view=query.view,
                records=records,
                brief=brief,
                diagnostics=RetrievalDiagnostics(
                    candidate_record_ids=candidate_ids,
                    selected_record_ids=selected_ids,
                    excluded_record_ids=[
                        record_id
                        for record_id in candidate_ids
                        if record_id not in set(selected_ids)
                    ],
                ),
            )

    def _candidate_rows(
        self,
        connection: sqlite3.Connection,
        query: MemoryQuery,
        revision: int,
        *,
        kind: MemoryRecordKind,
    ) -> dict[str, _Candidate]:
        candidates: dict[str, _Candidate] = {}
        tokens = _query_tokens(query)
        if tokens:
            expression = " OR ".join(f'"{token}"' for token in tokens)
            rows = connection.execute(
                """
                SELECT r.*, bm25(memory_fts) AS text_rank
                FROM memory_fts
                JOIN memory_records r ON r.record_id = memory_fts.record_id
                JOIN active_membership m ON m.record_id = r.record_id
                WHERE memory_fts MATCH ?
                  AND r.kind = ?
                  AND m.environment_id = ? AND m.profile_version = ?
                  AND m.start_revision <= ?
                  AND (m.end_revision IS NULL OR m.end_revision > ?)
                ORDER BY text_rank, r.activated_revision DESC, r.record_id
                LIMIT 64
                """,
                (
                    expression,
                    kind,
                    query.environment_id,
                    query.environment_profile_version,
                    revision,
                    revision,
                ),
            )
            for row in rows:
                candidate = self._candidate(connection, row, query.capability_names)
                candidate.text_rank = float(row["text_rank"])
                candidates[candidate.record_id] = candidate

        if query.capability_names:
            placeholders = ",".join("?" for _ in query.capability_names)
            rows = connection.execute(
                f"""
                SELECT DISTINCT r.*
                FROM capability_links c
                JOIN memory_records r ON r.record_id = c.record_id
                JOIN active_membership m ON m.record_id = r.record_id
                WHERE c.capability_name IN ({placeholders})
                  AND r.kind = ?
                  AND m.environment_id = ? AND m.profile_version = ?
                  AND m.start_revision <= ?
                  AND (m.end_revision IS NULL OR m.end_revision > ?)
                ORDER BY r.activated_revision DESC, r.record_id
                LIMIT 64
                """,
                (
                    *query.capability_names,
                    kind,
                    query.environment_id,
                    query.environment_profile_version,
                    revision,
                    revision,
                ),
            )
            for row in rows:
                candidate = self._candidate(connection, row, query.capability_names)
                existing = candidates.get(candidate.record_id)
                if existing is None:
                    candidates[candidate.record_id] = candidate
                else:
                    existing.capability_overlap = max(
                        existing.capability_overlap,
                        candidate.capability_overlap,
                    )
        return candidates

    def _candidate(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        query_capabilities: list[str],
    ) -> _Candidate:
        capabilities = {
            str(item[0])
            for item in connection.execute(
                "SELECT capability_name FROM capability_links WHERE record_id = ?",
                (row["record_id"],),
            )
        }
        kind = str(row["kind"])
        verification = row["verification_status"]
        verified_category = 2 if kind == "lesson" else (1 if verification == "confirmed" else 0)
        return _Candidate(
            record_id=str(row["record_id"]),
            kind=kind,
            canonical_json=str(row["canonical_json"]),
            summary=str(row["search_summary"]),
            capability_overlap=len(capabilities & set(query_capabilities)),
            verified_category=verified_category,
            verification_status=str(verification) if verification is not None else None,
            evidence_group_id=(
                str(row["evidence_group_id"]) if row["evidence_group_id"] is not None else None
            ),
            text_rank=float("inf"),
            activated_revision=int(row["activated_revision"]),
        )

    def _pack_brief(
        self,
        connection: sqlite3.Connection,
        selected: list[_Candidate],
        revision: int,
    ) -> MemoryBrief:
        lessons: list[RecalledLesson | str] = []
        episodes: list[str] = []
        contradictions: list[str] = []
        for candidate in selected:
            if candidate.kind == "lesson":
                lesson = LessonRecord.model_validate_json(candidate.canonical_json)
                recalled = _lesson_brief(lesson)
                if recalled not in lessons and len(lessons) < 4:
                    lessons.append(recalled)
                for episode_id in lesson.contradicting_episode_ids:
                    if len(contradictions) == 4:
                        break
                    row = connection.execute(
                        """
                        SELECT r.search_summary
                        FROM memory_records r
                        JOIN active_membership m ON m.record_id = r.record_id
                        WHERE r.record_id = ? AND m.start_revision <= ?
                          AND (m.end_revision IS NULL OR m.end_revision > ?)
                        """,
                        (episode_id, revision, revision),
                    ).fetchone()
                    if row is not None and str(row[0]) not in contradictions:
                        contradictions.append(str(row[0]))
                continue
            episode = EpisodeRecord.model_validate_json(candidate.canonical_json)
            if episode.verification.status == "contradicted":
                if candidate.summary not in contradictions and len(contradictions) < 4:
                    contradictions.append(candidate.summary)
            elif candidate.summary not in episodes and len(episodes) < 4:
                episodes.append(candidate.summary)
        return MemoryBrief(
            lessons=lessons,
            similar_episodes=episodes,
            contradictions=contradictions,
        )

    def _load_consolidation_batch(self, query: ConsolidationQuery) -> ConsolidationBatch:
        with self._connect() as connection:
            revision = self._validate_view(
                connection,
                query.view,
                environment_id=query.environment_id,
                profile_version=query.environment_profile_version,
            )
            eligible_episode_rows = connection.execute(
                """
                SELECT
                    r.canonical_json,
                    r.activated_revision,
                    r.evidence_group_id,
                    r.step,
                    r.record_id,
                    r.verification_status
                FROM memory_records r
                JOIN active_membership em ON em.record_id = r.record_id
                WHERE r.kind = 'episode'
                  AND r.environment_id = ? AND r.profile_version = ?
                  AND em.start_revision <= ?
                  AND (em.end_revision IS NULL OR em.end_revision > ?)
                  AND (? IS NULL OR r.record_id = ?)
                  AND NOT EXISTS (
                    SELECT 1
                    FROM lesson_episode_links le
                    JOIN active_membership lm ON lm.record_id = le.lesson_id
                    WHERE le.episode_id = r.record_id
                      AND le.relation = 'evidence'
                      AND lm.start_revision <= ?
                      AND (lm.end_revision IS NULL OR lm.end_revision > ?)
                  )
                ORDER BY r.activated_revision, r.evidence_group_id, r.step, r.record_id
                """,
                (
                    query.environment_id,
                    query.environment_profile_version,
                    revision,
                    revision,
                    query.trigger_episode_id,
                    query.trigger_episode_id,
                    revision,
                    revision,
                ),
            ).fetchall()
            lesson_rows = connection.execute(
                """
                SELECT r.canonical_json
                FROM memory_records r
                JOIN active_membership m ON m.record_id = r.record_id
                WHERE r.kind = 'lesson'
                  AND r.environment_id = ? AND r.profile_version = ?
                  AND m.start_revision <= ?
                  AND (m.end_revision IS NULL OR m.end_revision > ?)
                ORDER BY r.activated_revision DESC, r.record_id
                LIMIT ?
                """,
                (
                    query.environment_id,
                    query.environment_profile_version,
                    revision,
                    revision,
                    query.lesson_limit,
                ),
            ).fetchall()
        episode_rows = _select_consolidation_rows(
            eligible_episode_rows,
            limit=query.episode_limit,
        )
        return ConsolidationBatch(
            query=query,
            episodes=[
                EpisodeRecord.model_validate_json(row["canonical_json"]) for row in episode_rows
            ],
            active_lessons=[LessonRecord.model_validate_json(row[0]) for row in lesson_rows],
        )

    def _activate_record(
        self,
        kind: str,
        record: EpisodeRecord | LessonRecord,
        base_revision: int,
    ) -> MemoryCommit:
        canonical = _canonical_json(record.model_dump(mode="json"))
        environment_id = record.environment_id
        profile_version = record.environment_profile_version
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT canonical_json FROM memory_records WHERE record_id = ?",
                    (record.record_id,),
                ).fetchone()
                current = self._current_revision(
                    connection,
                    environment_id=environment_id,
                    profile_version=profile_version,
                )
                if existing is not None:
                    if str(existing["canonical_json"]) != canonical:
                        raise SQLiteMemoryError(
                            f"memory record ID collision for {record.record_id!r}"
                        )
                    connection.execute("COMMIT")
                    return MemoryCommit(
                        applied=False,
                        record_ids=[record.record_id],
                        previous_revision=current,
                        new_revision=current,
                        reason="identical record already exists",
                    )
                if current != base_revision:
                    raise MemoryRevisionConflict(
                        f"stale memory revision {base_revision}; current revision is {current}"
                    )
                new_revision = current + 1
                if isinstance(record, LessonRecord):
                    self._validate_lesson_links(connection, record, current)
                self._insert_record(
                    connection,
                    kind=kind,
                    record=record,
                    canonical=canonical,
                    activated_revision=new_revision,
                )
                connection.execute(
                    """
                    INSERT INTO memory_revisions(environment_id, profile_version, revision)
                    VALUES (?, ?, ?)
                    """,
                    (environment_id, profile_version, new_revision),
                )
                connection.execute(
                    """
                    INSERT INTO active_membership(
                        record_id, environment_id, profile_version, start_revision
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (record.record_id, environment_id, profile_version, new_revision),
                )
                connection.execute(
                    """
                    INSERT INTO memory_revision_changes(
                        environment_id, profile_version, revision, record_id, change_kind
                    ) VALUES (?, ?, ?, ?, 'activate')
                    """,
                    (environment_id, profile_version, new_revision, record.record_id),
                )
                changed_ids = [record.record_id]
                if isinstance(record, LessonRecord):
                    for superseded in record.supersedes:
                        connection.execute(
                            """
                            UPDATE active_membership SET end_revision = ?
                            WHERE record_id = ? AND end_revision IS NULL
                            """,
                            (new_revision, superseded),
                        )
                        connection.execute(
                            """
                            INSERT INTO memory_revision_changes(
                                environment_id, profile_version, revision,
                                record_id, change_kind
                            ) VALUES (?, ?, ?, ?, 'deactivate')
                            """,
                            (environment_id, profile_version, new_revision, superseded),
                        )
                        connection.execute(
                            """
                            INSERT INTO lesson_supersedes(lesson_id, superseded_lesson_id)
                            VALUES (?, ?)
                            """,
                            (record.record_id, superseded),
                        )
                        changed_ids.append(superseded)
                    self._insert_lesson_links(connection, record)
                connection.execute(
                    """
                    UPDATE memory_heads SET current_revision = ?
                    WHERE environment_id = ? AND profile_version = ?
                    """,
                    (new_revision, environment_id, profile_version),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return MemoryCommit(
            applied=True,
            record_ids=changed_ids,
            previous_revision=current,
            new_revision=new_revision,
            reason=f"activated {kind}",
        )

    def _insert_record(
        self,
        connection: sqlite3.Connection,
        *,
        kind: str,
        record: EpisodeRecord | LessonRecord,
        canonical: str,
        activated_revision: int,
    ) -> None:
        if isinstance(record, EpisodeRecord):
            summary = _episode_summary(record)
            verification = record.verification.status
            evidence_group_id = record.evidence_group_id
            run_id = record.run_id
            step = record.step
            capabilities = [record.selected_action.name]
        else:
            summary = record.claim
            verification = "confirmed"
            evidence_group_id = None
            run_id = None
            step = None
            capabilities = record.capability_names
        connection.execute(
            """
            INSERT INTO memory_records(
                record_id, kind, environment_id, profile_version, canonical_json,
                search_summary, verification_status,
                evidence_group_id, run_id, step, activated_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.record_id,
                kind,
                record.environment_id,
                record.environment_profile_version,
                canonical,
                summary,
                verification,
                evidence_group_id,
                run_id,
                step,
                activated_revision,
            ),
        )
        connection.execute(
            "INSERT INTO memory_fts(record_id, search_summary) VALUES (?, ?)",
            (record.record_id, summary),
        )
        connection.executemany(
            "INSERT INTO capability_links(record_id, capability_name) VALUES (?, ?)",
            [(record.record_id, name) for name in sorted(set(capabilities))],
        )

    def _validate_lesson_links(
        self,
        connection: sqlite3.Connection,
        lesson: LessonRecord,
        revision: int,
    ) -> None:
        for episode_id in [
            *lesson.evidence_episode_ids,
            *lesson.contradicting_episode_ids,
        ]:
            self._require_active_record(
                connection,
                record_id=episode_id,
                kind="episode",
                environment_id=lesson.environment_id,
                profile_version=lesson.environment_profile_version,
                revision=revision,
            )
        for lesson_id in lesson.supersedes:
            self._require_active_record(
                connection,
                record_id=lesson_id,
                kind="lesson",
                environment_id=lesson.environment_id,
                profile_version=lesson.environment_profile_version,
                revision=revision,
            )

    def _require_active_record(
        self,
        connection: sqlite3.Connection,
        *,
        record_id: str,
        kind: str,
        environment_id: str,
        profile_version: str,
        revision: int,
    ) -> None:
        row = connection.execute(
            """
            SELECT 1
            FROM memory_records r
            JOIN active_membership m ON m.record_id = r.record_id
            WHERE r.record_id = ? AND r.kind = ?
              AND r.environment_id = ? AND r.profile_version = ?
              AND m.start_revision <= ?
              AND (m.end_revision IS NULL OR m.end_revision > ?)
            """,
            (
                record_id,
                kind,
                environment_id,
                profile_version,
                revision,
                revision,
            ),
        ).fetchone()
        if row is None:
            raise SQLiteMemoryError(
                f"referenced {kind} {record_id!r} is not active in the base view"
            )

    def _insert_lesson_links(
        self,
        connection: sqlite3.Connection,
        lesson: LessonRecord,
    ) -> None:
        connection.executemany(
            """
            INSERT INTO lesson_episode_links(lesson_id, episode_id, relation)
            VALUES (?, ?, 'evidence')
            """,
            [(lesson.record_id, episode_id) for episode_id in lesson.evidence_episode_ids],
        )
        connection.executemany(
            """
            INSERT INTO lesson_episode_links(lesson_id, episode_id, relation)
            VALUES (?, ?, 'contradiction')
            """,
            [(lesson.record_id, episode_id) for episode_id in lesson.contradicting_episode_ids],
        )

    def _validate_view(
        self,
        connection: sqlite3.Connection,
        view: MemoryView,
        *,
        environment_id: str,
        profile_version: str,
    ) -> int:
        database_id = self._database_id(connection)
        if view.database_id != database_id or view.revision is None:
            raise SQLiteMemoryError("memory view does not belong to this database")
        exists = connection.execute(
            """
            SELECT 1 FROM memory_revisions
            WHERE environment_id = ? AND profile_version = ? AND revision = ?
            """,
            (environment_id, profile_version, view.revision),
        ).fetchone()
        if exists is None:
            raise SQLiteMemoryError("memory view revision is unavailable for this profile")
        return view.revision

    def _current_revision(
        self,
        connection: sqlite3.Connection,
        *,
        environment_id: str,
        profile_version: str,
    ) -> int:
        row = connection.execute(
            """
            SELECT current_revision FROM memory_heads
            WHERE environment_id = ? AND profile_version = ?
            """,
            (environment_id, profile_version),
        ).fetchone()
        if row is None:
            raise SQLiteMemoryError("memory profile view must be resolved before writing")
        return int(row[0])

    @staticmethod
    def _database_id(connection: sqlite3.Connection) -> str:
        row = connection.execute(
            "SELECT value FROM memory_metadata WHERE key = 'database_id'"
        ).fetchone()
        if row is None:
            raise SQLiteMemoryError("memory database identity is missing")
        return str(row[0])


class _Candidate:
    __slots__ = (
        "record_id",
        "kind",
        "canonical_json",
        "summary",
        "capability_overlap",
        "verified_category",
        "verification_status",
        "evidence_group_id",
        "text_rank",
        "activated_revision",
    )

    def __init__(
        self,
        *,
        record_id: str,
        kind: str,
        canonical_json: str,
        summary: str,
        capability_overlap: int,
        verified_category: int,
        verification_status: str | None,
        evidence_group_id: str | None,
        text_rank: float,
        activated_revision: int,
    ) -> None:
        self.record_id = record_id
        self.kind = kind
        self.canonical_json = canonical_json
        self.summary = summary
        self.capability_overlap = capability_overlap
        self.verified_category = verified_category
        self.verification_status = verification_status
        self.evidence_group_id = evidence_group_id
        self.text_rank = text_rank
        self.activated_revision = activated_revision


def _select_consolidation_rows(
    rows: list[sqlite3.Row],
    *,
    limit: int,
) -> list[sqlite3.Row]:
    """Select bounded, cross-run evidence without biasing every world to its first steps."""

    groups: dict[str, list[sqlite3.Row]] = {}
    group_order: list[str] = []
    for row in rows:
        group_id = str(row["evidence_group_id"])
        if group_id not in groups:
            groups[group_id] = []
            group_order.append(group_id)
        groups[group_id].append(row)

    quota = {group_id: 0 for group_id in group_order}
    remaining = min(limit, len(rows))
    while remaining:
        allocated = False
        for group_id in group_order:
            if quota[group_id] == len(groups[group_id]):
                continue
            quota[group_id] += 1
            remaining -= 1
            allocated = True
            if remaining == 0:
                break
        if not allocated:
            break

    selected_by_group = {
        group_id: _representative_group_rows(groups[group_id], quota[group_id])
        for group_id in group_order
        if quota[group_id]
    }
    selected: list[sqlite3.Row] = []
    for rank in range(max(quota.values(), default=0)):
        for group_id in group_order:
            group_rows = selected_by_group.get(group_id, [])
            if rank < len(group_rows):
                selected.append(group_rows[rank])
    return selected


def _representative_group_rows(
    rows: list[sqlite3.Row],
    limit: int,
) -> list[sqlite3.Row]:
    if limit >= len(rows):
        return rows

    non_confirmed = [
        index for index, row in enumerate(rows) if row["verification_status"] != "confirmed"
    ]
    if len(non_confirmed) >= limit:
        selected = _evenly_spaced_indices(non_confirmed, limit)
    else:
        selected = list(non_confirmed)
        candidates = [index for index in range(len(rows)) if index not in set(selected)]
        selected.extend(_evenly_spaced_indices(candidates, limit - len(selected)))
    return [rows[index] for index in sorted(selected)]


def _evenly_spaced_indices(indices: list[int], count: int) -> list[int]:
    if count <= 0:
        return []
    if count >= len(indices):
        return indices
    if count == 1:
        return [indices[len(indices) // 2]]
    last = len(indices) - 1
    return [indices[round(position * last / (count - 1))] for position in range(count)]


def _candidate_sort_key(value: _Candidate) -> tuple[int, float, int, int, str]:
    return (
        -value.capability_overlap,
        value.text_rank,
        -value.verified_category,
        -value.activated_revision,
        value.record_id,
    )


def _episode_summary(episode: EpisodeRecord) -> str:
    arguments = _canonical_json(episode.selected_action.arguments)
    expected = " | ".join(episode.expected_result)
    evidence = " | ".join(episode.verification.evidence)
    fields = [
        f"situation={_clip(episode.situation_summary, 200)}",
        f"action={_clip(f'{episode.selected_action.name}({arguments})', 120)}",
        f"expected={_clip(expected, 150)}",
        f"outcome={_clip(episode.observation_summary, 180)}",
        (f"assessment={episode.verification.status}:{_clip(evidence, 100)}"),
    ]
    return "; ".join(fields)[:_EPISODE_SUMMARY_LIMIT]


def _query_tokens(query: MemoryQuery) -> list[str]:
    values = [query.text, *_string_values(query.signals), query.objective]
    tokens: list[str] = []
    seen: set[str] = set()
    for value in values:
        for match in _TOKEN.finditer(value.casefold()):
            token = match.group(0)
            if token in seen:
                continue
            seen.add(token)
            tokens.append(token)
            if len(tokens) == 24:
                return tokens
    return tokens


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _string_values(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _string_values(child)]
    return []


def _select_candidates(
    *,
    lessons: list[_Candidate],
    episodes: list[_Candidate],
    limit: int,
) -> list[_Candidate]:
    if limit == 0:
        return []
    lesson_limit = min(_LESSON_RECALL_LIMIT, max(1, limit // 4))
    reserved = list(lessons[:lesson_limit])
    contradiction = next(
        (item for item in episodes if item.verification_status == "contradicted"),
        None,
    )
    if contradiction is not None and len(reserved) < limit:
        reserved.append(contradiction)

    selected: list[_Candidate] = []
    group_counts: dict[str, int] = {}
    selected_summaries: set[tuple[str, str]] = set()
    for candidate in [*reserved, *episodes]:
        if candidate in selected:
            continue
        summary = candidate.summary
        if candidate.kind == "lesson":
            lesson = LessonRecord.model_validate_json(candidate.canonical_json)
            summary = _canonical_json(_lesson_brief(lesson).model_dump(mode="json"))
        summary_key = (candidate.kind, summary)
        if summary_key in selected_summaries:
            continue
        group_id = candidate.evidence_group_id
        at_group_limit = (
            group_id is not None and group_counts.get(group_id, 0) >= _PER_EVIDENCE_GROUP_LIMIT
        )
        if at_group_limit and candidate not in reserved:
            continue
        selected.append(candidate)
        selected_summaries.add(summary_key)
        if group_id is not None:
            group_counts[group_id] = group_counts.get(group_id, 0) + 1
        if len(selected) == limit:
            break
    return selected


def _lesson_brief(lesson: LessonRecord) -> RecalledLesson:
    return RecalledLesson(
        claim=lesson.claim, applies_when=lesson.applies_when, exceptions=lesson.exceptions
    )


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
