"""Read saved L calls and create review documents; never run inference."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from script_core.temporal_entity import partition, SETTINGS
from script_core.i_evidence_planner import render
import torch

OUT = Path(__file__).resolve().parent
RUN = ROOT / 'workdirs/evidence-planner-L-I-300-20261102-r1'
H = ROOT / 'workdirs/generalized-lazy-H-300-20261101-r5'
CASES = [
    ('01_bricks', '951ccf40-1a99-4138-a441-4edf16af5446', '搬砖：筛选后由错变对'),
    ('02_baby_chair', '73ecc613-5e39-4348-94ba-ee6cb197367a', '婴儿摇椅：关键交谈证据仍在，却由对变错'),
    ('03_syringe', 'c7670500-3a0f-4f31-a85a-a1cf708b2f49', '注射器：答对，但筛选后证据不足'),
    ('04_bowl_bad', 'b45379aa-d6f3-4033-83df-5577fc49cfc0', '调配碗中食物：丢掉搅拌与工具，只剩末条候选，答错'),
    ('05_conversation_bad', '3e700f0b-b4a9-4000-92b7-f0fa3be55161', '谈话目的：已选来源没有对话内容，筛选后仍答错'),
]
ANALYSIS = {
    '04_bowl_bad': '事后分析：I、全保留、L均回答E，标注为B，因此不能说错误由L筛选单独造成。L将R0—R139全部skip，仅最后R140保留，最终证据只是一个未解决观察“C picks the tin of spice”。139秒的搅拌、142—144秒的勺子/木勺和混合物等与题目直接相关的原文及派生句均被删掉。上游还有角色分类错误：142与144秒把spoon抽成body_part；生成句只显示with spoon，掩盖了内部角色错配。当前N限制身体部位不能作为instrument，并未保证反方向上工具不被标成body_part，不能声称这类错误已经解决。原解码协议在此前全部skip时禁止最后项继续skip；保留末条不构成充分性判断，也没有记录逐项理由。各选项共用长流程，区别在最后追加冰箱/烤箱/微波炉/台面冷却或不追加；最终单句不足以区分。原文关于米、面粉、香料的描述与选项关于麦片、奶、糖的描述也不能直接等同。',
    '05_conversation_bad': '事后分析：I、全保留、L均回答D，标注为C。原来选中的25秒主要是走动、看桌子、拿手机等caption，没有对话逐字稿或明确讨论食材的陈述；仅据这些来源不能确认谈话主题。L将R0—R30全部skip，只保留最后的未解决观察R31“A woman Y walks towards C”。原解码协议在此前全skip时禁止最后项skip，不能将这条非空结果当作充分证据。本例同时暴露上游证据不足与下游过度筛选，不能归因为遗漏一个位置字段，也不能据标注补写原视频对话。',
    '01_bricks': '事后分析：第107秒的原文明确包含 brick wall，原文候选 R42 和事实候选 R43 均跳过；第143秒把砖放到已排列砖上的 R62/R63 均保留。答案从 D（建墙）变成标注 B（堆砖）。这与删除 wall 表述、保留堆叠表述相一致，但规划器未保存选择理由，不能断言这是其实际意图，也不能据标注断定 wall 原文是错误的。进一步核对原始抽取调用：字段规划请求了destination/location/surface；模型实际生成destination=brick wall，因on不满足destination绑定而被校验器以unsupported_destination_binding拒绝，并非模型完全没提到brick wall。instrument=left hand也被body_part_is_not_instrument拒绝。最终接受字段不包含这些拒绝项；原始调用及CPU回放见docs/history/N_Source_Role_Binding_2026-11-02.json。',
    '02_baby_chair': '事后分析：第168秒 R100 “A woman X talks to C.”仍保留，第165—166秒取饼干/食物也仍保留；因此这不是删掉交谈句造成的直接信息丢失。仅能确认删去29条候选后，回答从B变D；没有回答推理或进一步消融，无法确定具体因果。另有明确的上游字段错误：第168秒 H 将 woman 填为 X 的 color，派生 R101 “The color of X is woman.”，L也保留；这是已存抽取错误，不能认为通过来源审计就语义正确。',
    '03_syringe': '事后分析：280条仅保留 R0，其正文为把塑料管放入瓶中，不支持针头在开头拆除丢弃、结尾重新装上的前后变化。回答命中标注A只能记为答对，不能视为证据筛选成功。该题主动保留的是首条R0，不是解码器为避免全skip而强制最后一条非skip的情形。不能从仅字母输出恢复回答模型的实际推理。',
}
manifest = {}

def read(p):
    manifest[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return json.loads(p.read_text())

def dump(p, x):
    p.write_text(json.dumps(x, ensure_ascii=False, indent=2) + '\n')

def link(p, label):
    return f'[{label}]({p})'

def cell(s):
    return str(s).replace('|', '&#124;').replace('\n', '<br>')

def interval(pair):
    return f'[{pair[0]},{pair[1]})'

labels = read(RUN / 'evaluation_only.json')
summary = ['# L-r1 真实 case 与 bad case\n',
    '来源：冻结离线 L-r1，版本 I-evidence-planner-L-v1-open-state-adjacent。全部读取已保存调用，无模型重跑。不是新默认 L 的 Frames 显示版本验证。\n',
    '前三例是 L 对全部保留组发生答案正误变化的全部三题；后两例是L实际答错、仅剩最后候选的诊断案例。属于结果导向选择，不代表随机样本。\n',
    '|案例|采样点|候选→保留|I / 全保留 / L|正确答案|\n|---|---:|---:|---|---|']

for folder, qid, title in CASES:
    dest = OUT / folder
    dest.mkdir(exist_ok=True)
    rdir = RUN / 'runs' / qid
    q = read(RUN / 'question' / f'{qid}.json')
    pool = read(RUN / 'pool' / f'{qid}.json')
    plan = read(rdir / 'plan.json')
    saved_input = read(rdir / 'L_evidence_planner/answer_output_input.json')
    saved_audit = read(rdir / 'plan_audit.json')
    packet = read(H / 'packets' / f'{qid}.json')
    text, audit = render(pool, plan)
    assert text == saved_input['prompts'][0].split('  Description: ', 1)[1].split('\n\n###', 1)[0]
    assert audit == saved_audit
    assert not plan['merge_groups'] and not plan['uncertain']
    obs = pool['source_memory']['observations']
    assert sorted(o['frame_index'] for o in obs) == sorted(packet['selected_seconds'])
    decisions = {rid: kind for kind in ['keep', 'skip', 'uncertain'] for rid in plan[kind]}
    assert set(decisions) == {r['id'] for r in pool['records']}
    # Map actual complete paragraphs, rather than recreating their wording.
    final_lines = text.splitlines()[1:]
    assert len(final_lines) == len(audit['groups'])
    final_by_id = {rid: line for group, line in zip(audit['groups'], final_lines) for rid in group['record_ids']}
    feature = Path('/mnt/ssd1/public/VideoAgent/EgoSchema/egoschema_features') / f'{qid}.pt'
    manifest[str(feature)] = hashlib.sha256(feature.read_bytes()).hexdigest()
    features = torch.load(feature, map_location='cpu', weights_only=True)
    directory = partition(qid, features, packet['packet']['captions'], SETTINGS)
    scenes = directory['scenes']
    scene_of = {i: s['id'] for s in scenes for i in s['chronological_order']}
    dump(dest / 'reconstructed_boundaries.json', {
        'notice': 'CPU reconstruction from retained EVA features and archived segmentation settings; not an original navigation log.',
        'settings': SETTINGS, 'feature': str(feature),
        'scenes': [{k: s[k] for k in ['id', 'interval', 'caption_ids']} for s in scenes]})
    dump(dest / 'pool.json', pool)
    dump(dest / 'plan.json', plan)
    dump(dest / 'plan_audit.json', saved_audit)
    dump(dest / 'raw_planner_output.json', read(rdir / 'planner_output.json'))
    dump(dest / 'actual_planner_input.json', read(rdir / 'planner_output_input.json'))
    dump(dest / 'actual_final_input.json', saved_input)
    dump(dest / 'messages.json', [dict(role='system', content=saved_input['head']),
                                 dict(role='user', content=saved_input['prompts'][0])])
    (dest / 'final_description.txt').write_text(text)
    results = {arm: read(rdir / arm / 'result.json') for arm in ['I_control', 'L_all_evidence', 'L_evidence_planner']}
    dump(dest / 'results.json', dict(correct_option='ABCDE'[labels[qid]], arms=results))
    answers = ['ABCDE'[int(v['selected_option'])] for v in results.values()]
    doc = [f'# {title}\n', f'Case ID：`{qid}`。真实运行：{link(rdir, "L-r1 保存调用")}。\n',
        '## 原问题与全部选项\n', q['question'] + '\n']
    doc += [f'- {letter}: {option}' for letter, option in zip('ABCDE', q['options'])]
    doc += ['\n## 场景与实际读取范围\n',
        '场景边界为本次 CPU 重建：沿用已封存 penalty=1.0、min_samples=4、max_scene_seconds=45 及原 EVA 特征。重建不调用模型。原场景 summary、首选理由、逐轮访问顺序、缺口与停止原因的历史记录缺失，原记录不可恢复；本版本未重做导航，不从最终选中集合倒推顺序。\n',
        'L 继承 H→I 的既有证据，不重新选帧或看图。下表“有读取”只表示该重建区间中存在实际选中 caption，不表示知道首次访问顺序。\n',
        '|场景|秒区间|当时 summary|实际选中采样点|\n|---|---|---|---|']
    selected = set(packet['selected_seconds'])
    for s in scenes:
        picked = [i for i in s['chronological_order'] if i in selected]
        doc.append(f'|{s["id"]}|{interval(s["interval"])}|原记录不可恢复|{", ".join(map(str, picked)) or "未选中"}|')
    doc += ['\n## 每帧处理前后（全部已选采样点）\n',
        'C数字是 caption 来源 ID，对应这里一秒采样网格中的时间；不是原视频帧率下的帧号。R数字是本题 L 候选 ID，E数字是上游文本实体引用，不证明物理身份。来源中的 source_caption_ids 指向这些 C ID。\n',
        '原文列保持来源标记与措辞；无已接受字段不等于从未尝试抽取。直接 caption/H 事实候选和上游时序关联候选分开列出。共享候选保留全部采样来源；逐帧重复展示同一最终句段只是回查，实际输入的每个段落只出现一次。图片链接供人核对，L 未重新看图。\n',
        '模型只输出逐 ID 决策，没有逐项自然语言理由；本表不补写理由。\n',
        '|帧／区间／场景|原始文本|实际解构字段|完整候选及实际处理|最终输入的完整对应段落|\n|---|---|---|---|---|']
    rows = []
    for o in obs:
        cid = o['caption_id']
        matches = [r for r in pool['records'] if any(x.get('caption_id') == cid for x in r['occurrences'])]
        assert matches
        facts = {k: v for k, v in o['evidence'].items() if k != 'original_text'}
        candidates = []
        finals = []
        for r in matches:
            direct = any(s.get('caption_id') == cid and s['region'] in ['description', 'H fact'] for s in r['sources'])
            sources = ', '.join(f'{x.get("caption_id", "无caption")} {interval(x["interval"])}' for x in r['occurrences'])
            candidates.append(f'{r["id"]} ({"直接原文/事实" if direct else "上游关联"}; {"、".join(r["kinds"])}): {r["text"]} → {decisions[r["id"]]}。全部来源：{sources}')
            if r['id'] in final_by_id and final_by_id[r['id']] not in finals:
                finals.append(final_by_id[r['id']])
        img = Path('/mnt/ssd1/public/VideoAgent/EgoSchema/egoschema_frames') / qid / f'{o["frame_index"]}.jpg'
        assert img.is_file()
        row = [f'{link(img, cid)} / {interval(o["sample_interval"])} / {scene_of[o["frame_index"]]}',
               o['evidence']['original_text'], json.dumps(facts, ensure_ascii=False) if facts else '仅原文，无已接受结构字段',
               '\n\n'.join(candidates), '\n\n'.join(finals) or '无；该采样点全部关联候选跳过']
        rows.append(dict(caption_id=cid, interval=o['sample_interval'], scene=scene_of[o['frame_index']], image=str(img),
                         original=o['evidence']['original_text'], accepted_fields=facts,
                         candidate_ids=[r['id'] for r in matches], final_paragraphs=finals))
        doc.append('|' + '|'.join(cell(v) for v in row) + '|')
    dump(dest / 'frame_trace.json', rows)
    doc += ['\n## 全部候选与分区\n',
        f'{len(pool["records"])} 条候选，保留 {len(plan["keep"])}，跳过 {len(plan["skip"])}，不确定 0，模型合并组 0。相同句子的跨采样归并发生在候选池构建阶段，不是本题规划器新做的合并。\n',
        '完整正文、全部来源、原始离散时间见 [pool.json](pool.json)；全部原始决策见 [raw_planner_output.json](raw_planner_output.json)，程序解析后见 [plan.json](plan.json)，来源审计见 [plan_audit.json](plan_audit.json)。\n',
        '## 实际最终 Description（完整原样）\n', '```text\n' + text + '\n```\n',
        '完整 system/user 消息见 [messages.json](messages.json)，实际保存输入见 [actual_final_input.json](actual_final_input.json)。head 是 system，prompts[0] 是 user，query_id 是元数据。旧提示仍写解释与置信度，但外部 LMFE 解码器约束实际回答仅 prediction: A–E；没有保存自然语言回答推理。这里保留旧 At sampled intervals/source assertion 原样，没有用新 Frames 格式替换。source assertion 仅表示来源文本声称，不能视为视觉核验。\n',
        '## 结果（与证据链分开）\n',
        f'I 对照：{answers[0]}；全部保留：{answers[1]}；L：{answers[2]}；标注答案：{"ABCDE"[labels[qid]]}。\n',
        '实际答案与 token 见 [results.json](results.json)。\n',
        '## 事后检查（不是当时模型理由）\n', ANALYSIS[folder]]
    (dest / 'case.md').write_text('\n'.join(doc) + '\n')
    summary.append(f'|[{title}]({folder}/case.md)|{len(obs)}|{len(pool["records"])} → {len(plan["keep"])}|{" / ".join(answers)}|{"ABCDE"[labels[qid]]}|')
    print(folder, 'scenes', [(s['id'], s['interval']) for s in scenes], 'frames', len(obs))

summary += ['\n每例包含全部选帧的原文→实际字段→完整候选/决策→实际最终段落的可读表，以及完整 Description、消息、规划输入输出与来源审计。场景 summary 和逐轮导航的历史缺失已逐例明示。',
    '\n仅生成展示文件。来源 SHA256 记入 manifest.json，生成结束后全部重新校验，未修改任何冻结实验。']
(OUT / 'README.md').write_text('\n'.join(summary) + '\n')
for path, sha in manifest.items():
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha, path
dump(OUT / 'manifest.json', manifest)
print('Validated exact final descriptions, saved audits, selected frame sets, candidate partitions, image paths, and unchanged source hashes.')
