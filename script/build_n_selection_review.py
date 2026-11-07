"""Readable saved selection inputs, old decisions and new grounded reviews."""
import argparse
import json
from pathlib import Path


def build(out):
    read=lambda name:json.loads((out/name).read_text())
    report=read('report.json');experiment=read('experiment.json')
    results={r['id']:r for r in report['cases']}
    lines=['# N场景初筛诊断', '', experiment['version'], '',
        '仅测试候选初筛，没有读取详细场景或生成最终答案。选项顺序保持原样。', '',
        '自动候选/排序检查通过不等于所有语义解释正确；人工核查另见semantic_review.json。', '',
        '[完整报告与成本](report.json) · [原始调用](calls.jsonl)', '']
    def cell(text):return str(text).replace('|','\\|').replace('\n','<br>')
    for case in read('cases.json'):
        cid=case['id'];r=results.get(cid)
        lines += ['## '+cid,'',case['inputs']['question'],'',
            *[f'- O{i}: {text}' for i,text in enumerate(case['inputs']['options'])],'']
        if r is None:
            lines += ['未完成；请查看已保存的逐次调用和失败报告。',''];continue
        lines += [f"自动检查通过：{r['passed']}；候选顺序：{', '.join(r['candidate_order']) or '空'}。",'',
            f"处理秒数：{r['seconds']:.3f}；输出token：{r['output_tokens']}。",'']
        for scene in case['inputs']['directory']:
            sid=scene['id'];decision=r['derived_selection']['scene_decisions'][sid]
            lines += [f"### {sid} / {scene['interval']}",'',scene['description'],'']
            if 'old_output' in case:
                old=case['old_output']['scene_decisions'][sid]
                lines += [f"原M：{old['relevance']}，{old['priority']}；{old['reason']}",'']
            lines += [f"N程序汇总：{decision['relevance']}，{decision['priority']}；{decision['reason']}",'',
                '| 选项 | 分数 | 原文引用 | 模型理由 |','|---|---:|---|---|']
            for oid,review in r['model_output']['scene_reviews'][sid]['option_reviews'].items():
                lines.append('| '+' | '.join(cell(v) for v in [oid,review['priority'],review['anchor'],review['reason']])+' |')
            lines.append('')
    (out/'selection_review.md').write_text('\n'.join(lines))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    build(p.parse_args().out.resolve())
