"""Execute world code with OS-enforced filesystem boundaries, never in-process."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .files import dumps, inside


class Sandbox:
    def __init__(self, knowledge, work, timeout=120):
        self.knowledge = Path(knowledge).resolve()
        self.work = Path(work).resolve()
        self.timeout = timeout
        self.work.mkdir(parents=True, exist_ok=True, mode=0o700)

    def command(self, argv):
        runtime = Path(sys.base_prefix).resolve()
        environment = Path(sys.prefix).resolve()
        if sys.platform == "darwin" and shutil.which("sandbox-exec"):
            profile = "(version 1)(allow default)\n"
            # Keep platform services available; deny user files, then expose only
            # interpreter files and this invocation's knowledge and scratch space.
            reads = [str(runtime), str(environment), str(self.knowledge), str(self.work)]
            exclusions = " ".join(f"(require-not (subpath {json.dumps(p)}))" for p in reads)
            profile += '(deny file-read-data (require-all (require-any (subpath "/Users") (subpath "/home") (subpath "/private/tmp") (subpath "/private/var/folders")) ' + exclusions + '))\n'
            profile += f"(deny file-write* (require-all (require-not (subpath {json.dumps(str(self.work))})) (require-not (literal \"/dev/null\"))))\n"
            return ["/usr/bin/sandbox-exec", "-p", profile, *argv]
        if sys.platform.startswith("linux") and shutil.which("bwrap"):
            args = ["bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--proc", "/proc", "--dev", "/dev"]
            for p in ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", str(runtime), str(environment), str(self.knowledge)]:
                if Path(p).exists():
                    args += ["--ro-bind", p, p]
            return [*args, "--bind", str(self.work), str(self.work), "--chdir", str(self.work), "--", *argv]
        raise RuntimeError("Script sandbox unavailable: requires macOS sandbox-exec or Linux bubblewrap")

    def run(self, argv, payload=None, extra_env=None, *, first_json=False):
        (self.work / "tmp").mkdir(exist_ok=True)
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
               "TMPDIR": str(self.work / "tmp"), **(extra_env or {})}
        output, errors = self.work / ".stdout", self.work / ".stderr"
        proc = None
        try:
            with output.open("wb") as out, errors.open("wb") as err:
                proc = subprocess.Popen(self.command(argv), stdin=subprocess.PIPE,
                    stdout=out, stderr=err, cwd=self.work, env=env, start_new_session=True)
                proc.stdin.write(dumps(payload or {}).encode())
                proc.stdin.close()
                deadline = time.monotonic() + self.timeout
                while proc.poll() is None:
                    if first_json and output.stat().st_size:
                        try:
                            # Some discovery commands receive a full JSON document
                            # on a connection that never closes. Persist that document.
                            return json.loads(output.read_text())
                        except (ValueError, UnicodeDecodeError):
                            pass
                    if time.monotonic() > deadline:
                        raise TimeoutError("World script timed out; request remains pending")
                    if output.stat().st_size + errors.stat().st_size > 8_000_000:
                        raise ValueError("World script exceeded 8 MB output limit")
                    time.sleep(.05)
            raw = output.read_text(errors="replace")
            stderr = errors.read_text(errors="replace")
            if first_json:
                try:
                    return json.loads(raw)
                except ValueError:
                    pass
            if proc.returncode:
                raise RuntimeError(f"World script exit {proc.returncode}: {stderr[:3000]}")
            if len(raw.encode()) > 8_000_000:
                raise ValueError("World script exceeded output limit")
            return json.loads(raw)
        finally:
            if proc is not None:
                # Also terminate descendants left behind by an otherwise finished parent.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            output.unlink(missing_ok=True)
            errors.unlink(missing_ok=True)

    def adapter(self, payload):
        entry = inside(self.knowledge, "scripts/adapter.py")
        if not entry.is_file():
            raise ValueError("Missing scripts/adapter.py")
        return self.run([sys.executable, "-I", str(entry)], payload)

    def history(self, payload):
        if payload.get("operation") not in {"discover", "read"}:
            raise ValueError("Historical interface only supports discover/read")
        entry = inside(self.knowledge, "scripts/history.py")
        if not entry.is_file():
            raise ValueError("Missing scripts/history.py")
        return self.run([sys.executable, "-I", str(entry)], payload)
