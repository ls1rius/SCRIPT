"""One source-validated preprocessing directory per video inside an experiment."""
from copy import deepcopy
import importlib.metadata
import json
from pathlib import Path
import time

from .config import digest
from .persistent_cache import effective_config, source_identity, SOFTWARE
from .state import write_json


def identity(packet, cfg, settings, source_root=None, software=None):
    return digest(dict(version=1, video_id=packet['question']['video_id'],
        captions=packet['captions'], feature_hash=packet['feature_hash'],
        config=effective_config(cfg), settings=settings,
        source=source_identity(str(source_root or Path(__file__).resolve().parent)),
        software=software or {name: importlib.metadata.version(name) for name in SOFTWARE}))


def load_or_build(out, packet, cfg, settings, backend):
    from .budget import BudgetLedger, Meter
    from .temporal_pipeline import directory
    from .temporal_entity import validate_directory
    began = time.monotonic()
    key = identity(packet, cfg, settings)
    target = Path(out)/'preprocessing/directories'/packet['question']['video_id']/key
    path = target/'directory.json'
    if path.exists():
        try:
            entry = json.loads(path.read_text())
            if entry.get('key') == key and entry.get('checksum') == digest(entry.get('directory')):
                d = deepcopy(entry['directory'])
                validate_directory(d)
                if d['captions'] == packet['captions']:
                    d['cache_reuse'] = dict(hit=True, key=key, path=str(path),
                        lookup_seconds=time.monotonic()-began, original_cost_preserved=True)
                    return d
        except (ValueError, KeyError, TypeError):
            pass
    if (target/'calls.jsonl').exists():
        # Never overwrite partially spent preprocessing or a damaged artifact.
        target = target/('retry-'+str(time.time_ns()))
        path = target/'directory.json'
    ledger = BudgetLedger(settings['directory_seconds'], answer_reserve=0, calls_path=target/'calls.jsonl')
    meter = Meter(backend, ledger, cfg, target/'raw_calls')
    try:
        import torch
        features = torch.load(packet['feature_uri'], map_location='cpu', weights_only=True).tolist()
        d = directory(packet, features, meter, settings)
        d['cost_summary'] = ledger.summary()
        write_json(path, dict(key=key, directory=d, checksum=digest(d)))
        d['cache_reuse'] = dict(hit=False, key=key, path=str(path), original_cost_preserved=True)
        return d
    finally:
        meter.close()
