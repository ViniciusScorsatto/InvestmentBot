"""Run deterministic regressions without production credentials or config overrides."""
import os
from pathlib import Path
import subprocess
import sys

env = {key:value for key,value in os.environ.items()
       if key != "DATABASE_URL" and not key.startswith("SWING_LAB_")}
root = Path(__file__).resolve().parents[1]
raise SystemExit(subprocess.call([sys.executable,"-m","unittest","discover","-s","swing-lab","-v"],cwd=root,env=env))
