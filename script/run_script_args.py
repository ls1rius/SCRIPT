"""Run SCRIPT arguments saved as JSON, without interpreting them as shell code."""
import json
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent.parent
args = json.loads(Path(sys.argv[1]).read_text())
if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
    raise ValueError('Arguments must be a JSON array of strings')
subprocess.run([sys.executable, str(root / 'script_qa.py'), *args], cwd=root, check=True)
