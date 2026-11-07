# 语义记忆的数据结构与整理流程

本文记录 2026-10-28 讨论确认的方案，配套实验为 `Controlled_Memory_Experiment_2026-10-28.md`。区分逻辑概念、磁盘存档及最终模型输入，避免把尚未实现的统一实体库当成现有功能。

## 2026-10-29：存档与决策输入分开

详细来源、ID、分类、角色、审计与索引继续保存在存档。最终回答应由固定代码生成面向判断的证据视图。
目标是相关、可靠且必要的证据；格式紧凑只是第一步，不能把 token 减少等同于判断质量改善。
后续证据筛选需要检查问题及选项真正不同的条件，并保留否定、时间顺序、对象和工具关系；证据不足须显式呈现。

当前运行采用 `script/schedule_evidence_evaluations.py` 统一调度，状态位于
`workdirs/controlled-memory-300-20261028-r1/control/throughput_20261029_r1/status.json`。
GPU 0 和 GPU 3 各承担一个互不重叠的抽取批次，GPU 3 同时保留一个回答任务通道；已准备且密封的来源优先进入原协议回答，再进入紧凑回答。
两套评测共用一次抽取产物；成功答案跳过，未完成但已花费的抽取不自动重启。现有 GPU 工作者通过 PID 和启动时间核验后接续，已运行批次不打断。
新工作者启动前检查至少 42 GiB 空闲；GPU 1、2 当前用于其他高占用任务。调度仅改变资源和跨批次执行顺序，冻结的推理代码、问题、选项、输入合同和评分保持不变。

`script_core/qa_evidence_view.py` 已提供独立的紧凑渲染：保留各条采样时间，隐藏存储 ID 和索引；若同一来源仍须保留完整原句，就展示该原句，避免再重复全部派生事实。没有保留原文的来源展示具名事实字段。审计映射单独保存，未知结构或没有可见证据的来源报错。
这一步尚未按问题筛选来源，也未压缩旧时序记忆；已有 A/B/C 冻结输入及默认入口保持原样。

CPU 预览命令（不调用模型、不生成答案；输出目录须新建）：

```bash
/mnt/ssd2/hli/work/miniconda3/envs/vts/bin/python script/preview_compact_evidence.py \
  --run workdirs/controlled-memory-300-20261028-r1 \
  --out workdirs/compact-evidence-preview-NEW \
  --tokenizer checkpoints/Qwen/Qwen2.5-VL-7B-Instruct
```

预览分别统计原 A/B/C 与紧凑 B/C 在同一已准备题目集合上的完整输入 token；来源映射、输入哈希、代码哈希与题目清单留在输出目录。此预览没有回答准确率或推理耗时结果。

用户随后授权在空卡评测，独立运行目录为 `workdirs/compact-evidence-300-20261029-r1/`，控制入口为 `script/run_compact_evidence_experiment.py`。
同一批 300 题重新生成 A 原始输入、B 紧凑原文、C 紧凑语义的答案，沿用父实验冻结的 QA 代码、模型、生成参数、旧时序记忆、视觉观察及解析协议。先核对 A 前 20 题的历史预测复现，再运行处理组，不按正确率决定是否继续。
截至提交时已准备 C 的 193 题，其余等待父实验的密封抽取产物，固定代码投影后自动接续；不重复进行模型抽取，继承的抽取成本单列计入。
调度每 10 秒检查四张 GPU：连续 3 次没有计算进程、利用率不高于 10%、至少 42 GiB 空闲且取得共享租约才启动；忙卡即使显存足够也等待。已有实验不停止或改写。
`queue_status.json`、`progress.json`、`report.json` 分别记录排队、执行及配对结果。该版本仍是呈现层实验，未加入按问题筛选证据。

随后用户指定“卡的话3号卡就可以跑吧”，资源策略改为固定 GPU 3 共享运行：仍连续 3 次检查至少 42 GiB 空闲，允许已有计算进程和高利用率。只停止尚未启动 GPU 工作者的旧空卡监听器，采用 `control/gpu3_shared/` 中独立密封的控制器；不修改原冻结实验。共享 GPU 的耗时包含计算竞争，不能解释为受控的算法速度差异。

## 三种信息及其关联

实体、事实、证据是三种横向关联的信息，不是固定嵌套的三层目录。

| 信息 | 回答的问题 | 当前实现 |
|---|---|---|
| 实体 | 谁、什么物体参与了描述 | 事实字段中的局部名称/提及；没有完整的跨场景统一实体身份层 |
| 事实 | 对象具有什么特征、处于什么条件、发生什么 | 动作、属性、状态、关系、变化、结果、事件联系七类 |
| 证据 | 这些记录来自哪里、何时观察到 | 来源 ID、原文、来源类型、采样时间、证据状态及原始引用 |

一条事实可引用多个参与对象；同一实体提及可参与多类事实。一条来源可以支撑多个事实。实体不是七类之一，工具/材料/对象是事件中的角色，不是某个实体的永久类别。

## 当前磁盘存档

每道题固定使用其上游已经读取的来源，存档包含：

```text
题目范围内的记忆
├─ sources：全部已读来源、原文、标记、ID、离散采样时间
├─ actions / attributes / states / relations / changes / results / event_links
├─ residuals：无法可靠结构化或需要保留细节的原文
└─ prior_hypotheses：既有身份/对应假设，不升级为观测事实
```

事实通过 `source_id` 关联来源。实体名称存于 `actor`、`patient`、`entity`、`left`、`right` 等字段。当前不凭同名把不同场景的对象自动合并，也不把工具角色直接当实体类型。

## 七类事实的字段和子类型

