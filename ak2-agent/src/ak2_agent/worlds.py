from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .files import atomic, digest, dumps, inside, tree


@dataclass
class World:
    path: Path
    manifest: dict

    @property
    def id(self):
        return self.path.name

    @property
    def local(self):
        return inside(self.path, ".local")

    @property
    def knowledge(self):
        return inside(self.path, "knowledge")

    @property
    def memory(self):
        return inside(self.path, ".local/experience.sqlite3")

    def require_exists(self):
        if not (self.path / "AGENTS.md").is_file():
            raise ValueError("World was removed; no files will be recreated")

    def descriptor(self):
        return {"id": self.id, "description": self.manifest["description"]}

    def version(self):
        return digest(tree(self.knowledge))

    def ready(self):
        mark = self.local / "adapted.json"
        if not mark.exists():
            return False
        value = json.loads(mark.read_text())
        return value.get("hash") == self.version() and value.get("manifest_hash", digest(self.manifest)) == digest(self.manifest)

    def publish(self, files):
        self.require_exists()
        for path, content in files.items():
            validate_artifact(path, content)
        # Called under world adaptation lock; consumers also hold that lock.
        for path in tree(self.knowledge).keys() - files.keys():
            inside(self.knowledge, path).unlink()
        for path, content in files.items():
            atomic(inside(self.knowledge, path), content, private=False)
        atomic(self.local / "adapted.json", dumps({"hash": digest(files), "manifest_hash": digest(self.manifest), "contract": 1}))


def discover(root):
    base = Path(root).resolve() / "worlds"
    result = []
    if base.is_symlink():
        raise ValueError("Worlds directory cannot be a symlink")
    for path in sorted(base.glob("*/AGENTS.md")):
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError("Worlds cannot be symlinks")
        body = path.read_text()
        match = re.search(r"```world\s*\n(.*?)\n```", body, re.S)
        if not match:
            raise ValueError(f"Missing world JSON block: {path}")
        manifest = json.loads(match[1])
        wid = path.parent.name
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", wid) or manifest.get("id") != wid:
            raise ValueError(f"Invalid world id: {wid}")
        if not isinstance(manifest.get("description"), str) or not isinstance(manifest.get("bootstrap"), str):
            raise ValueError("World requires description and bootstrap command")
        if not isinstance(manifest.get("bootstrap_replay_safe"), bool):
            raise ValueError("World must declare bootstrap_replay_safe")
        if not isinstance(manifest.get("defaults", {}), dict):
            raise ValueError("World defaults must be an object")
        result.append(World(path.parent.resolve(), manifest))
    return result


def select(root, world_id=None, task="", brain=None):
    worlds = discover(root)
    if world_id:
        matches = [w for w in worlds if w.id == world_id]
        if not matches:
            raise ValueError("World not found: " + world_id)
        return matches[0]
    if not worlds:
        raise ValueError("No installed worlds")
    if not task and len(worlds) == 1:
        return worlds[0]
    if not task or brain is None:
        raise ValueError("Specify --world or provide a task to select a world")
    route = brain.route(task, [w.descriptor() for w in worlds])
    matches = [w for w in worlds if w.id == route["world_id"]]
    if not matches:
        raise ValueError("No matching world: " + route["reason"])
    return matches[0]


def validate_artifact(path, content):
    part = Path(path)
    if (part.is_absolute() or ".." in part.parts or "__pycache__" in part.parts or not part.parts or
        part.parts[0] not in {"scripts", "skills", "prompts", "references"} or
        part.suffix not in {".py", ".md", ".json", ".txt"}):
        raise ValueError("Only scripts/, skills/, prompts/, references/ artifacts are allowed")
    if len(content.encode()) > 256_000:
        raise ValueError("Knowledge artifact exceeds 256 KB")
    if part.suffix == ".py":
        compile(content, path, "exec")


def apply_proposal(files, proposal):
    candidate = dict(files)
    for file in proposal.get("files", []):
        validate_artifact(file["path"], file["content"])
        candidate[file["path"]] = file["content"]
    for patch in proposal.get("patches", []):
        path, old = patch["path"], patch["old"]
        if path not in candidate or not old or candidate[path].count(old) != 1:
            raise ValueError("Patch must match exactly one existing fragment: " + path)
        content = candidate[path].replace(old, patch["new"], 1)
        validate_artifact(path, content)
        candidate[path] = content
    return candidate


def snapshot(source, target):
    files = tree(source)
    for name, content in files.items():
        atomic(inside(target, name), content)
    return digest(files)


def register(root, wid, description, command, replay_safe=False):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", wid):
        raise ValueError("World id must use lowercase letters, digits and hyphens")
    path = inside(Path(root) / "worlds", wid)
    if path.exists():
        raise ValueError("World already exists")
    manifest = {"id": wid, "description": description, "bootstrap": command,
                "defaults": {}, "bootstrap_replay_safe": replay_safe}
    atomic(path / "AGENTS.md", f"# {wid}\n\n```world\n{json.dumps(manifest, ensure_ascii=False, indent=2)}\n```\n", private=False)
    return path
