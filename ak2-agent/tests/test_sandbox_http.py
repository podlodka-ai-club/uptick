import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from ak2_agent.sandbox import Sandbox


@unittest.skipUnless(os.getenv('AK2_SANDBOX_TEST') == '1', 'Requires actual OS sandbox')
class SandboxHTTPTests(unittest.TestCase):
    def test_adapter_loads_httpx_from_environment_without_reading_other_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scripts = root / 'knowledge' / 'scripts'
            scripts.mkdir(parents=True)
            outside = root / 'outside.txt'
            outside.write_text('not available to adapter')
            program = '''import httpx, json, sys
from pathlib import Path
with httpx.Client() as client:
    pass
try:
    Path(%r).read_text()
    denied = False
except PermissionError:
    denied = True
print(json.dumps({'prefix': sys.prefix, 'httpx': httpx.__version__, 'denied': denied}))
''' % str(outside)
            (scripts / 'adapter.py').write_text(program)
            result = Sandbox(root / 'knowledge', root / 'work').adapter({})
            self.assertEqual(Path(result['prefix']).resolve(), Path(sys.prefix).resolve())
            self.assertEqual(result['httpx'], '0.28.1')
            self.assertTrue(result['denied'])
