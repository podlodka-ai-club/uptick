from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def atomic(path, value, *, private=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(value)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, 0o600 if private else 0o644)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def inside(root, relative):
    root = Path(root).resolve()
    part = Path(relative)
    if part.is_absolute() or ".." in part.parts or not part.parts:
        raise ValueError("Path must be relative and stay inside its world")
    target = root / part
    for parent in [target, *target.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("Symlinks are not supported in world data")
    if not target.resolve().is_relative_to(root):
        raise ValueError("Path escapes its world")
    return target


def tree(root):
    root = Path(root)
    result = {}
    if root.exists():
        for p in sorted(root.rglob("*")):
            if p.is_symlink():
                raise ValueError("Symlinks are not supported in knowledge")
            relative = p.relative_to(root)
            if "__pycache__" in relative.parts or p.suffix in {".pyc", ".pyo"}:
                continue
            if p.is_file():
                result[relative.as_posix()] = p.read_text()
    return result


def digest(files):
    return hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def lock(path, *, wait=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        announced = False
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as e:
                if not wait:
                    raise ValueError("This world operation/run is already active") from e
                if not announced:
                    emit("waiting_for_lock", path=str(path))
                    announced = True
                time.sleep(.2)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


SECRET = re.compile(r"password|passwd|secret|token|authorization|(?:^|_)auth$|api_key", re.I)


def redact(value):
    if isinstance(value, dict):
        return {k: "[private]" if SECRET.search(k) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"(?i)((?:password|пароль|authorization)\s*[:=]\s*)[^\s,;]+", r"\1[private]", value)
    return value


def emit(event, **data):
    print(dumps(redact({"event": event, **data})), flush=True)
