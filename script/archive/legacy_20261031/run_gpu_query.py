"""Run saved CLI arguments without interpolating question text into shell code."""
from pathlib import Path
import sys
_PROJECT_ROOT = next(p for p in Path(__file__).resolve().parents if (p/'script_core').is_dir())
sys.path[:0] = [str(_PROJECT_ROOT), str(_PROJECT_ROOT/'script')]

import json
from pathlib import Path
import subprocess
import sys

root = _PROJECT_ROOT
args = json.loads(Path(sys.argv[1]).read_text())
if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
    raise ValueError('Query arguments must be a JSON array of strings.')
subprocess.run([sys.executable, str(root / 'demo.py'), *args], cwd=root, check=True)
