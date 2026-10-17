"""Real-model SCRIPT smoke on a legal 36-second EgoSchema prefix, with two questions."""
import json
from pathlib import Path
import sys
import argparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from script_qa import main
from script_core.state import write_json, write_jsonl


def run(output=None):
    output = Path(output) if output else ROOT / 'workdirs/script/gpu-smoke-20261014'
    output.mkdir(parents=True, exist_ok=True)
    video_id = '0a01d7d0-11d6-4af6-abd9-2025656d3c63'
    write_json(output / 'manifest.json', [{'video_id': video_id,
        'uri': str((ROOT / 'data/EgoSchema/egoschema/videos' / f'{video_id}.mp4').resolve()), 'cutoff': 36.}])
    write_jsonl(output / 'questions.jsonl', [
        {'question_id': 'smoke-fabric', 'video_id': video_id, 'question': '手里操作的布料主要是什么颜色？',
         'options': ['黑色或深色', '白色', '绿色', '蓝色', '粉色']},
        {'question_id': 'smoke-activity', 'video_id': video_id, 'question': '这段视频里的人主要在做什么？',
         'options': ['缝制布料', '清洗厨房用具', '玩桌游', '骑自行车', '修理汽车']},
    ])
    cfg = ROOT / 'configs/script.yaml'
    print('SCRIPT REAL VIDEO BUILD', flush=True)
    rc = main(['build', '--manifest', str(output / 'manifest.json'), '--config', str(cfg), '--out', str(output / 'base')])
    if rc:
        return rc
    print('SCRIPT REAL VIDEO TWO QUERIES', flush=True)
    return main(['query', '--base', str(output / 'base'), '--questions', str(output / 'questions.jsonl'),
                 '--config', str(cfg), '--out', str(output / 'runs')])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    sys.exit(run(parser.parse_args().output))
