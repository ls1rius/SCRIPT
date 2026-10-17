"""Run saved CLI arguments without interpolating question text into shell code."""
import json
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent.parent
args = json.loads(Path(sys.argv[1]).read_text())
if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
    raise ValueError('Query arguments must be a JSON array of strings.')
subprocess.run([sys.executable, str(root / 'demo.py'), *args], cwd=root, check=True)