| 类别 | 必需语义字段 | 子类型/角色 |
|---|---|---|
| 动作 | actor、verb | patient、instrument、material、body_part、surface、source_location、destination、location、manner、recipient 共 10 种可选角色 |
| 属性 | entity、property、value | color、material、shape、size、texture、pattern、appearance、other 共 8 类 |
| 状态 | entity、dimension、value | cleanliness、wetness、integrity、open_closed、power、posture、motion、other 共 8 类 |
| 关系 | left、predicate、right、relation_type | spatial、possession、part_whole、attachment、other；未验证的 other 表述保留原文 |
| 变化 | entity、before、after | 无额外固定分类，必须有明确的前后条件表述 |
| 结果 | entity、predicate、value | 无额外固定分类，只记录明确产物/结果 |
| 事件联系 | left_event、connector、right_event、link_type | before、after、simultaneous、cause、purpose 共 5 类 |

每条事实另带来源 ID。存档省略未知动作角色，模型中间抽取需要检查全部角色并用 JSON null 表示未知。七类为空是合法情况，不要求每个来源产生七类，也不要求每个实体有八种属性。

属性与临时状态分开，属性也保留来源和时间，不默认永久成立。变化、结果、因果不能仅由动作常识自动补出。当前事件联系使用来源中的事件文字，并非完整的全局事件 ID 图。

## 实际整理顺序

1. 保存上游实际读到的全部描述与已接受补看观察；没有实体抽取也必须保存。未读描述不加入。
2. 建立来源单元；相同文字可在内部去重，但保留全部原始 caption ID 和离散采样时间。`#C/#O` 标记与正文分离，完整原文仍存档。
3. 七类分别检查每个来源，抽取阶段不提供问题、选项、答案或标签。所有字段必须来自同一授权来源；不能拼接无关句子形成新事实。
4. 校验来源、否定/不确定性及角色绑定。无依据或校验失败的候选不进入确定事实；最多一次的既有动作角色复核计入原预算。来源检查失败或预算耗尽要分别记录，不能以来源覆盖率代替分类完成率。
5. 不能可靠重建的复杂句、多事实句、否定或修饰细节保留完整原文。只有受限简单句满足可逆绑定时才允许不向最终视图重复附原句。
6. 按来源和采样时间生成最终视图；每个来源必须有事实或可见原文。模型无需反复跨来源表、实体索引和事实表查时间。

例如 `#C C wipes the red book with a cloth.` 可产生动作（C / wipes / red book / cloth）和属性（book / color / red）；同一句仍可保留为必要原文。这个例子是结构示意，不表示每次模型调用都会正确抽取。

## 新受控实验的模型输入

最终展示的层级为“题目 → 来源观测 → 七类事实及必要原文 → 具体字段”。B/C 使用相同的逐 caption 外层容器：

```json
{
  "caption_id": "C12",
  "frame_index": 12,
  "sample_interval": [12, 13],
  "evidence": {
    "actions": [{"actor": "C", "verb": "wipes", "patient": "red book", "instrument": "cloth"}],
    "attributes": [{"entity": "book", "property": "color", "value": "red"}],
    "categorized_original": {
      "original_text": "#C C wipes the red book with a cloth.",
      "information_types": ["action", "attribute"]
    }
  }
}
```

不同采样时刻的相同文字在 B/C 都展开为独立行，不合并成连续动作或独立事件次数。C 仅替换行内 evidence；题目、选项、原系统提示、旧时序记忆及已接受视觉观察都固定。视觉证据仍留在原位置，不只给 C 多转写一遍。

## 100 条来源盲审发现及入模前拒收

从固定首 20 题的 caption 来源中按正文和证据状态去重，再按 `SHA256(text + NUL + status)` 固定取前 100 条。审查内容只有原文和抽取字段，不展示问题、选项、标签或 benchmark 对错。

审计发现工具/身体部位混淆、把方向副词当直接对象、把工具名称中的一个词当材料、把起点同时当输入材料，以及把观看设备当物理承载表面等问题。新实验 C 的投影器追加有限拒收规则：

- `around/away/up/down` 等方向副词不能独自作为 patient。
- 手、脚、手指等身体部位不能在明确身体部位表达中写成 instrument。
- material 与 source_location 等角色完全重叠，或仅是工具名称的一部分时，不接受该动作记录。
- “观看视频 on phone”中的设备不能未经核验直接提升为物理 surface。

规则只拒收，不自动把某角色改成另一个角色。被拒的记录留在审计文件，整个来源原句进入最终输入。完整来源盲审、每条拒收与原文保留情况保存到新实验的 `validation/blind_source_audit.json` 和 `render_audits/`。

这些规则针对已发现问题，是有限校验，不是完整语言理解。100 条来源审计也不是穷尽的独立语义验证。来源覆盖、分类完成率、角色正确率和最终回答准确率必须分别报告。

## 实体层的边界

未来若加入实体 ID、别名、跨场景实例对应和统一事件 ID，需要独立实验。本轮先验证七类事实表示，避免同时改变对象身份推断，造成实验归因再次混杂。

## 执行准备记录

2026-10-28 已完成 C 的逐 caption 投影器、100 条来源盲审及 37 项针对性检查；上述审计共拒收 6 条候选记录，保留原句。已提交独立运行目录 `workdirs/controlled-memory-300-20261028-r1/`，准备时复用并验证 58 题的抽取存档，不复用其最终答案。新任务先等待至少 42 GiB 空闲显存和 GPU 租约，再执行原 SCRIPT 前 20 题重放验证，之后按预定阶段完成三组对照。实际是否已获 GPU、是否通过重放及当前进度，均以运行目录中的实时 JSON 为准。
