"""SCRIPT CLI: build immutable scenes, query with lazy evidence, evaluate independently."""
import argparse
import json
from pathlib import Path
import sys
import time

from script_core.backend import preflight
from script_core.budget import ModelWorker
from script_core.build import build_video, load_base
from script_core.config import load_config, digest
from script_core.controller import answer_query
from script_core.evaluation import evaluate, read_jsonl
from script_core.state import write_json
from script_core.strategies import build_eager


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    build = commands.add_parser('build', help='Question-independent M0 and optional M_eager')
    build.add_argument('--manifest', required=True, help='JSON list of video_id, uri, optional cutoff/subtitle_uri')
    query = commands.add_parser('query', help='Run queries; default empty per-question overlay')
    query.add_argument('--base', required=True)
    query.add_argument('--questions', required=True, help='JSONL without answers or evaluation fields')
    query.add_argument('--resume', action='store_true')
    query.add_argument('--summary-graphs', help='Directory containing <video_id>.json caption assertion graphs')
    query.add_argument('--graph-routing', action='store_true', help='Use caption graph matches for coverage follow-ups')
    query.add_argument('--replay-source', help='Frozen real query directory or root containing per-question directories')
    query.add_argument('--replay-only', action='store_true', help='Replay planning too; fail if any recorded call is missing')
    query.add_argument('--sequence-manifest', help='Required for sequence_shared: video/prefix/order/cache directory')
    for cmd in (build, query):
        cmd.add_argument('--config', default='configs/script.yaml')
        cmd.add_argument('--out', required=True)
    scoring = commands.add_parser('evaluate')
    scoring.add_argument('--runs', required=True)
    scoring.add_argument('--annotations', required=True)
    scoring.add_argument('--out', required=True)
    doctor = commands.add_parser('doctor', help='Verify local model files and export hashes without loading weights')
    doctor.add_argument('--config', default='configs/script.yaml')
    doctor.add_argument('--out', default='workdirs/script/doctor')
    return p


