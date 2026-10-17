"""Run the original VideoTree pipeline on EgoSchema or a custom five-choice question.

使用已有 LaViLa captions；提问时建树、筛选描述并回答，不重新生成 captions。
summary.json 汇总本次问答结果，不是模型生成的视频摘要。
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    paths = parser.add_argument_group('Data and output')
    paths.add_argument('--data-root', default='data/EgoSchema/egoschema')
    paths.add_argument('--output', default='workdirs/videotree-smoke')

    models = parser.add_argument_group('Models')
    models.add_argument('--model', default='checkpoints/Qwen/Qwen2.5-VL-7B-Instruct')
    models.add_argument('--feature-model', default='checkpoints/BAAI/EVA-CLIP-8B')
    models.add_argument('--feature-batch-size', type=int, default=4)
    models.add_argument('--max-new-tokens', type=int, default=768)

    examples = parser.add_argument_group('Examples and custom question')
    examples.add_argument('--video-id')
    examples.add_argument('--question')
    examples.add_argument('--options', nargs=5, metavar=('A', 'B', 'C', 'D', 'E'))
    examples.add_argument('--num-examples', type=int, default=3)
    examples.add_argument('--list-examples', action='store_true')
    parser.add_argument('--skip-preprocess', action='store_true',
                        help='Reuse existing frames and features without checking their caches.')
    return parser


def validate_args(args, parser):
    if bool(args.question) != bool(args.options):
        parser.error('A custom question requires --question and exactly five --options.')
    if args.question and not args.video_id:
        parser.error('A custom question requires --video-id.')
    if args.num_examples < 1:
        parser.error('--num-examples must be positive.')
    if args.feature_batch_size < 1 or args.max_new_tokens < 1:
        parser.error('Batch size and output token limit must be positive.')


def load_examples(args, data_root, parser):
    """优先选择子集；子集外的视频使用完整集及对应 captions。"""
    annotations = json.loads((data_root / 'subset_anno.json').read_text())
    captions_path = data_root / 'lavila_subset.json'
    if args.video_id and args.video_id not in annotations:
        annotations = json.loads((data_root / 'fullset_anno.json').read_text())
        captions_path = data_root / 'lavila_fullset.json'
        if args.video_id not in annotations:
            parser.error('Video ID is absent from the EgoSchema annotations.')

    video_ids = [args.video_id] if args.video_id else list(annotations)[:args.num_examples]
    selected = {uid: dict(annotations[uid]) for uid in video_ids}
    return selected, captions_path


def print_examples(examples):
    for uid, item in examples.items():
        print(uid, item['question'])
        for i in range(5):
            print(f'  {chr(65 + i)}. {item[f"option {i}"]}')


def apply_custom_question(examples, args):
    if not args.question:
        return
    item = examples[args.video_id]
    item['question'] = args.question
    for i, option in enumerate(args.options):
        item[f'option {i}'] = option
    # 替换问题后，原题的标准答案不再适用。
    item.pop('truth', None)


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def run(script, *args):
    """使用当前解释器运行一个阶段，失败时立即停止。"""
    # 保留子进程隔离和 VSCode subProcess 调试支持。
    command = [sys.executable, str(ROOT / script), *map(str, args)]
    print('RUN', ' '.join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def preprocess_video(args, data_root, video_ids):
    """按 1 FPS 抽帧并提取 EVA-CLIP 特征，脚本自动复用已有缓存。"""
    if args.skip_preprocess:
        return
    run('data_extraction/extract_images.py', '--data-root', data_root,
        '--video-ids', *video_ids)
    run('data_extraction/extract_features.py', '--model', args.feature_model,
        '--batch-size', args.feature_batch_size,
        '--video-ids', *video_ids)


def build_video_tree(common_args, output):
    """先扩大聚类数量寻找相关内容，再细分相关簇并保存所选帧索引。"""
    run('adaptive_breath_expansion.py', *common_args,
        '--output_filename', 'relevance_score.json',
        '--prompt_type', 'cap_score', '--disable_eval')
    run('depth_expansion.py',
        '--relevance_path', output / 'relevance_score.json',
        '--width_path', output / 'width_res.json',
        '--output_path', output / 'depth_expansion_res.json')


def answer_questions(common_args, output, evaluate):
    """按所选索引读取 captions，按时间排列后交给 LLM 回答。"""
    evaluation_args = [] if evaluate else ['--disable_eval']
    run('main_qa.py', *common_args,
        '--output_filename', 'standard_qa.json',
        '--tree_node_idx', output / 'depth_expansion_res.json', *evaluation_args)


def summarize_example(uid, item, frame_indices, data_root):
    prediction = item['pred']
    if prediction not in range(5):
        raise RuntimeError(f'Invalid answer for {uid}: {item["response"]}')
    answer = chr(65 + prediction)

    # 1 FPS 下零起始索引 i 对应 i 秒；evidence 保存所选 caption，未经人工核实。
    evidence = [{'second': i, 'caption': item['narration'][i]} for i in frame_indices]
    result = {
        'video_id': uid,
        'video_path': str(data_root / 'videos' / f'{uid}.mp4'),
        'question': item['question'],
        'options': {letter: item[f'option{letter}'] for letter in 'ABCDE'},
        'answer': answer,
        'answer_text': item[f'option{answer}'],
        'response': item['response'],
        'selected_frames': len(frame_indices),
        'evidence': evidence,
    }
    if item['truth'] >= 0:
        result.update(truth=chr(65 + item['truth']), correct=prediction == item['truth'])
    return result


def save_summary(args, data_root, output, evaluate, started):
    """汇总已有输出，不调用模型；启用评估时结果包在 data 字段里。"""
    results = json.loads((output / 'standard_qa.json').read_text())
    rows = results.get('data', results)
    tree_rows = json.loads((output / 'depth_expansion_res.json').read_text())
    frame_indices = {row['name']: row['sorted_values'] for row in tree_rows}
    summary = {
        'pipeline': 'EVA-CLIP-8B -> adaptive width -> relevance-based depth -> LLM QA',
        'model': str(args.model),
        'seconds': round(time.monotonic() - started, 2),
        'examples': [],
    }
    for uid, item in rows.items():
        result = summarize_example(uid, item, frame_indices[uid], data_root)
        summary['examples'].append(result)
        print(f'ANSWER {uid}: {result["answer"]}. {result["answer_text"]}', flush=True)
    if evaluate:
        summary['accuracy'] = results['acc']
    summary_path = output / 'summary.json'
    write_json(summary_path, summary)
    print('SUMMARY', summary_path, flush=True)


def run_pipeline(args, data_root, output, examples, captions_path):
    output.mkdir(parents=True, exist_ok=True)
    annotation_path = output / 'input_anno.json'
    write_json(annotation_path, examples)
    # 仅在所有样本都有公开标准答案时计算准确率。
    evaluate = all(item.get('truth', -1) in range(5) for item in examples.values())
    started = time.monotonic()

    preprocess_video(args, data_root, list(examples))
    common_args = [
        '--anno_path', annotation_path,
        '--data_path', captions_path,
        '--duration_path', data_root / 'duration.json',
        '--model', args.model,
        '--max_new_tokens', args.max_new_tokens,
        '--output_base_path', output,
        '--save_info', '--start_from_scratch',
    ]
    build_video_tree(common_args, output)
    answer_questions(common_args, output, evaluate)
    save_summary(args, data_root, output, evaluate, started)


def main():
    parser = build_parser()
    args = parser.parse_args()
    validate_args(args, parser)
    # 所有相对路径均以项目根目录为基准。
    data_root = (ROOT / args.data_root).resolve()
    output = (ROOT / args.output).resolve()
    examples, captions_path = load_examples(args, data_root, parser)
    if args.list_examples:
        print_examples(examples)
        return
    apply_custom_question(examples, args)
    run_pipeline(args, data_root, output, examples, captions_path)


if __name__ == '__main__':
    main()
