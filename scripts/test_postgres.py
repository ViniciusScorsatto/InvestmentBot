"""Run integration tests against TEST_DATABASE_URL, or an isolated temporary Postgres."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
server = None
try:
    env = os.environ.copy()
    if not env.get("TEST_DATABASE_URL"):
        import pixeltable_pgserver
        directory = tempfile.mkdtemp(prefix="investmentbot-pg-")
        server = pixeltable_pgserver.get_server(directory, cleanup_mode="delete")
        env["TEST_DATABASE_URL"] = server.get_uri()
    result = subprocess.run([sys.executable,"-m","unittest","discover","-s","tests/integration","-v"],cwd=root,env=env)
    sys.exit(result.returncode)
finally:
    if server is not None:
        server.cleanup()
