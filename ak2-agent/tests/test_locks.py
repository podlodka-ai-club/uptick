import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path

from ak2_agent.files import lock


class LockTests(unittest.TestCase):
    def test_shared_adaptation_waits_but_duplicate_run_lock_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"lock"
            child = subprocess.Popen([sys.executable, "-c",
                "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); "
                "print('locked',flush=True); sys.stdin.read(1)", str(path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                with self.assertRaisesRegex(ValueError,"already active"):
                    with lock(path):
                        pass
                def acquire():
                    with lock(path,wait=True):
                        return "acquired"
                future = pool.submit(acquire)
                try:
                    with self.assertRaises(TimeoutError):
                        future.result(timeout=.05)
                finally:
                    child.stdin.write("q")
                    child.stdin.flush()
                self.assertEqual(future.result(timeout=3),"acquired")
            finally:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=3)
                child.stdin.close()
                child.stdout.close()
                pool.shutdown()
