"""Frozen, validated prototype configuration (plan appendix B)."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


DEFAULTS = {
    'schema_version': 2, 'seed': 0, 'method': 'B2',
    'construction': {'strategy': 'goal_lazy', 'prebuild_entity_candidates': False,
                     'prebuild_relations': False, 'prebuild_identity_links': False},
    'cache': {'scope': 'query', 'strict_input_match': True, 'reuse_overlap_inputs': True,
              'verify_dependencies_on_read': True, 'share_query_bindings': False},
    'models': {'visual_encoder': 'checkpoints/BAAI/EVA-CLIP-8B',
               'detector': 'checkpoints/IDEA-Research/grounding-dino-tiny',
               'vlm': 'checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',
               'text_embedding': 'checkpoints/BAAI/bge-m3',
               'generation': {'do_sample': False, 'max_new_tokens': 1024},
               'batching': {'visual_encoder': 4, 'text_embedding': 1,
                            'text_embedding_max_tokens': 8192},
               'max_input_tokens': 16384, 'precision': 'bfloat16', 'device': 'cuda',
               'revisions': {}, 'local_files_only': True},
    'segmentation': {'sample_fps': 1., 'penalty': 5., 'min_samples': 2,
                     'solver': 'exact_dp', 'tie_epsilon': 1e-8, 'max_scene_duration': None,
                     'fixed_window_seconds': 30., 'kmeans_k': 8,
                     'shot_histogram_threshold': .35, 'shot_min_seconds': .5},
    'initialization': {'window_seconds': 8., 'window_stride_seconds': 7.,
                       'caption_source': 'vlm',
                       'caption_frames_per_window': 4, 'caption_longest_side': 768,
                       'summary_max_chinese_chars': 160, 'summary_reduce_batch_windows': 16,
                       'use_provided_subtitles': True, 'generate_asr': False},
    'materialization': {'default_scope': 'goal', 'include_interaction_context': True,
                        'max_entities_per_batch': 16, 'detection_box_threshold': .35,
                        'detection_text_threshold': .25, 'same_class_nms_iou': .6,
                        'supplemental_detection_rounds': 1, 'supplemental_terms_per_window': 12},
    'tracking': {'max_gap_seconds': 2., 'min_detections_stable': 2, 'gate_iou': .1,
                 'gate_cosine': .85, 'accept_score': .45, 'iou_weight': .6,
                 'appearance_weight': .4, 'representative_crops': 4},
    'identity': {'candidate_top_k': 5, 'candidate_min_visual_cosine': .7,
                 'visual_weight': .8, 'description_weight': .2, 'entity_history_limit': 10,
                 'verify_before_confirm': True, 'unknown_occurrence_may_remain_unbound': True,
                 'index_returns_known_only': True},
    'retrieval': {'dense_top_k': 20, 'bm25_top_k': 20, 'rrf_constant': 60,
                  'initial_scene_count': 5, 'all_summaries_if_scene_count_le': 20, 'frontier_max': 12},
    'query': {'global_task_limit': 8, 'new_observations_per_task': 2,
              'reading_policy': 'coverage_first', 'max_script_reads': 3, 'script_context_windows': 12,
              'answer_strategy': 'direct', 'answer_order': 'content_hash', 'caption_policy': 'full',
              'no_progress_before_backtrack': 2, 'explore_every_nonexploration_tasks': 3,
              'global_recovery_rounds': 2, 'tool_transient_retries': 1, 'json_repair_retries': 1},
    'observation': {'max_window_seconds': 8., 'base_fps': 2., 'dense_fps': 4.,
                    'longest_side': 768, 'min_target_short_side': 24},
    'coverage': {'unit_seconds': 6., 'boundary_context_seconds': 1., 'global_summary_time_bins': 12},
    'ablations': {'feedback_unresolved': True, 'feedback_next_leads': True,
                  'explicit_local_goal': True, 'global_backtrack': True,
                  'entity_navigation': True, 'time_navigation': True,
                  'retain_candidates': True, 'observation_policy': 'target_driven',
                  'retrieval_entry': 'scene'},
    'budget': {'unit': 'processing_seconds', 'online_cap': 600., 'final_answer_reserve': 30.,
               'local_task_cap': 60., 'total_cap_rule': 'measured_initialization_plus_online_cap',
               'build_call_cap': 600., 'tool_call_cap': 60.},
}


def load_config(path=None):
    cfg = deepcopy(DEFAULTS)
    if path:
        import yaml
        overrides = yaml.safe_load(Path(path).read_text()) or {}
        def merge(dst, src):
            for key, val in src.items():
                if key not in dst:
                    raise ValueError(f'Unknown configuration field: {key}')
                if isinstance(dst[key], dict) and key != 'revisions':
                    if not isinstance(val, dict):
                        raise ValueError(f'{key} must be a mapping')
                    merge(dst[key], val)
                else:
                    dst[key] = val
        merge(cfg, overrides)
    validate_config(cfg)
    project = Path(__file__).resolve().parent.parent
    for kind in ('visual_encoder', 'detector', 'vlm', 'text_embedding'):
        if cfg['models'][kind].startswith('checkpoints/'):
            cfg['models'][kind] = str((project / cfg['models'][kind]).resolve())
    return cfg


def validate_config(cfg):
    def finite_values(value):
        if isinstance(value, dict):
            for v in value.values():
                finite_values(v)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError('Configuration numbers must be finite')
    finite_values(cfg)
    if cfg['initialization']['caption_source'] not in ('vlm', 'provided'):
        raise ValueError('initialization.caption_source must be vlm or provided')
    if cfg['query']['reading_policy'] not in ('coverage_first', 'script_guided'):
        raise ValueError('Unknown reading policy')
    for name, allowed in {'answer_strategy': ('direct', 'contrastive'),
                          'answer_order': ('original', 'content_hash'),
                          'caption_policy': ('full', 'ranked')}.items():
        if cfg['query'][name] not in allowed:
            raise ValueError(f'Unknown query.{name}')
    for name in ('max_script_reads', 'script_context_windows'):
        if type(cfg['query'][name]) is not int or cfg['query'][name] < 1:
            raise ValueError(f'query.{name} must be a positive integer')
    for name, value in cfg['models']['batching'].items():
        if type(value) is not int or value < 1:
            raise ValueError(f'models.batching.{name} must be a positive integer')
    for name, value in cfg['ablations'].items():
        if name == 'observation_policy':
            if value not in ('target_driven', 'uniform_dense'):
                raise ValueError('Invalid observation ablation')
        elif name == 'retrieval_entry':
            if value not in ('scene', 'window', 'event', 'fusion'):
                raise ValueError('Invalid retrieval entry ablation')
        elif type(value) is not bool:
            raise ValueError(f'ablations.{name} must be boolean')
    if cfg['schema_version'] != 2 or cfg['method'] not in ('B0', 'B1', 'B1S', 'B2', 'B3'):
        raise ValueError('Unsupported schema or method')
    c = cfg['construction']
    if c['strategy'] not in ('goal_lazy', 'scene_lazy', 'eager'):
        raise ValueError('Unknown construction strategy')
    eager = c['strategy'] == 'eager'
    if c['prebuild_relations'] != eager or c['prebuild_identity_links'] != eager:
        raise ValueError('Only eager enables prebuild_relations and prebuild_identity_links')
    if c['prebuild_entity_candidates'] or cfg['initialization']['generate_asr']:
        raise ValueError('Coarse entity prebuild and ASR are outside this prototype')
    if cfg['cache']['scope'] not in ('query', 'sequence_shared'):
        raise ValueError('Invalid cache scope')
    for flag in ('strict_input_match', 'verify_dependencies_on_read'):
        if cfg['cache'][flag] is not True:
            raise ValueError(f'{flag} is required')
    if cfg['cache']['share_query_bindings']:
        raise ValueError('Query bindings cannot be shared')
    if cfg['segmentation']['solver'] not in ('exact_dp', 'fixed_windows', 'kmeans_runs', 'shot_histogram') or cfg['segmentation']['max_scene_duration'] is not None:
        raise ValueError('Unsupported segmentation experiment')
    if cfg['ablations']['retrieval_entry'] in ('event', 'fusion') and not eager:
        raise ValueError('Event/fusion retrieval requires separately paid eager construction')
    if (type(cfg['segmentation']['kmeans_k']) is not int or cfg['segmentation']['kmeans_k'] < 1
            or cfg['segmentation']['fixed_window_seconds'] <= 0
            or not 0 < cfg['segmentation']['shot_histogram_threshold'] <= 1
            or cfg['segmentation']['shot_min_seconds'] < 0):
        raise ValueError('Invalid segmentation comparison parameters')
    if cfg['budget']['unit'] != 'processing_seconds':
        raise ValueError('Only measured serial processing_seconds is implemented')
    if cfg['models']['device'] not in ('cpu', 'cuda') or cfg['models']['precision'] not in ('float32', 'bfloat16'):
        raise ValueError('Unsupported model device or precision')
    for section, names in {
        'segmentation': ['min_samples'], 'initialization': ['caption_frames_per_window', 'summary_reduce_batch_windows'],
        'query': ['global_task_limit', 'new_observations_per_task', 'global_recovery_rounds', 'tool_transient_retries', 'json_repair_retries'],
        'coverage': ['global_summary_time_bins'], 'materialization': ['max_entities_per_batch'],
    }.items():
        for name in names:
            if type(cfg[section][name]) is not int or cfg[section][name] < 0:
                raise ValueError(f'{section}.{name} must be a nonnegative integer')
    for section, names in {
        'segmentation': ['sample_fps', 'min_samples', 'tie_epsilon'],
        'initialization': ['window_seconds', 'window_stride_seconds', 'caption_frames_per_window',
                           'caption_longest_side', 'summary_reduce_batch_windows'],
        'budget': ['online_cap', 'final_answer_reserve', 'local_task_cap', 'tool_call_cap', 'build_call_cap'],
        'query': ['global_task_limit', 'new_observations_per_task'],
        'observation': ['max_window_seconds', 'base_fps', 'dense_fps', 'longest_side'],
        'coverage': ['unit_seconds', 'global_summary_time_bins'],
    }.items():
        for name in names:
            if not isinstance(cfg[section][name], (int, float)) or cfg[section][name] <= 0:
                raise ValueError(f'{section}.{name} must be positive')
    if cfg['segmentation']['penalty'] < 0:
        raise ValueError('Segmentation penalty cannot be negative')
    if cfg['initialization']['summary_reduce_batch_windows'] < 2:
        raise ValueError('Summary reduction needs at least two windows per batch')
    if cfg['initialization']['window_stride_seconds'] > cfg['initialization']['window_seconds']:
        raise ValueError('Window stride must not introduce gaps')
    if cfg['observation']['max_window_seconds'] > 8:
        raise ValueError('Observation packages must be <= 8 seconds')
    if cfg['coverage']['unit_seconds'] + 2 * cfg['coverage']['boundary_context_seconds'] > 8:
        raise ValueError('Coverage core plus context must be <= 8 seconds')


def base_config(cfg):
    """Navigation and detailed construction cannot change the shared M0."""
    result = {k: deepcopy(cfg[k]) for k in ('schema_version', 'seed', 'models', 'segmentation', 'initialization')}
    result['models']['revisions'].pop('@online', None)
    return result
