"""Original VideoTree algorithms as SCRIPT tools; no alternative QA template."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
import re
from pathlib import Path
import sys
import tempfile
import time

from .config import digest


class OriginalModel:
    """Original Qwen.forward using the model resident in the SCRIPT worker."""
    def __init__(self, backend):
        from model import Qwen
        from transformers import AutoTokenizer
        self.backend = backend
        self.qa = Qwen.__new__(Qwen)
        self.qa.model, _ = backend.load('vlm')
        self.qa.tokenizer = AutoTokenizer.from_pretrained(backend.cfg['models']['vlm'], local_files_only=True)
        self.qa.temperature = 0
        self.qa.max_new_tokens = backend.cfg['models']['generation']['max_new_tokens']
        self.max_new_tokens = self.qa.max_new_tokens
        self.usage = {'input_tokens': 0, 'output_tokens': 0}

    def set_post_process_fn(self, function):
        self.qa.set_post_process_fn(function)

    def forward(self, head, prompts):
        self.backend.check_deadline()
        from .backend import deadline_stopping_criteria
        self.qa.stopping_criteria=(deadline_stopping_criteria(self.backend.deadline)
                                  if self.backend.deadline is not None else None)
        prediction, info = self.qa.forward(head, prompts)
        self.backend.check_deadline()
        self.usage['input_tokens'] += info['prompt_tokens']
        self.usage['output_tokens'] += info['completion_tokens']
        return prediction, info


def explicit_qa_answer(response):
    """Read an explicit answer declaration; never infer it from option text."""
    clean=response.replace('**','').replace('`','').strip()
    matches=re.findall(r'(?im)^\s*(?:prediction|(?:the\s+)?(?:correct\s+)?answer)\s*'
                       r'(?::|=|\bis\b)\s*(?:option\s+)?[\[(]?([A-E])\b',clean)
    choices=set(c.upper() for c in matches)
    return ord(next(iter(choices)))-ord('A') if len(choices)==1 else -1


def run_width(inputs, model, device='cuda', seed=0):
    """Invoke the unchanged original launch function with an injected model.

    Invocation is serial inside ModelWorker. Only argument routing and weight
    ownership are adapted; clustering/scoring/retry logic stays in the original.
    """
    import adaptive_breath_expansion as original
    from .state import write_json
    query = inputs['query']
    uid = query['id']
    with tempfile.TemporaryDirectory(prefix='script-videotree-') as folder:
        folder = Path(folder)
        (folder/'features').mkdir()
        (folder/'features'/f'{uid}.pt').symlink_to(Path(inputs['feature_uri']).resolve())
        write_json(folder/'questions.json', {uid: {'question': query['question'],
            **{f'option {i}': text for i, text in enumerate(query['options'])}}})
        write_json(folder/'captions.json', {uid: inputs['captions']})
        write_json(folder/'duration.json', {uid: inputs['duration']})
        argv = ['adaptive_breath_expansion.py', '--anno_path', str(folder/'questions.json'),
                '--data_path', str(folder/'captions.json'), '--duration_path', str(folder/'duration.json'),
                '--frame_feat_path', str(folder/'features'), '--output_base_path', str(folder),
                '--output_filename', 'relevance.json', '--prompt_type', 'cap_score',
                '--model', 'injected-script-model', '--device', device, '--seed', str(seed),
                '--max_new_tokens', str(getattr(model, 'max_new_tokens', 1024)),
                '--start_from_scratch', '--save_info', '--disable_eval']
        saved_argv, saved_model = sys.argv, original.get_model
        trace = io.StringIO()
        try:
            sys.argv = argv
            original.get_model = lambda args: model
            with redirect_stdout(trace), redirect_stderr(trace):
                original.launch()
        finally:
            sys.argv, original.get_model = saved_argv, saved_model
        relevance = json.loads((folder/'relevance.json').read_text())
        relevance = relevance.get('data', relevance)[uid]
        width = json.loads((folder/'width_res.json').read_text())[0]
        return {'width': width, 'relevance': relevance, 'trace': trace.getvalue()}


def run_depth(inputs):
    from depth_expansion import (hierarchical_clustering_with_external_primary,
                                 find_closest_points_in_temporal_order_subsub)
    import torch
    features = torch.load(inputs['feature_uri'], map_location='cpu', weights_only=True)
    width, relevance = inputs['width'], inputs['relevance']
    labels, scores = width['cluster_ids_x'], relevance['cluster_relevance']
    if len(labels) != len(features) or len(scores) <= max(labels):
        raise ValueError('VideoTree width/features mismatch')
    clusters = hierarchical_clustering_with_external_primary(features, labels, scores, 4, 4)
    indices = find_closest_points_in_temporal_order_subsub(features, clusters, scores)
    if not indices:
        raise ValueError('Original depth expansion selected no captions')
    return {'selected_seconds': indices, 'cluster_relevance': scores}


def call_original(backend, operation, inputs):
    """Exact-input tool cache shared across ablations, with paid work recorded."""
    root = backend.cfg['compatibility']['cache_directory']
    from .video import file_hash
    if not hasattr(backend, '_videotree_sources'):
        project = Path(__file__).resolve().parents[1]
        backend._videotree_sources = {name: file_hash(project/name) for name in (
            'adaptive_breath_expansion.py', 'depth_expansion.py', 'model.py', 'prompts.py',
            'util.py', 'dataset.py', 'kmeans_pytorch/__init__.py')}
    feature_hash = file_hash(inputs['feature_uri']) if inputs.get('feature_uri') else None
    key = digest([operation, inputs, backend.cfg['models'], backend.cfg['seed'], backend._videotree_sources, feature_hash])
    path = Path(root)/inputs['query_id']/(key+'.json') if root else None
    if path is not None and path.exists():
        cached = json.loads(path.read_text())
        if cached['key'] != key or cached['operation'] != operation:
            raise ValueError('Original tool cache mismatch')
        if getattr(backend, 'deadline', None) is not None and cached['source_seconds'] > backend.deadline-time.monotonic():
            raise ValueError('Cached original work would exceed its standalone allowance')
        result = deepcopy(cached['result'])
        result['usage'] = {'input_tokens': 0, 'output_tokens': 0,
            'tool_cache': {'hit': True, 'key': key, 'source_seconds': cached['source_seconds'],
                           'scope': 'Exact original tool input; standalone cost separate'}}
        return result
    started = time.monotonic()
    if operation == 'videotree_depth':
        data, usage = run_depth(inputs), {}
    else:
        if not hasattr(backend, '_videotree_model'):
            backend._videotree_model = OriginalModel(backend)
        model = backend._videotree_model
        model.usage = {'input_tokens': 0, 'output_tokens': 0}
        if operation == 'videotree_width':
            data = run_width(inputs, model, backend.cfg['models']['device'], backend.cfg['seed'])
        else:
            from prompts import update_pred_response
            model.set_post_process_fn(update_pred_response)
            prediction, info = model.forward(inputs['head'], inputs['prompts'])
            if prediction not in range(5):
                prediction=explicit_qa_answer(info['response'])
                if prediction not in range(5):
                    from .budget import ToolFailure
                    exc=ToolFailure('Original VideoTree QA did not produce a legal answer')
                    exc.raw_output={'data':None,'raw_text':info['response'],'usage':dict(model.usage)}
                    raise exc
                info['answer_parse_recovery']='explicit_declaration'
            data = {'selected_option': str(prediction), 'text': info['response'], 'info': info}
        usage = dict(model.usage)
        backend.model_tokens['vlm'] = usage['input_tokens']
    result = {'data': data, 'usage': usage}
    if path is not None:
        from .state import write_json
        seconds = time.monotonic()-started
        write_json(path, {'key': key, 'operation': operation, 'source_seconds': seconds, 'result': result})
        result['usage']['tool_cache'] = {'hit': False, 'key': key, 'source_seconds': seconds}
    return result