def safe_directory_id(id_):
    # IDs are data; no model/user strings are interpreted as paths.
    return digest(id_)[:20]


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == 'evaluate':
        metrics = evaluate(args.runs, args.annotations, args.out)
        print(json.dumps(metrics['groups'], ensure_ascii=False, indent=2))
        return 0
    cfg = load_config(args.config)
    start, setup_cpu = time.monotonic(), time.process_time()
    cfg, artifacts = preflight(cfg)
    artifacts['validation_processing_seconds'] = time.monotonic()-start
    startup = {'seconds': time.monotonic()-start, 'cpu_seconds': time.process_time()-setup_cpu}
    write_json(Path(args.out) / 'artifacts.json', artifacts)
    write_json(Path(args.out) / 'run_config.json', cfg)
    if args.command == 'doctor':
        print('All four model snapshots available. Hashes:', Path(args.out) / 'artifacts.json')
        return 0
    backend = ModelWorker(cfg)
    try:
        if args.command == 'build':
            rows = json.loads(Path(args.manifest).read_text())
            if isinstance(rows, dict):
                rows = rows['videos']
            if len({r['video_id'] for r in rows}) != len(rows):
                raise ValueError('Duplicate video_id in manifest')
            video_index = {}
            for index, row in enumerate(rows):
                directory = Path(args.out) / safe_directory_id(row['video_id'])
                base, meta = build_video(row, cfg, backend, directory, startup if index == 0 else None)
                video_index[row['video_id']] = str(directory.resolve())
                if cfg['construction']['strategy'] == 'eager':
                    build_eager(base, cfg, backend, directory / 'M_eager')
            write_json(Path(args.out) / 'videos.json', video_index)
            return 0
        questions = read_jsonl(args.questions)
        if not questions or len({q.get('question_id', q.get('id')) for q in questions}) != len(questions):
            raise ValueError('Questions must be a nonempty JSONL with unique question IDs')
        if cfg['query']['pipeline']=='scheme4':
            if (args.summary_graphs or args.graph_routing or args.replay_source or args.replay_only or
                    args.sequence_manifest or cfg['cache']['scope']!='query'):
                raise ValueError('Scheme 4 uses its own summary evidence and query cache; legacy replay/graph/sequence options require query.pipeline=legacy')
            from script_core.scheme4 import run_queries
            return run_queries(args,questions,cfg,backend)
        shared = None
        if cfg['cache']['scope'] == 'sequence_shared':
            if not args.sequence_manifest:
                raise ValueError('sequence_shared requires --sequence-manifest with frozen question order/prefix/cache directory')
            from script_core.sequence import SequenceCache
            shared = SequenceCache(args.sequence_manifest, questions, cfg)
        root = Path(args.base)
        video_index = json.loads((root / 'videos.json').read_text()) if (root / 'videos.json').exists() else None
        cumulative = []
        initialized = {}
        for index, row in enumerate(questions):
            question_id = row.get('question_id', row.get('id'))
            out = Path(args.out) / safe_directory_id(question_id)
            metadata, base = {}, None
            result_path = out / 'result.json'
            try:
                directory = Path(video_index[row['video_id']]) if video_index else root
                restore_started, restore_cpu = time.monotonic(), time.process_time()
                base, metadata = load_base(directory)
                metadata = dict(metadata, base_restore_seconds=time.monotonic()-restore_started,
                                base_restore_cpu_seconds=time.process_time()-restore_cpu,
                                model_snapshot_validation_seconds=startup['seconds'] if index == 0 else 0.,
                                model_snapshot_validation_cpu_seconds=startup['cpu_seconds'] if index == 0 else 0.)
                details = None
                if cfg['construction']['strategy'] == 'eager':
                    details, eager_cost = build_eager(base, cfg, backend, directory / 'M_eager',
                                                     max(0., row.get('total_cap', 1e12)-metadata['initialization_cost']-cfg['budget']['final_answer_reserve']))
                    metadata = dict(metadata, initialization_cost=metadata['initialization_cost']+eager_cost['online'])
                    metadata['eager_build_complete'] = eager_cost['construction_complete']
                if shared:
                    details = shared.read(base, details)
                write_json(out / 'run_config.json', cfg)
                query_backend = backend
                if args.replay_only and not args.replay_source:
                    raise ValueError('--replay-only requires --replay-source')
                if args.replay_source:
                    from script_core.replay import ReplayBackend
                    replay_source = Path(args.replay_source)
                    if not (replay_source / 'calls.jsonl').exists():
                        replay_source /= safe_directory_id(question_id)
                    query_backend = ReplayBackend(backend, replay_source, args.replay_only)
                graph = (json.loads((Path(args.summary_graphs) / f'{row["video_id"]}.json').read_text())
                         if args.summary_graphs else None)
                if args.graph_routing and graph is None:
                    raise ValueError('--graph-routing requires --summary-graphs')
                result = answer_query(base, row, metadata, cfg, query_backend, out, details, args.resume,
                                      summary_graph=graph, graph_routing=args.graph_routing)
                if args.replay_source:
                    write_json(out / 'replay_manifest.json', query_backend.manifest())
                    result['experiment_mode'] = query_backend.manifest()['mode']
                    write_json(out / 'result.json', result)
                if shared and not result['error']:
                    shared.commit(out, base)
                initialized.setdefault(row['video_id'], metadata['initialization_cost'])
                failure_path = Path(args.out) / 'query_failures' / (safe_directory_id(question_id)+'.json')
                failure_path.unlink(missing_ok=True)
            except Exception as exc:
                # A bad model record or one video's state must not abort the shard.
                # Keep a separate failure file: never overwrite an existing result
                # or a directory owned by another active writer.
                calls_path = out / 'calls.jsonl'
                calls = read_jsonl(calls_path) if calls_path.exists() else []
                result = {'error': {'code': 'query_failed', 'message': f'{type(exc).__name__}: {exc}'},
                          'cost_summary': {'initialization': metadata.get('initialization_cost', 0.),
                                           'online': sum(c['actual_cost'] for c in calls)}}
                cost = result['cost_summary']
                cost.update(total=cost['initialization']+cost['online'],
                            total_cap=row.get('total_cap', cost['initialization']+cfg['budget']['online_cap']))
                result_path = Path(args.out) / 'query_failures' / (safe_directory_id(question_id)+'.json')
                write_json(result_path, dict(result, question_id=question_id, answer=None, stop_decision=None,
                    query={'id': question_id, 'video_id': row['video_id'],
                           'scope': row.get('scope', [0., base.manifest['cutoff']] if base else None)},
                    run_config=cfg, experiment_mode=('recorded_calls' if args.replay_only else
                        'fixed_observation_replay' if args.replay_source else 'live')))
                print(f'QUERY FAILED {question_id}: {exc}', file=sys.stderr, flush=True)
            if 'initialization_cost' in metadata:
                initialized.setdefault(row['video_id'], metadata['initialization_cost'])
            cumulative.append({'question_id': question_id, 'result_path': str(result_path),
                               'error': result['error'], 'cost': result['cost_summary']})
            print('RESULT', result_path, flush=True)
        write_json(Path(args.out) / 'sequence_cost.json', {
            'protocol': cfg['cache']['scope'], 'initialization_once': sum(initialized.values()),
            'cumulative_total': sum(initialized.values())+sum(r['cost']['online'] for r in cumulative), 'questions': cumulative})
        return 1 if any(r['error'] for r in cumulative) else 0
    finally:
        backend.close(graceful=True)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, KeyError, FileNotFoundError) as exc:
        print(f'SCRIPT ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
