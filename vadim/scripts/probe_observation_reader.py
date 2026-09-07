"""Recover a real recorded response after process restart using only a bookmark.

Development plumbing probe, without model or simulator calls. Source observations
already live in SQLite; no second archive of their bytes is created.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

from uptick_agent.memory.observation_reader import ObservationBookmark, StoredObservationReader
from uptick_agent.memory.stores import SqliteStructuredStore


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


async def prepare(source: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    metadata = json.loads((source / "prepare.json").read_text())
    with (
        sqlite3.connect(f"file:{(source / 'memory.sqlite3').resolve()}?mode=ro", uri=True) as src,
        sqlite3.connect(output / "memory.sqlite3") as dst,
    ):
        src.backup(dst)
    store = SqliteStructuredStore(output / "memory.sqlite3")
    records = await store.list(namespace=metadata["namespace"])
    resources = [
        r
        for r in records
        if r.record_type == "experience-transition"
        and r.payload["action"].get("kind") == "get_resources"
    ]
    record = max(resources, key=lambda r: len(canonical(r.payload["result"])))
    expected = canonical(record.payload["result"])
    iteration = record.payload["iteration"] + 1
    reader = StoredObservationReader(
        store,
        namespace=metadata["namespace"],
        run_id=record.payload["run_id"],
    )
    bookmark = await reader.bookmark(record.record_id, current_iteration=iteration)
    (output / "bookmark.json").write_text(bookmark.model_dump_json() + "\n")
    receipt = {
        "namespace": metadata["namespace"],
        "run_id": record.payload["run_id"],
        "current_iteration": iteration,
        "expected_bytes": len(expected),
        "expected_digest": hashlib.sha256(expected).hexdigest(),
        "source_record_id": record.record_id,
        "source_record_hash": record.content_hash,
        "bookmark_bytes": len(bookmark.model_dump_json().encode()),
        "source_record_count": len(records),
    }
    (output / "prepare.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


async def read(output: Path) -> None:
    destination = output / "read-report.json"
    if destination.exists():
        raise FileExistsError("read report already exists")
    metadata = json.loads((output / "prepare.json").read_text())
    bookmark = ObservationBookmark.model_validate_json((output / "bookmark.json").read_text())
    store = SqliteStructuredStore(output / "memory.sqlite3")
    reader = StoredObservationReader(
        store, namespace=metadata["namespace"], run_id=metadata["run_id"]
    )
    offset, chunks = 0, []
    while True:
        chunk = await reader.read(
            bookmark,
            current_iteration=metadata["current_iteration"],
            offset=offset,
            max_bytes=1024,
        )
        encoded = chunk["text"].encode()
        assert len(encoded) == chunk["returned_bytes"] <= 1024
        assert chunk["digest"] == metadata["expected_digest"]
        chunks.append(encoded)
        if chunk["eof"]:
            break
        assert chunk["next_offset"] > offset
        offset = chunk["next_offset"]
        assert len(chunks) <= 100, "non-progressing or unexpectedly large read"
    reconstructed = b"".join(chunks)
    assert len(reconstructed) == metadata["expected_bytes"]
    assert hashlib.sha256(reconstructed).hexdigest() == metadata["expected_digest"]
    records = await store.list(namespace=metadata["namespace"])
    assert len(records) == metadata["source_record_count"]
    report = {
        "recovered_after_process_restart": True,
        "exact_digest_verified": True,
        "response_bytes": len(reconstructed),
        "bookmark_bytes": metadata["bookmark_bytes"],
        "bounded_read_chunks": len(chunks),
        "source_record_count_unchanged": True,
        "model_or_simulator_calls": 0,
        "limitation": "Recovery mechanism only; not yet charged runner actions or model utility.",
    }
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "read"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.phase == "prepare" and args.source is None:
        parser.error("prepare requires --source")
    asyncio.run(prepare(args.source, args.output) if args.phase == "prepare" else read(args.output))
