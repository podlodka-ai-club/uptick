"""Truthful filesystem checks for the file-records benchmark adapter."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from uptick_agent.benchmarks.file_records import (
    DeduplicateRecords,
    FileRecordsDecision,
    FileRecordsEnvironment,
    NoopRecords,
    environment_content_hash,
)


def _write_records(workspace: Path, content: bytes) -> Path:
    path = workspace / "records.txt"
    path.write_bytes(content)
    return path


def test_deduplicate_rewrites_real_file_and_reports_actual_metrics(tmp_path: Path) -> None:
    records_path = _write_records(tmp_path, b"alpha\nbeta\nalpha\nbeta\n")
    environment = FileRecordsEnvironment(tmp_path, scenario_id="records-a-v1")

    async def scenario() -> None:
        session, started = await environment.start(seed=11, agent_id="test", agent_version="1")
        assert session.environment_id == started.data["environment_id"]
        assert started.format == "newline_records"
        assert started.data["line_count"] == 4
        assert started.data["unique_lines"] == 2
        assert started.data["duplicate_lines"] == 2
        assert started.data["changed"] is False
        assert [metric.name for metric in started.objective_metrics] == [
            "duplicate_lines",
            "line_count",
            "unique_lines",
        ]

        result = await environment.execute(session, DeduplicateRecords())
        assert result.ok is True
        assert result.terminal is True
        assert result.data["changed"] is True
        assert result.data["duplicate_lines"] == 0
        assert result.data["content_hash"] != started.data["content_hash"]
        assert records_path.read_bytes() == b"alpha\nbeta\n"

        final = await environment.finish(
            session,
            steps=1,
            duration_seconds=0.1,
            stop_reason="deduplication complete",
        )
        assert final.status == "completed"
        assert [(metric.name, metric.value) for metric in final.objective_metrics] == [
            ("duplicate_lines", 0.0),
            ("line_count", 2.0),
            ("unique_lines", 2.0),
        ]

    asyncio.run(scenario())


def test_noop_keeps_duplicates_and_finish_reports_failed_task(tmp_path: Path) -> None:
    records_path = _write_records(tmp_path, b"same\nsame\n")
    environment = FileRecordsEnvironment(tmp_path, scenario_id="records-counter-v1")

    async def scenario() -> None:
        session, _started = await environment.start(seed=12, agent_id="test", agent_version="1")
        result = await environment.execute(session, NoopRecords())
        assert result.ok is True
        assert result.data["changed"] is False
        assert result.data["duplicate_lines"] == 1
        assert result.terminal is True
        assert records_path.read_bytes() == b"same\nsame\n"

        final = await environment.finish(
            session,
            steps=1,
            duration_seconds=0.0,
            stop_reason="noop selected",
        )
        assert final.status == "failed"
        assert final.objective_metrics[0].name == "duplicate_lines"
        assert final.objective_metrics[0].value == 1.0

    asyncio.run(scenario())


def test_restart_reads_current_disk_state_and_not_cached_initial_state(tmp_path: Path) -> None:
    records_path = _write_records(tmp_path, b"a\na\n")
    environment = FileRecordsEnvironment(tmp_path, scenario_id="records-restart-v1")

    async def scenario() -> None:
        first, _started = await environment.start(seed=13, agent_id="test", agent_version="1")
        await environment.execute(first, DeduplicateRecords())
        assert records_path.read_bytes() == b"a\n"

        second, restarted = await environment.start(seed=14, agent_id="test", agent_version="1")
        assert second.scenario_content_hash != first.scenario_content_hash
        assert restarted.data["line_count"] == 1
        assert restarted.data["duplicate_lines"] == 0

    asyncio.run(scenario())


def test_empty_file_is_a_real_completed_zero_duplicate_scenario(tmp_path: Path) -> None:
    records_path = _write_records(tmp_path, b"")
    environment = FileRecordsEnvironment(tmp_path, scenario_id="records-empty-v1")

    async def scenario() -> None:
        session, started = await environment.start(seed=14, agent_id="test", agent_version="1")
        assert started.data["line_count"] == 0
        assert started.data["unique_lines"] == 0
        assert started.data["duplicate_lines"] == 0

        result = await environment.execute(session, DeduplicateRecords())
        assert result.data["changed"] is False
        assert records_path.read_bytes() == b""
        final = await environment.finish(
            session,
            steps=1,
            duration_seconds=0.0,
            stop_reason="empty input",
        )
        assert final.status == "completed"

    asyncio.run(scenario())


def test_identity_is_directory_independent_but_scenario_content_sensitive(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    initial = b"one\ntwo\none\n"
    _write_records(first_dir, initial)
    _write_records(second_dir, initial)

    first = FileRecordsEnvironment(first_dir, scenario_id="records-same-v1")
    second = FileRecordsEnvironment(second_dir, scenario_id="records-same-v1")
    assert first.environment_content_hash == second.environment_content_hash
    assert environment_content_hash() == first.environment_content_hash

    async def scenario() -> None:
        first_session, _ = await first.start(seed=15, agent_id="test", agent_version="1")
        second_session, _ = await second.start(seed=15, agent_id="test", agent_version="1")
        assert first_session.scenario_content_hash == second_session.scenario_content_hash
        assert first_session.workspace != second_session.workspace

        _write_records(second_dir, b"one\nthree\none\n")
        changed_session, _ = await second.start(seed=16, agent_id="test", agent_version="1")
        assert first_session.scenario_content_hash != changed_session.scenario_content_hash

    asyncio.run(scenario())


def test_actions_have_no_model_supplied_path_and_symlink_escape_is_rejected(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError):
        FileRecordsDecision.model_validate(
            {"action": {"kind": "inspect", "path": "/outside/records.txt"}}
        )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside\noutside\n")
    (workspace / "records.txt").symlink_to(outside)
    environment = FileRecordsEnvironment(workspace)

    async def scenario() -> None:
        with pytest.raises(ValueError, match="symlink"):
            await environment.start(seed=16, agent_id="test", agent_version="1")

    asyncio.run(scenario())


def test_public_state_recomputes_counts_from_disk(tmp_path: Path) -> None:
    _write_records(tmp_path, b"a\nb\na\n")
    environment = FileRecordsEnvironment(tmp_path, scenario_id="records-state-v1")

    async def scenario() -> None:
        session, _ = await environment.start(seed=17, agent_id="test", agent_version="1")
        (tmp_path / "records.txt").write_bytes(b"a\nb\n")
        state = environment.public_state(session)
        assert state["line_count"] == 2
        assert state["duplicate_lines"] == 0
        assert state["content_hash"] == hashlib.sha256(b"a\nb\n").hexdigest()

    asyncio.run(scenario())
