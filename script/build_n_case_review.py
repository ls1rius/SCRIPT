"""Render an existing N diagnostic without inference or rewriting its results."""
import argparse
import json
from pathlib import Path


def build(out):
    def read(name):
        return json.loads((out/name).read_text())
    def decisions(plan):
        result={rid:k for k in ('keep','skip','uncertain') for rid in plan[k]}
        result.update({rid:'group' for group in plan['merge_groups'] for rid in group})
        return result
    report=read('report.json')
    if not report['complete']:
        raise ValueError('Diagnostic has not completed')
    experiment=read('experiment.json');artifact=experiment.get('artifact_directory','N')
    baseline=experiment.get('variant')=='unmodified_M_parent_control'
    question=read('question.json');pool=read(artifact+'/pool.json')['records']
    actual=decisions(read(artifact+'/plan.json'))
    proposed=decisions(read(artifact+'/proposed_plan.json')) if not baseline else actual
    rows=read('source_rows.json');observations={r['caption_id']:r for r in read('memory.json')['observations']}
    overrides=report['source_preservation']
    links=(f'[模型及实际计划]({artifact}/plan.json)' if baseline else
        f'[原模型计划]({artifact}/proposed_plan.json) · [实际计划]({artifact}/plan.json) · '
        f'[来源保留审计]({artifact}/source_preservation.json)')
    lines=['# '+('未修改M父版本对照' if baseline else 'N')+'固定来源单题核对', '',
        '固定原135个采样点、64条来源和10字段请求；没有新导航、字段规划或自动重试。',
        '本页检查已请求字段的执行及原文保留，不能证明这些字段都必要，也不是单因素消融。', '',
        question['question'], '',
        *[f'{chr(65+i)}. {option}' for i,option in enumerate(question['options'])], '',
        f"实际答案：{report['answer']}；标注：{report['gold']}；正确：{report['correct']}。", '',
        '[完整最终正文](final_description.txt) · '+links+' · [调用成本](report.json)', '',
        '程序覆盖次数：'+str(len(overrides['overrides']))+'；原模型决定未改写。', '',
        '## 全部采样点与实际候选决定', '']
    for row in rows:
        cid=row['caption_id'];sid=row['source_id']
        candidates=[r for r in pool if any(o.get('caption_id')==cid for o in r['occurrences'])]
        lines += [f"### Frames {row['sample_interval']} / {cid}", '',
            '原文：'+row['original_text'], '',
            f'[该来源抽取结果](extraction_results/{sid}.json)', '',
            '```json',json.dumps(observations[cid]['evidence'],ensure_ascii=False,indent=2),'```', '',
            *[f"- {r['id']}：{proposed[r['id']]} → {actual[r['id']]}；{r['text']}" for r in candidates], '']
    (out/'case_review.md').write_text('\n'.join(lines))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',type=Path,required=True)
    build(parser.parse_args().out.resolve())
