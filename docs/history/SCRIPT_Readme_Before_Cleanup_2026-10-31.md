# 清理前的项目 README 历史快照

归档于 2026-10-31。文中运行状态、默认方案及命令为历史记录；当前使用方式见仓库根 README。

# SCRIPT

2026-10-31，**普通查询默认使用 F（按需版），可切换 E（完全版）**。通用 CLI、Python 默认配置以及
`configs/script.yaml` / `configs/script_local.yaml` 已接入同一套 E/F 实现。

```bash
# 默认 F；也可显式添加 --memory F。
python script_qa.py query --base <base> --questions <questions.jsonl> --out workdirs/query-F
# 切换 E，使用独立输出目录。
python script_qa.py query --base <base> --questions <questions.jsonl> --out workdirs/query-E --memory E
```

配置方式：`query.temporal_qa_evidence: lazy_script` 为 F，`joint_entities` 为 E；也可使用
`--config configs/script_F.yaml` 或 `--config configs/script_E.yaml`。D 通过
`--config configs/script_D.yaml` 显式启用，历史完整输入使用 `configs/script_temporal_full.yaml`。
`--memory E/F` 要求 summary 来源的 `adaptive_partial_temporal` 管线，不能与 `--scheme4` 同用；
单独 `--scheme4` 仍切换到原方案 4。

E 复用联合七类抽取、最多一次角色复核、8 秒局部实体关联及 2 秒状态支持间隙；
F 复用问题字段规划、增量字段缓存及独立来源 batch 2（可配置 `field_extraction_batch_size: 1/2/4`）。
来源抽取不接收问题或选项；两种模式只替换最终 Description，保持问题、选项、旧时序记忆及已接受视觉观察。
抽取预算不足时保留原句并标记失败字段；上下文超限显式报错，不裁剪或改用另一模式重答。

F 默认缓存位于 `<out>/memory_cache/F/script.sqlite3`，可用 `query.memory_cache_directory` 指定共享缓存根目录；
按视频描述版本、来源原文和模型／代码版本隔离，跨问题改变选中片段也可复用同源字段，
当前问题只呈现已选来源与请求字段。缓存命中节省的是后续查询的实际抽取，已有构建成本仍属于原查询。
每题 `partial_temporal/` 保存 `semantic_sources.json`、`entity_evidence_audit.json`、`qa_context.json`，
以及 E 的 `joint_entity_memory.json` 或 F 的 `field_plan.json`、`lazy_script_memory.json`。
`result.json` 中的 `memory_variant` 和 `qa_evidence.mode` 标明实际执行版本。

新在线运行沿用共享上游取证和组织流程，其预算含上游开销，不等同于已封存的离线 300 题最终输入干预评测。
切换 E/F 后应使用新输出目录；原配置下的 `--resume` 才能复用原答案。G/H 暂保留独立实验入口。

2026-10-31 新增 **H：基于 F 的通用按需记忆**，与 E → G 对应形成 F → H。
保留 F 的字段规划、增量抽取、SQLite 缓存、逐采样呈现和独立来源批量执行；
将固定状态维度改为整类 `states` 请求及 `entity/description` 开放描述，并采用 G 的未知执行者规则。
实现为 `script_core/generalized_lazy_memory.py`，独立实验入口为 `script/run_generalized_lazy_experiment.py`。
H 使用独立数据库和版本，不复用 F/G 的抽取或答案；当前为代码实现与 CPU 验证，尚无真实模型功能或准确率结果。
设计、检查和准备目录见 [H 实现记录](../SCRIPT_H_Generalized_On_Demand_Memory_2026-10-31.md)。

2026-10-31 确认两条方向的默认方案：

| 方向 | 默认版本 | 定位 |
| --- | --- | --- |
| 完全版 | **E**（`joint-entities-E-300-20261029-r3`） | 对上游已选来源联合抽取七类信息，不按当前问题选择抽取字段；建立局部实体关联和实体时间线 |
| 按需版 | **F**（`lazy-script-F-300-20261030-r3`） | 根据问题和选项规划字段，按需增量抽取，使用 SQLite 字段缓存；已接入独立来源批量执行 |

A/B/C/D 保留为对照；G 延续 E、H 延续 F，作为独立实验版本继续记录，不自动替换各自默认版。
这里的「完全版」指已选证据范围内的全类别抽取；上游证据选择仍依赖问题，
不代表已经为整个视频预建问题无关的完整记忆。F 的问题依赖位于字段规划，来源抽取器只接收原文与所需字段。

E/F 的普通在线入口与切换方式见文首；独立离线实验入口仍为 `script/run_joint_entity_experiment.py` 和
`script/run_lazy_script_experiment.py`。通用入口接入不改写冻结实验或在运行任务。

2026-10-30 新增 **G：基于 E 的开放状态记忆**。保留八类属性、动作角色、关系及事件联系，
状态改为 `entity/description`，取消固定状态维度；执行者未知时不补写。G 沿用 E 的局部实体对应、
采样时间组织与来源保留，独立源码和产物不覆盖 E/F。实现与执行协议见
[G 结构设计](../SCRIPT_G_Generalized_Memory_Design_2026-10-30.md)。
当前 `workdirs/generalized-entities-G-300-20261030-r3/` 已冻结相同 300 题及原 QA 协议，
35 项 CPU 检查（含真实约束解码器）、14 条真实来源功能样例及原始 QA 重放全部通过，
已在 GPU 0 开始正式评测。r1/r2 的来源功能问题和全部花费保留。
状态见 `queue_status.json`，功能结果见 `validation/functional.json`，
同题比较及成本见 `report.json`。未完成前不报告全量准确率；当前通用入口默认 F，G 仍使用独立入口。

G 的独立执行优化见 [执行优化记录](../SCRIPT_G_Execution_Optimization_2026-10-30.md)。
`workdirs/G-execution-batch-20261030-r2/` 在原提示、逐来源约束和完整角色复核不变的条件下，
比较串行与 batch 2/4；78 条来源的真实验证已于 21:11 完成，29 项 CPU 检查通过。
batch 2/4 的观测速度分别为串行的 1.20/1.55 倍，但有 3/5 条来源结果发生变化，未通过完全一致检查。
该目录独立于原 G 正式评测，共享 GPU 耗时仅作观测，尚未接入原评测或默认查询入口。

2026-10-30 按用户要求，批量抽取作为 **F 的执行层优化**直接接入，不占用新的算法组编号。
`script/run_lazy_script_experiment.py` 默认每批处理 2 条独立来源；同一常驻模型复用原提示、
字段规划、每条来源专属 JSON 解码器、语义校验、SQLite 字段缓存和原文保留规则，
不拼接多条描述，也不合并来源词表。单条尾批继续走原调用；批量执行可能存在浮点计算差异，
不声称与串行输出逐字一致。模型故障不自动重试或降级重答。
每批耗时仅计一次，`field_extraction_model_calls` 统计实际模型调用，
`field_extraction_source_requests` 另计来源请求数，`extraction_batches` 记录批次数。
每题审计保存执行版本和批量大小；报告列出串行与批量执行的题数，不把两段耗时解释为受控加速比。

正在运行的 F 使用 `script/optimize_lazy_script_execution.py` 在模型调用之间接续，
保留原冻结源码、已完成答案、相同数据库和每题已消耗预算；接续产物及数据库快照见
`workdirs/lazy-script-F-300-20261030-r3/control/execution_optimization/`。
原独立测速目录 `workdirs/batch-extraction-speed-20261030-r1/` 的等待队列已取消，
未启动 GPU 工作者，64 条准备请求和取消记录保留；`script/run_extraction_batch_benchmark.py`
仅作为历史诊断工具保留，不再为此次执行优化额外排队。

2026-10-30 接入过 **D：紧凑 C 语义表示 + 状态时间段合并**；现为 `configs/script_D.yaml` 显式对照。
以下为当时的实现与评测记录：
配置为 `query.temporal_qa_evidence: state_segments`，合并允许的采样间隙为 2 秒。
语义抽取和最终呈现复用已完成评测的 C/D 代码；只替换最终 Description，保留原问题、选项、
系统提示、旧时序记忆、已接受视觉观察及回答协议。动作仍保留离散采样支持，必要原文继续呈现。
原版与紧凑版 ABC、D 的 300 题已完成；紧凑 A/B/C 分别为 186/188/186 题正确，D 为 188/300（62.67%）。
后续常规对照采用紧凑版 ABC；原版保留为历史参考。E/F 继续独立评测。
新查询沿用原在线预算，抽取失败或预算不足的字段保留原句并记录不完整状态；上下文超限显式报错。
该在线预算下的新运行不等同于已封存的离线 300 题评测。
每题 `partial_temporal/` 保存 `semantic_sources.json`、`semantic_memory.json`、`semantic_audit.json`、
`state_segment_memory.json` 和 `state_segment_audit.json`；`qa_context.json` 记录实际使用 D。
历史完整输入可显式使用 `--config configs/script_temporal_full.yaml`；`--scheme4` 继续选择原方案 4。

2026-10-30 F 修复版位于 `workdirs/lazy-script-F-300-20261030-r3/`，沿用同一批 300 题与原回答协议。
字段规划现在显式看到字段目录，必须保留 `actions.base` 作为推理所需的基础观测；目的、主题和意义
不能仅投影成显式 `purpose` 或 `changes` 字段。只有通过整句覆盖检查的简单事实可以省略原句，
字段未提及、未抽取完整、失败或不确定时都保留原文，所有上游已选描述均继续可见；空证据显式报错。
缓存版本更新为 `lazy-script-memory-v2-source-preserving`，独立数据库和新答案不复用旧版的规划/字段缓存。
旧 r2 的结果、数据库、已花费预算及停止记录保留；r3 单列并计入前两轮费用。
CPU 回放已检查 r2 前 185 题的 17,931 条描述，原有 36 题空描述在修复投影下均完整保留来源；
该检查不调用模型，也不使用答案或标签，不代表修复后的准确率。记录见 r3 的 `validation/source_coverage_repair.json`。
真实模型功能门检查增量字段、重复读取、工具/目的规划、空变化字段的原文保留及原始 QA 重放后，
在 GPU 3 独立执行新版本全量评测。`report.json` 新增 `source_coverage`，报告空描述数、来源行数与原文保留数。

当前保留原版 ABC、新格式紧凑版 ABC、在 C 上改进的 D，联合抽取和实体时间线 E，以及按任务存取的 F 的 300 题评测。`script/schedule_evidence_evaluations.py --parent-only --normalization-only` 调度 GPU 0/3 的共享抽取，新紧凑版与 D 在 GPU 3 共用同一个常驻回答模型，消费同一份抽取。共享调度状态见 `workdirs/controlled-memory-300-20261028-r1/control/throughput_normalization_only_20261029_r1/status.json`；各自进度和准确率见两套实验目录的 `report.json`。

2026-10-29 新格式紧凑评测位于 `workdirs/compact-evidence-300-20261029-r2/`：B/C 删除采样说明前言，统一使用 `Frame [start,end): ...`；C 继续保留分类语义字段或必要原文。固定原 300 题，复用已封存抽取，独立生成 A/B/C 答案；GPU 3 由 `script/run_shared_evidence_qa.py` 持续处理，模型跨实验、进度分段及等待新证据时保持常驻，全部完成后释放。调度接管记录见该实验的 `control/shared_C_D_20261029_r1/`。结果见该目录的 `report.json`，状态见 `queue_status.json`。带采样提示句的旧紧凑评测 r1 已按用户要求取消调度并删除运行目录；取消、费用摘要及共享抽取接管记录见原版实验的 `control/remove_old_compact_20261029_r1/`。

2026-10-29 20:17 原版 JSON 组的一次回答因共享显存突降而 OOM，原失败及成本保留，错误仍计错。21 时段已恢复共享抽取调度并接管原抽取 PID；原版剩余未执行回答由 `control/qa_after_compact_D_20261029_r1/` 等待紧凑版与 D 完成、释放模型后，以单个常驻模型继续，不重试已有失败答案。

2026-10-29 E 组当前版本位于 `workdirs/joint-entities-E-300-20261029-r3/`，入口为 `script/run_joint_entity_experiment.py`。相同来源逐条联合抽取七类，输出值受原文片段约束，必要时最多一次同源角色复核；否定/不确定原句直接保留。按局部词头、描述修饰及 8 秒窗口建立实体 ID，不确定对应另存；动作和状态按实体组织，状态重复支持间隙最多 2 秒。只对完整语法匹配的简单句省略重复原文，复杂句及未覆盖信息保留。GPU 0 的单个常驻模型连续执行 E 抽取与原协议最终回答；模型权重、精度、QA 配置及原 VideoTree 适配器保持原参考版本。已通过 44 项 CPU 检查、10 条真实模型功能样例及原始 QA 单例重放；按固定原顺序运行全部 300 题。报告包含 A/B/C/D/E 配对成绩、调用数、token、各阶段成本及按合并/别名/原文替换标记分组的结果。r1 的功能检查失败、r2 的源文本类型检查修订及其花费全部保留；r3 计入前两轮开发成本，未完整记账的中断调用墙钟时间估计单列。

2026-10-29 F 组位于 `workdirs/lazy-script-F-300-20261029-r2/`，入口为 `script/run_lazy_script_experiment.py`。沿用原有上游按问题选择的描述范围，仅规划当前题需要的字段，按视频、原文内容及抽取版本查询 SQLite；缺失字段才调用模型补抽并回写，后续任务可复用已有字段。区分尚未抽取、有值、来源未明确提及、不确定和失败；来源未提及不代表物理上不存在。事实保留采样时间和来源，当前 QA 只投影请求字段，汇总直接复用紧凑 C 的原始 `Frame [start,end): ...` 渲染器；不使用 E 的实体时间线。复杂信息暂不预抽，必要的失败/不确定来源保留原句。单个常驻模型连续完成抽取和原协议回答，固定同一批 300 题，模型和回答配置不变；23:24 按用户要求由 GPU 1 迁至 GPU 3，保留前 8 题结果、数据库已提交字段和原题已消耗预算，从第 9 题缺失字段继续。迁移中断调用约 1.41 秒的未落账墙钟估计单独保留并计入该题预算，迁移记录见 `control/migration_gpu3_r1/`。已通过 62 项 CPU 检查、真实模型增量字段/角色连接/重复读取检查及原始 QA 重放，已进入全量评测。数据库见 `database/script.sqlite3`，报告见 `report.json`；300 题来自 300 个视频，主要比较当前任务的存取与回答，重复查询仅作独立机制验证。r1 的额外来源筛选过窄、增量角色重复显示问题及其实际花费保留，r2 不再额外缩短原问题已选来源，并计入 r1 开发成本；修订依据为来源功能检查，未以答题准确率选择版本。

2026-10-29 D 组位于 `workdirs/state-segments-D-300-20261029-r1/`，入口为 `script/run_state_segment_experiment.py`。复用 C 的同一份语义抽取，将事实独立去重存储，按实体名称、状态维度/属性/关系及取值组织时间段；局部重复支持的间隙最多 2 秒，连续性及名称对应按推断记录，原采样点和来源引用保留，值改变后重开一段。动作样本保留离散时间，不据此推断事件次数。必要原文继续保留，合并后的属性或关系另按时间段呈现。固定原 300 题，仅新增 D 回答，并与紧凑版 A/B/C 的已有答案逐题比较；没有新的模型抽取调用。D 的 `report.json` 同时统计准确率、配对变化、token、耗时及继承抽取成本。

2026-10-28 新增[原 SCRIPT 协议下的受控记忆实验设计](../Controlled_Memory_Experiment_2026-10-28.md)：
A 原样重放、B 完整原文无损重新排版、C 相同容器中的语义表示。只改变最终 Description 区域，
保持原提示、旧时序记忆、视觉观察、生成参数及解析协议不变；A 为主对照，B 用于区分排版与语义表示的影响。
`workdirs/controlled-memory-design-300-20261028-r1/` 已准备 A/B 的 300 题输入并通过 CPU 审计：
原调用完全一致、固定区域不变、30,141 条已读描述可无损还原。随后已按用户授权完成 C 渲染、100 条来源盲审和 37 项检查，
并提交 `workdirs/controlled-memory-300-20261028-r1/` 的独立调度器，达到 42 GiB 空闲显存门槛后先重放 A 前 20 题。
数据结构见[语义记忆的数据结构与整理流程](../Semantic_Memory_Data_Structure_2026-10-28.md)，运行状态见新目录 `queue_status.json`。

2026-10-27 的上游入口为 `adaptive_partial_temporal`（2026-10-30 曾默认采用 D，现默认 F、可选 E）：
按问题选场景，读取已选场景的完整 summary，选择性抽取当前需要的实体、属性、独立观测和局部关系，按需少量补看，
再对这些已有证据判断观测连续性、跨场景实体对应并组织状态历史，最后只生成一次答案。
时序阶段使用已读取的相同描述和已核验的补看观察，不另选场景、不新增看图；对应与连续性保持文本推断属性。
原局部抽取版可显式使用 `--config configs/script_partial.yaml`。
最终输入的新对照模式可显式使用 `--config configs/script_memory_quotes.yaml`：
以实体时序记忆为主体，只附记忆实际引用且上游已读取的少量原始描述，以及原先已接受的视觉观察，
不再把全部已选场景描述传给最终回答。默认每个事实最多 2 句、全题最多 12 句，保留原文、时间戳和关联事实 ID；
优先保留状态转移两端，并单独保留有依据的动作、工具来源。引用额度不足、空记忆和未附原句的事实显式记录，
不会自动补入未建图的描述。检索、实体抽取、时序组织和视觉补查沿用原流程，仍只生成一次最终答案。
配置项为 `query.temporal_qa_evidence: memory_quotes`、`temporal_qa_max_quotes`、`temporal_qa_quotes_per_fact`；
最终引用清单位于 `partial_temporal/qa_context.json` 的 `evidence_selection`，摘要位于 `result.json` 的 `qa_evidence`。
历史完整输入配置为 `configs/script_temporal_full.yaml`（`full`）；当前默认为 F（`lazy_script`），可切换 E（`joint_entities`）或显式 D。冻结实验及其结果不变。旧稀疏引用实验因覆盖缺陷已按用户要求删除，不纳入有效实验对比。

此前覆盖修复版使用 `configs/script_complete_memory.yaml`（`complete_with_quotes`）或
`configs/script_complete_structured.yaml`（`complete_structured`）。修复版从上游**实际读到的全部描述**重新建立动作、
属性、条件状态和关系，而非只处理旧实体图引用过的描述；规范化阶段不提供问题、选项、答案，也不额外读未选描述或图像。
每条已读描述必须对应原子事实或最终可见的分类原文，否则覆盖校验失败。无法可靠结构化的修饰、否定、工具和对象细节保留原文，
未知角色保持空值。动作作为带采样时间的事件断言，避免旧状态合并仅留下首条描述；重复观测保留离散采样区间，不推断连续发生。
来源归档、语义事实和最终呈现分开保存为 `source_archive.json`、`complete_memory.json`、`source_coverage.json`、`qa_context.json`。
覆盖率只说明来源有去处，不等于语义提取已独立验证。实体名称索引也不证明两个同名物体是同一实例。
两种模式共用完整重建的记忆和原有已接受视觉观察，前者另外附最多 12 条按时间分布的辅助原句，后者将必要原文留在分类字段内。
若完整记忆超过上下文额度，显式记录回退原始完整输入，不通过静默删减信息来满足限制。

配对重跑位于 `workdirs/complete-memory-repair-300-20261027-r1/`，固定原来 300 题及 30,141 条已读描述，
共 24,134 个去重证据单元。沿用**原始 62% 对照的提示、选项顺序及答案解析协议**，同题重放完整输入与两种修复方案。
原始 300 题对照为 **186/300（62%）**。旧稀疏原句及其二次规范化实验存在证据覆盖缺陷，
其运行数据、配套完整描述对照和原型尝试已按用户要求删除，不纳入实验对比。
原始输入已逐题验证可精确重建，每个分片前 4 个重放预测必须匹配历史记录；严格单字母解析仅单列审计，避免混用评分口径。
GPU 0 的分片继续运行；GPU 2 的分片因共享显存突降而 OOM，其失败和花费原样保留，
第一次恢复在 GPU 2 上再次遇到共享显存突降，失败和花费另列；第二次恢复固定等待 GPU 0 当前分片释放租约，
并连续 6 次、每次间隔 10 秒满足 48 GiB 空闲后启动该分片，使用同一冻结推理源码，避免重复抢占 GPU 2 的短暂空闲。
合并进度见 `current_report.json`，恢复状态见 `resource_recovery.json` 指向的目录；`report.json` 保留首次尝试的进度。
该旧实验已停止，不报告其未完成的全量准确率。

更细的数据组织可使用 `--config configs/script_typed_memory.yaml`（`typed_memory_quotes`）：
动作、属性、语义状态、关系、时序变化、因果、身份对应、组成/派生关系和未分类信息分别存放。
属性保留 `field/value` 及观测时间；动作保留原来的 `kind`、参与实体和原句，未单独抽出的施事、动作词、对象、工具保持未知。
原 `states` 容器只是时序组装结果，不再把其中的动作描述自动当作语义状态；时序索引和语义记录分开保存，
变化另外区分 `state_change`、`action_sequence` 和 `unclassified_change`。
来源（caption/采样视觉）、上游断言或文本推断状态、原始引用和分类依据与语义类型分别记录，未分类内容保留。
`qa_context.json` 的 `final_memory` 保存分类后的最终记忆，`evidence_selection.information_types` 保存各类数量和属性字段统计。
`query.temporal_qa_information_types` 可指定保留类别，对应视觉观察和引用也按类型筛选；实体索引始终保留。
同一句原文可能同时支撑动作和关系等多类，消融审计会列出 `retained_sources_overlapping_excluded_types`，
因此移除某一集合不一定完全消除该类的语义信息。记录数量及分类依据都不等于重要性，贡献需要同题、同来源的实际回答对照。

旧稀疏记忆与后续规范化 300 题实验（含 r1 原型和 r2 正式运行）已于 2026-10-28 按用户要求删除。
删除范围记录见 `workdirs/memory-evidence-two-schemes-20261027/manifest.json`。

```bash
# 为已有完成题生成新模式输入预览：仅 CPU，不调用模型、不产生新答案。
python script/preview_memory_quotes.py \
  --run workdirs/full-temporal-videotree-500-20261027-r1 \
  --out workdirs/memory-quotes-preview-new

# 分类预览及固定同一批题的“移除动作类”预览，均不调用模型。
python script/preview_memory_quotes.py --typed \
  --run workdirs/full-temporal-videotree-500-20261027-r1 --out workdirs/typed-memory-preview-new
python script/preview_memory_quotes.py --typed --exclude-type action \
  --cohort workdirs/typed-memory-preview-new/summary.json \
  --run workdirs/full-temporal-videotree-500-20261027-r1 --out workdirs/typed-memory-no-action-preview-new

# 在独立目录运行新模式；base 和 questions 与普通查询使用相同来源。
python script_qa.py query --base <base-directory> --questions <questions.jsonl> \
  --config configs/script_memory_quotes.yaml --out workdirs/script/memory-quotes-query
```

原方案 3 可显式使用 `--config configs/script_scheme3.yaml`。
显式添加 `--scheme4`，或配置 `query.pipeline: scheme4`，可运行原方案 4：在原方案 3 取得的相同证据上建立跨阶段实体对应与状态历史，
只生成方案 4 的一次最终答案，不额外生成方案 3 的对照答案；跨阶段处理不另选场景、不额外读取图像。
实体始终从原始带时间戳 summary 提取，跨阶段对应保留文本推断属性。默认在线预算 1500 秒，最终回答预留 45 秒，
局部检索、实体提取、按需补看和时序组织共用这一个在线预算；目录构建另计且最多 600 秒，共享准备工作只计费一次。
历史实验保留冻结源码和结果；已取消的队列不自动恢复。
相同 300 题开发集上，当前时序版为 186/300（62%），局部抽取对照为 183/300（61%）；
计入来源准备的平均耗时分别为 49.60 和 45.59 秒。时序版零运行错误，但保留 103 题来源提取不完整记录，
新增的 1 个百分点尚不足以确认稳定提升。完整对照见 `workdirs/comparisons/partial-temporal-vs-five-20261027/`。

2026-10-27 已启动完整公开标注集 500 题的补齐与 VideoTree 同题对照，目录为
`workdirs/full-temporal-videotree-500-20261027-r1/`。原时序实验 300 题以带哈希的来源引用保留，
剩余 200 题使用冻结的默认入口完成取证、时序组织和一次最终回答；两部分分别报告，合计固定 500 题。
VideoTree 在本机未找到旧 500 题答案，本轮重新运行同一批全部 500 题，使用相同的原问题、选项、逐秒描述、
EVA 特征和本地 Qwen2.5-VL-7B。保留原仓库的宽度、深度、提示、索引和答案解析行为，解析回退单独计数。
这不表示复现原论文 GPT-4 成绩，也不是涵盖无公开答案 5031 题的完整隐藏测试集评测。

后台先以 3 个 CPU 工作者补齐新增视频帧，再依次执行 EVA 特征、默认时序版 200 题及 VideoTree 500 题。
最多同时一个 GPU 工作者，连续三次观察足够空闲显存后以 UUID 绑定；特征和 VideoTree 门槛为 40 GiB，
默认时序版为 56 GiB。显存耗尽或基础设施异常会保留记录并停止，不自动重置或重试已花费预算。
随后按用户要求，当前队列固定在物理 GPU 0；共享特征完成后，默认时序版 200 题与 VideoTree 500 题两个推理工作者
同时启动、同卡并行。联合启动门槛为连续三次观察到至少 56 GiB 空闲显存，GPU 租约由两个任务共同持有。
已运行的 GPU 0 特征进程保持原 PID，调度接管和并行控制验证记录位于 `control/gpu0/`；
此调度覆盖不修改冻结的推理源码、输入或已有结果，实际耗时包含同卡并发的计算竞争。
12:28 两个任务实际启动后，时序版首题于 12:30 因其他进程显存增长而 OOM 退出，VideoTree 保持运行。
按用户要求现由 `control/listen_gpu0/` 的后台监听器每 30 秒观察 GPU 0，连续三次至少 56 GiB 空闲后，
启动 `recovery/script-resource-retry-r2/` 中的单次独立恢复尝试；不停止 VideoTree，不修改算法源码。
原失败调用和 79.03 秒查询处理成本完整保留，报告另列失败花费与所有尝试累计花费，不当作免费续跑。
用户随后要求直接运行。12:50 已启动该恢复尝试，并通过仅对本进程生效的运行时资源适配，将在线检索
不用的 EVA 视觉塔留在 CPU，仅文本塔和原投影层上 GPU；原权重、精度、分词和文本计算均保持。
历史 6 条文本向量逐元素完全一致（最大误差 0），独立文本塔检查的 GPU 峰值为 1.33 GiB。
资源适配与验证见 `control/eva_text_gpu_only/`，实际启动记录见 `control/listen_gpu0/direct_launch.json`；
当前恢复入口的显存门槛为 24 GiB、单次观察直接启动，VideoTree 的进程与推理代码保持原样。
当前进度见 `queue_status.json`、`workers/*.json` 与 `logs/`；
`reports/comparison_{100,300,500,additional200}.json` 和 `reports/per_question.csv` 自动持续更新。
未完成时最终 `accuracy` 留空，错误计错，待执行题单列；冷启动和共享离线预处理另计。

```bash
# 只刷新本轮报告，不启动、续跑或重试任何推理。
workdirs/query-runtime-env/bin/python script/run_full_temporal_videotree.py report \
  --out workdirs/full-temporal-videotree-500-20261027-r1
```

普通查询可直接复用已有 summary 和匹配编码器的逐秒特征缓存，也可使用冻结实验根目录的输入包：

```bash
python script_qa.py query \
  --base workdirs/script-summary-temporal-four-arm-300-20261022-r3 \
  --questions workdirs/script-videotree-repair-20261015/smoke/questions.jsonl \
  --out workdirs/script/partial-temporal-query --resume

# 开启方案 4；使用独立输出目录，避免混用两种方案的断点记录。
python script_qa.py query \
  --base workdirs/script-summary-temporal-four-arm-300-20261022-r3 \
  --questions workdirs/script-videotree-repair-20261015/smoke/questions.jsonl \
  --out workdirs/script/scheme4-query --scheme4 --resume
```

该路径要求完整的原始逐秒描述及五个原始选项；不会为加载缓存而重读原视频。
默认记录位于每题目录的 `directory_calls/`、`partial_search/`、`partial_memory.json` 和 `partial_temporal/`；
`partial_search/evidence_bundle.json` 在回答前导出原始证据，`partial_temporal/network.json` 保存时序网络。
原始证据的不完整记录保留为 `source_incomplete`，影响最终充分性判定。
原方案 3/4 使用 `summary_search/`，仅原方案 4 额外生成 `cross_stage/`，最终结果为 `result.json`。
完成结果可断点复用，来源、问题和配置必须一致；没有完成证据导出的中断保留已花费预算，需换新输出目录重跑。
需要旧常规流程时显式传入 `--config configs/script_coverage.yaml`；其他旧配置也明确设置 `query.pipeline: legacy`。

本机运行（Conda `vts` 环境，EgoSchema 和权重已准备）：

```bash
# 四方案真实模型功能检查：默认 2 个完整视频，方案 3、4 配对使用相同证据。
# --gpus 填当前空闲的物理卡号；运行器检查占用，并用 UUID 绑定显卡。
bash script/run_local_smoke.sh all --gpus 2,3,4,5 \
  --output workdirs/dev-four-arm-smoke-r7

# 普通默认 adaptive_partial_temporal 查询；已完成且来源一致的结果可 --resume 复用。
bash script/run_local_smoke.sh script --output workdirs/dev-four-arm-smoke-r7

# 显式开启原方案 4，结果分别写入 adaptive_partial_temporal/ 和 scheme4/。
bash script/run_local_smoke.sh script --output workdirs/dev-four-arm-smoke-r7 --scheme4
```

数据位于 `data/EgoSchema/{egoschema,egoschema_frames,egoschema_features}`，
四个模型和 Hugging Face 缓存位于 `checkpoints/`。本机脚本设置缓存路径和离线加载，
自动生成匹配编码器的逐秒帧/特征缓存，保留全部原始描述和选项。未指定显卡时只选择空闲卡。
检查结果在输出目录的 `smoke/arms/`、`adaptive_partial_temporal/`（开启开关时为 `scheme4/`）、`worker_logs/` 及 `smoke/status.json`；
`evaluation_only.json` 的答案仅供事后评分，不进入推理输入。
这条命令是小样本功能检查，不会自动启动上文历史的每组 300 题实验。
修改源码后或推理中断后请换新 `--output`，保留旧日志和已花费预算。

此前方案 4 默认入口的历史验收：423 项测试通过，四方案各 2 个完整视频（共 8 次）均无运行错误，
普通方案 4 入口另外 2 次通过；断点复用未新增推理调用，预算及③④同证据检查通过。
详细记录为 `workdirs/dev-four-arm-smoke-r5/verification/final_report.json`。
这是功能验收；300 题评测尚未运行，普通方案 4 的两例仍如实标记 `evidence_status: insufficient`，
不能据此声称全数据集效果达标或所有潜在 bug 已排除。

本机四卡 300 题评测可使用独立后台运行器：

```bash
conda run -n vts python script/run_four_arm_300_local.py launch \
  --out workdirs/four-arm-300-20261022-r2 --gpus 2,3,4,5 \
  --asset-gpu 0 --feature-batch-size 16 --frame-workers 4 \
  --verification workdirs/source-language-verification-20261022
```

四方案分别固定一张卡，方案 4 在方案 3 导出证据后立即处理，不读取方案 3 的答案。
额外的空闲卡只准备共享逐秒特征；帧并行预取、特征按批提取，每个视频准备好即发布，
推理不用等待全部 300 个视频准备结束。各方案模型跨题常驻，结束后自动释放对应显卡。
运行器先检查卡上无其他任务，以 UUID 绑定设备，冻结源码后通过独立 tmux 会话运行。
同名输出目录不能重新启动，以免覆盖已花费预算。四组共用一份固定 300 题清单；
当前本机缺少历史 `cohort.json`，清单按仓库原 100 题加种子 0 扩充 200 题规则重建，
不声称已与缺失的历史清单逐项核对。
查看输出目录的 `progress.json`、`workers/*.json` 和 `logs/*.log`；全部结束后生成 `comparison.json`。
错误保留在每题 `result.json` 中，评分分母固定 300。后台会话名称在 `launch.json`。
本机当前使用 r2：r1 首道新题发现模型将英文实体名翻译成中文，已停止并保留日志。
新增原文短语解码约束并明确保持来源语言；432 项测试及失败材料的真实 GPU 复验通过后，
r2 使用完全相同的 300 题清单重新开始，各组结果不与 r1 混用，共享帧/特征缓存可复用。

2026-10-23 错误修复与补跑位于 `workdirs/four-arm-300-20261023-repair-r3/`。
旧 r2 推理进程已停止，原始结果、调用记录及中断位置保留在旧目录；
新目录通过符号链接引用成功结果，失败及未完成题使用修复版冻结源码重新执行。
实体抽取使用较小分页；语义重试将实体名称绑定到确实提到它的原始字幕，
无依据的可选关系和原因单独记录，未知来源不会变为有效证据。
③证据不完整时先重做③，再让④消费同一个完整证据包；不绕过配对检查。
每次重试保留独立调用预算，旧失败尝试归档到 `attempts/`，并保留旧实验引用；
原始花费不会被当成免费续跑。该目录是混合源码版本的修复汇总，不应标成原 r2 的冻结实验结果。
后台用两张已释放的 GPU 执行前两组与③④配对组，最多三轮尝试；
`progress.json` 区分已完成、成功和失败，`finished.json` 仅在后台任务结束后生成。
查看当前进度：

```bash
python script/run_four_arm_repair.py status \
  --out workdirs/four-arm-300-20261023-repair-r3
```

2026-10-23 本机新增原仓库 VideoTree 对照，后台目录为
`workdirs/videotree-original-500-20261023-r1/`，覆盖公开答案子集全部 500 题。
`cohorts.json` 的前 100 题、300 题严格沿用当前 `four-arm-300-20261022-r2/cohort.json`，
已逐项核对问题、五个选项、原始描述及顺序；剩余 200 题追加在后。
入口为 `script/run_original_videotree_local.py`，冻结 `../VideoTree` 源码，
调用其原始宽度入口、深度函数和 QA 入口。推理使用与当前实验相同的本地 Qwen2.5-VL-7B，
不声称复现论文 GPT-4 成绩。仅在冻结副本中修正 Kmeans 的可选依赖导入和现代 Torch 的单元素索引形状；
原版提示、caption 索引偏移、相关性与簇的对应行为及 QA 解析回退均保留，回退单独标记。
物理 GPU 0、7 分片推理，GPU 1 补齐共享 EVA 特征；已有任务继续运行。
查看 `progress.json`、`workers/`、`logs/` 和逐题 `runs/<video_id>/result.json`。
`reports/videotree_100.json`、`videotree_300.json`、`videotree_500.json` 分别汇总；
未跑完时最终 `accuracy` 保持空值，失败题计错，分母固定。
`reports/comparison_100.json` 和 `comparison_300.json` 持续汇总双方已完成同题的预测和错题变化；
推理耗时不含共享离线预处理和模型加载，且显卡不同，不能直接据此归因算法速度差异。
新实验启动示例（必须使用新输出目录，显卡必须空闲）：

```bash
conda run -n vts python script/run_original_videotree_local.py launch \
  --out workdirs/videotree-original-500-NEW \
  --reference workdirs/four-arm-300-20261022-r2 \
  --videotree ../VideoTree --gpus 0,7 --asset-gpu 1
```

2026-10-22 r3：按用户要求增加“深入探究”对照，共四组，每组相同 300 题：
① 单场景原始 summary 回答；② 同一场景加局部文本实体网络；③ 按需深入读取更多场景的 summary，
各场景实体保持独立，必要时按原上限少量补看；④ 复用③完全相同的 summary、读取范围和补看观察，
只额外建立跨阶段身份对应与实体历史。③→②比较深入探究（含按需补看），④→③比较跨阶段记忆组织。
④只读取③在回答前导出的证据包，不读取③的答案或置信度；不再读取图像。
共享的原始检索、提取和补看成本仍计入④原有预算，避免缓存回放产生免费预算或夸大速度收益。
③④由同一 GPU 分片成对执行，前两组独立；最多两个未结束作业。新目录为
`workdirs/script-summary-temporal-four-arm-300-20261022-r3/`，此前 r2 三组检查与调度已停止。

<details>
<summary>历史方案、修复与运行记录（旧报告已移至 docs/history/）</summary>

以下为历史实现和实验记录；旧命令应使用对应冻结源码与配置。当前默认 F、可选 E 及预算以本文开头为准。

2026-10-22 r2（历史修订）：用户要求给第三组增加尽量少用的按需补看。实体与关系仍直接从原始 summary 提取；
仅跨场景组在 summary 导航结束、仍有具体且影响选项区分的可见信息缺口时，才允许一次补看。
默认最多读取 3 张已有缓存视频帧、定位窗口最多 8 秒、补看全部处理最多 60 秒，均计入原有每题预算。
前两组不读取图像。补看只产生局部观察，不创建视觉实体图；独立视觉检查器不接收问题、选项或 summary，
只有整条命题通过检查且引用真实输入帧时才作为最终回答的补充证据，原描述及文本实体网络保持来源属性。
新目录为 `workdirs/script-summary-temporal-sparse-300-20261022-r2/`；旧 r1 验证与调度已停止并保留记录。
CPU 回归和独立 GPU 检查通过后才继续正式实验；普通题目未触发补看也视为合法路径。

2026-10-22 r1（历史修订）：用户要求直接从原始带时间戳的 summary/逐秒描述提取实体、
属性、状态和关系。r7 仍使用看图提取，执行偏离要求；其监督器和未结束任务已停止，结果保留为旧视觉提取实验。
当前 `run_temporal_entity.py` 默认使用 `query.temporal_entity_source: summary`：视觉特征仅用于连续场景分段和中心检索，
进入场景后向提取模型提供完整原始描述，不提供图片、问题或选项。跨场景对应使用双方实体各自的描述引用，
身份对应保留为文本推断；不会因名称相同就确认同一物体，也不自动假定跨场景状态持续。
新流程保留原始文字、时间与引用，未记载的属性、变化和原因保持未知；r1 不自动补看图。
新实验使用 `workdirs/script-summary-temporal-300-20261022-r1/`，不回填或混合旧 r7 的结果。
详见[2026-10-22 summary 提取纠正](SCRIPT_Temporal_Entity_Relation_Plan_2026-10-19.md)。

此工作区新增了方案文档中的 SCRIPT 研究原型，入口为 `script_qa.py`。
原有 VideoTree 的 `demo.py` 与实验脚本继续保留在下文。

当前确认的完整主线见[连续场景与时序实体关系网络详细方案](SCRIPT_Temporal_Entity_Relation_Plan_2026-10-19.md)，
包含场景簇表示、实体属性、状态持续时间、变化操作、跨场景身份对齐、查询流程、实现差距与验证建议。

2026-10-19 用户重申的主线约束：先聚类形成场景，再按问题进入场景并按需建立局部关系。
场景簇必须是时间上连续的区间，依次覆盖视频；视觉特征相似度用于确定切分边界，不能把不相邻时段合成一个簇。
相似场景在之后再次出现时建立新簇，通过导航指针或对象状态线索关联；层级细化也须在父区间内连续切分。
复用视觉特征与原始描述、帧和时间引用；原 VideoTree 无时间约束的相似性聚类仅保留为历史基线对照，
不能直接作为满足本主线约束的分段器。现有 `segmentation.contiguous_partition` 可作为连续视觉特征分段的实现基础。
“全部秒级描述直接回答”仅作为已完成的诊断消融，不作为主方案。
每簇保存中心检索特征、帧按与中心相似度的排序、原始时序，以及覆盖整簇的场景描述；相似簇间指针支持双向导航。
进入簇建立复杂关系时，为每个可观察实体建立档案，保存可获取的结构、组成、纹理、外观、颜色等属性及各自来源。
关系记忆组织为时序实体关系网络：实体保留稳定身份，状态只在发生有依据的变化时新增，每个状态记录起止秒数和当时的关系。
前后状态通过变化事件连接，记录导致变化的具体动作、执行者、工具、操作时间和证据；关系边也有生效与结束时间。
仅补充已有状态的属性观测不自动生成物理状态变化；状态起止不确定时保存边界范围，变化原因未知时不得补造。
查询以实体属性、状态和时间为入口；跨场景先做实体身份对齐，再挂接新状态、动作和新关系，保留旧关系的历史时间。
相同实体可跨场景延续，拆分、组装或材料转化则保留新实体及其来源/组成关系。
没有来源支持的前态、后态或变化原因必须保持未知，描述推断不升级为视觉事实。
普通动作及重复细节保存在原始场景材料中，按问题需要读取，不默认全部提升为核心关系。
当前 `scene_memory` 仍用描述驱动的连续阶段划分，尚未接入上述连续视觉特征分段，局部图仍是通用事件/角色抽取；
`key_memory` 的变化线索只是附加索引，尚未实现上述实体属性档案、完整状态有效区间和跨场景时序网络。正在运行的 r2 是该旧实现的修复复验，
其冻结源码和已有结果保留，不能将它表述为上述新约束已经实现或已验证。
详见[主线约束与现有实现差距](SCRIPT_Key_Memory_Experiments_2026-10-18.md)。

2026-10-19 修复场景记忆的两处运行问题：分段输出必须保留视频末尾，避免细碎场景耗尽数组上限后整段回退；
全局及跨阶段问题增加停止前的必要时间覆盖检查，达到访问上限仍不足时保留不足状态。
首轮五题 GPU 检查的 15 次新路径均无目录回退，全局题已跨场景展开，但暴露了联合读取的上下文超限和实体对应引用错误，
因此完整复验未放行。追加修复去除重复引文及长存储 ID；对应解码要求两侧各自引用所选实体的来源。
仍超限时显式分批关系图，每批保留全部已打开描述、关键信息和跨场景对应，所有批次同意才允许停止。
305 项 CPU 回归与 48 项定向检查通过；真实 tokenizer 审计的 35 次判断、15 次 QA 均符合原输入上限。
第二轮 GPU 检查及通过后的四组各 300 题复验已提交，结果使用新目录
`workdirs/script-key-memory-repair-300-20261019-r2/`，尚无新准确率。原 VideoTree QA 和选项顺序保留。
详见[修复与复验记录](SCRIPT_Key_Memory_Experiments_2026-10-18.md)。

新增独立的关键信息记忆和分块分层导航：保留状态变化、交接、异常、区分性属性及材料转化线索，
复杂关系继续按查询建立；四组各 300 题，比较原 VideoTree、场景流程、关键信息和分层模式。
详见[关键信息记忆实验](SCRIPT_Key_Memory_Experiments_2026-10-18.md)。

新增按需展开的场景记忆路径：初始目录完整保留逐秒描述，只建立轻量场景和转换指针；
查询先打开一个场景，进入后才抽取局部关系，有具体缺口时才沿指针扩展并建立文本实体对应。
首轮固定 300 题，与新执行的原 VideoTree 对照，见[场景记忆实验](SCRIPT_Scene_Lazy_Experiments_2026-10-18.md)。

新增 SCRIPT 主查询入口内的 VideoTree 兼容模式：关闭扩展时调用原宽度、深度和 QA，
随后用九组配置分别检查图检索、反馈、局部视觉、答案保护、QA 替换和排序。
先逐题验收历史成功样本的原树、提示和预测，再释放正式消融；
详见[框架内兼容实验](SCRIPT_VideoTree_Compatibility_Experiments_2026-10-18.md)。

2026-10-18 保持修复后常规读取为默认。最新 300 题为 171/300（57.0%）；
原始每秒描述完整进入正常回答，但未从描述预建实体关系，在线实体表也为空。
详见[常规错题与 summary 利用分析](SCRIPT_Coverage_Summary_Analysis_2026-10-18.md)。

新增从原始秒级描述提取实体提及、事件角色与时间关系的可选模块，
通过新鲜常规对照、仅图辅助回答、图辅助后续检索与回答、基线保护四组验证。
图保持未核验描述身份；默认仍为修复后的常规读取。
详见[描述实体事件图实验](SCRIPT_Summary_Graph_Experiments_2026-10-18.md)。

2026-10-17 已提交从 VideoTree 逐步迁移到 SCRIPT 的 9 组固定证据实验，每组原固定 300 题。
分别替换描述筛选、QA 接口、选项排序、窗口组织、两种视觉事实，再检查基线保护和联合停止模块；
首轮诊断因 JSON 配置解析问题中断，已修复并于 2026-10-18 重新提交；223 项 CPU 检查通过。
先做原版完整路径和兼容性诊断，通过后自动运行全组，
包括现有任务在内最多同时两张 H200。详见[迁移实验设计与作业](SCRIPT_Migration_Experiments_2026-10-17.md)。

2026-10-17 新增每秒描述优先的少看流程：直接读取去除窗口重叠的原始描述时间线，
先给出带描述引用的初判，仅对会影响答案的可见缺口补看，每次最多一个窗口、总计最多三次。
没有补看时直接复用初判；补看后的最终回答保留完整描述并独立选择，记录初判与终判的变化。
212 项 CPU 检查通过；固定 300 题的 54,000 条描述已逐条验证文字和时间完整保留。
新少看复验已完成：142/300（47.33%），平均 31.36 秒/题，1 道失败。285 次成功初判全部返回空检查，
新少看分支实际未补看，且描述 QA 本身退步，不能视为已验证的改善。修复后常规组已完成：171/300（57.0%），2 道失败。
两组共享冻结源码和 M0；详见
[实现与验证记录](SCRIPT_Caption_First_2026-10-17.md)。

2026-10-17 的最新修复针对固定 300 题中的错误案例：默认用简短最终选择替代五项比较表，
按选项内容确定展示顺序并恢复原编号；剧本引导读取每次细看后根据核验反馈重新规划，
两组最终均使用完整描述时间线。视觉核验新增完整命题检查，部分支持不能升级为整条事实。
204 项 CPU 回归检查通过；复用前轮相同 M0 的两组各 300 题复验已完成，结果位于
`workdirs/script-badcase-repair-300-20261017-r2/`。少看策略为 163/300（54.33%）、
平均 68.60 秒/题；常规读取为 169/300（56.33%）、156.08 秒/题；两组各有 2 道失败，保留计错。
同题历史 VideoTree 为 195/300（65%），其缓存及建库条件与本次在线时间不完全相同。
当时普通 CLI 入口的 `configs/script.yaml` 默认使用修复后的常规读取（`coverage_first`），
关闭描述优先少看分支；显式常规配置为 `configs/script_coverage.yaml`，少看配置为
`configs/script_guided.yaml`。当时低层 `load_config()` 无文件时同样默认覆盖读取，
已有冻结实验配置不变。少看配置及回退流程的 12 项针对性检查通过。
详见[最新少看性能分析](SCRIPT_Guided_Performance_Analysis_2026-10-17.md)、
[上一轮错误案例分析](SCRIPT_300_Badcase_Analysis_2026-10-17.md)和
[修复与复验记录](SCRIPT_300_Badcase_Repairs_2026-10-17.md)。下文早期实验数字保留其原始范围。

2026-10-16 的 V7c 修复已通过 141 项 CPU 测试，GPU 复验目录为
`workdirs/script-videotree-repair-v7c-20261016/`。先运行 6 道固定开发题诊断，
完整性审计通过后自动运行原固定 100 题；模型、600 秒预算和最多 8 轮保持不变。
本轮已完成 100/100 题：SCRIPT 正确率 52%，平均 134.3 秒/题；V5 为 50%、172.7 秒/题。
正确率提高 2 个百分点，平均耗时降低 22.2%；修正 17 道旧错题，同时丢失 15 道旧对题。
四组完整性审计均通过，内部工具错误、超时和预算超支均为零；复用的 VideoTree 同题结果为 59%。
仅提交两个单卡作业：第一张卡先诊断，通过后在同一分配中继续跑 50 题，
同时释放第二个作业的挂起状态，使其申请另一张卡跑其余 50 题；诊断失败则取消第二个作业。
重复事实保留历次核验并传播冲突；纯黑帧记录为无效观察，不作为事实或有效覆盖。
事实区分状态、动作和推断，动态动作要求过程证据。选择题保存选项原文与独立解释，
逐项核验使用固定选项键，并显式保留长共同前缀之后的区别。
后续观察按选项缺口检索，记录新增区分信息；充分性仍由独立证据条件检查。
`comparison.json` 提供实际进度与最终结果，`smoke/status.json` 和
`shards/*/integrity_report.json` 提供工程验收，`run_summary.json` 提供完整汇总。
审计报告与执行状态使用不同文件名，避免汇总读取被覆盖；原推理源码快照保持不变。
修复清单位于 `docs/history/SCRIPT_Evidence_Repairs_V7c_2026-10-16.json`。

2026-10-15 的修复保持原模型、600 秒在线预算和最多 8 轮任务。
全局活动题先按预算安排默认 6 个分散时间点，优先观察前、中、后段，剩余轮次
用于选项区分；真正要求全部、计数或最后一次的题仍保留严格覆盖条件。
反复或转折问题另需跨时间事实支持，选择题停止前逐项核验选项及事实引用。
整题证据不足时，最终回答仍可读取独立核验的局部事实，内部不足状态继续保留。
不在实际观察区间中的实体角色不能进入已支持事实；单题异常单独记录并继续后续问题，
有答案标注的失败题计为错误。

模型上下文省略存储元数据和无关实体历史，保留事实文本与所有输入帧；图像仍逐帧标注
实体 ID。必要时按实际 token 数缩小图像并记录模型实际输入尺寸，覆盖检查使用这些尺寸；
文本评估超限则显式分批，不静默截断事实。后续方案实验使用已固定的 300 道开发题，
修复结果使用新目录和源码快照，不覆盖旧实验。少量定向诊断题用于运行检查，不能代表整批准确率。

观察模块现在接收全部候选选项和检查目标，优先提取工具、左右手、准备步骤及顺序等区别；
候选文字不代表已见事实，提出的事实仍由不接收选项的视觉核验模块独立检查。
观察缺口带原视频区间保存，并传给后续规划、语义核验和最终回答；缺口不作为反证。
输出缓存同时区分问题、选项、检查目标和观察目标，防止把泛化描述复用于不同的细节检查。

`configs/script_guided.yaml` 显式启用剧本引导少看策略，并提供已有描述及批处理设置。
普通概括选择题先规划关键缺口，
只细看 0–3 个不同窗口，每次读取后根据核验结果决定下一次检查，不机械完成六点覆盖或反复 assess；
最终粗描述默认保留完整时间线，`caption_policy: ranked` 可选择旧的最多 12 窗口模式。
已核验事实保留。成本停止仍记录证据不足，计数、最后一次、否定及穷尽类问题沿用原覆盖流程。
M0 摘要字段在约束解码时限制长度，短单窗口描述直接复用；新建库成本与在线问答分别验收。
实现与诊断安排见 [少看精看与建库耗时](SCRIPT_Selective_Reading_2026-10-16.md)。

运行可靠性修复将模型重载、检查点和最终回答的预留一起放在原 600 秒内，
回答重试共用原 30 秒额度；短余额不再触发注定超时的模型启动。
无有效证据时，解码约束禁止生成引用字段，程序仅补回空数组，不接受占位 ID。
解析与回答限制重复数组和冗长文本；角色中已出现但漏声明的变量补为未绑定变量，
不创建事实、身份绑定或充分状态。原 21 道失败题的专用复验可用
`python script/verify_runtime_repairs.py status --out workdirs/script-runtime-repair-v4-20261015`
查看；验收检查合法完整回答、错误和预算违规，不能仅以进程退出码判断成功。

V5 在阅读原版 VideoTree 的描述筛选与 QA 实现后，修正了问题解析、证据输入和执行开销。
解析只读问题，并区分有原文依据的文字歧义与待观察信息；文字歧义可由语义核验引用事实消解。
概括题使用直接观察及选项区分点，不为每个时间窗口重复生成目标或默认构建对象关系图。
所有题型的最终回答均保留按时间排列的原始描述，明确标为未核验；超限时选择整段窗口，
记录实际模型输入的窗口 ID，不删除已核验事实。观察阶段为后续评估预留同一任务内的预算。
模型在 token 或批次边界协作停止以保留加载状态，硬超时仍可终止异常进程。
检查点按变化增量写入；报告分别统计整题失败、内部工具失败、软停止、硬超时与模型重载。
V5 整批复验目录为 `workdirs/script-videotree-repair-v5-20261015`，完整 100 题结果为
50% 正确率、平均问答约 173 秒，作为后续修复的历史对照保留。

首轮效果对比采用 SCRIPT B2/goal_lazy 与本地 Qwen 适配的 VideoTree：固定
100 道 EgoSchema 有公开答案的题，两个各 50 题的单卡任务，每张卡并行运行
两个独立的 25 题分组，使用相同
Qwen 权重、选项及已有 LaViLa 描述。答案只用于运行后独立评分。
这轮只使用 SCRIPT 默认在线预算，不展开预算档位或消融。
VideoTree 按已有流程选择描述回答，SCRIPT 仍可在线补看原视频，因此它是
同描述来源的系统效果对比。LaViLa 原始生成成本不计入任一方法的本次耗时；
VideoTree 已有帧/特征缓存另记，不能将运行耗时直接当作完整冷启动成本。

```bash
python script/run_pilot_comparison.py prepare --out workdirs/script-videotree-pilot-new --count 100 --shards 4 --seed 0 --feature-batch-size 64 --embedding-batch-size 16 --streams-per-gpu 2
# 两个单卡 PBS 作业分别使用 job-index 0、1；完成两个分组后退出并释放 GPU。
python workdirs/script-videotree-pilot-new/source/script/run_pilot_comparison.py run-job --out workdirs/script-videotree-pilot-new --job-index 0
python script/run_pilot_comparison.py score --out workdirs/script-videotree-pilot-new
```

`comparison.json` 与 `comparison.csv` 包含逐题预测、准确率、错误和配对结果。
未完成时 `complete=false`，`final_accuracy=null`，不把未跑完的样本排除后
冒充完整准确率。每个任务保存独立日志、模型/配置/源码哈希和阶段耗时。
优化后特征 batch=64，文本嵌入最多 batch=16，同时限制 padding 后的总 token 数。
无损 PNG 使用较低压缩级别以减少解码阶段的 CPU 开销。每张卡的两个分组
有独立模型、状态、输出与预算；配置记录 `streams_per_gpu=2`，处理秒数包含
共享 GPU 引起的等待。优化前的记录单独保留，最终准确率使用新配置的完整
100 题，不与旧配置混合。当前结果目录为
`workdirs/script-videotree-pilot-batch64-20261015/`；首张卡先在同一组真实缓存
图像上测量 batch 16、32、64，结果保存为 `feature_batch_benchmark.json`。

扩展评测先运行 VideoTree，使用包含关系固定的 100、300、500 题三组：保留原
100 题及其已有基线结果，以 seed=0 打乱其余 400 题，先补足 300，再补足 500。
500 题包含公开子集的全部题目，包括该子集内此前排除的调试题；调试题 ID 单独记录。
模型、提示词、建树流程和生成参数保持原基线设置，推理输入不含答案。
结果保存于 `workdirs/videotree-scaling-100-300-500-20261016/`，`cohorts.json`
可供后续 SCRIPT 使用相同分组。`comparison.json` 分组报告准确率、失败数与问答
流程耗时；抽帧和特征提取阶段另记耗时，复用结果保留历史耗时。未完成组的
`final_accuracy` 保持 `null`。

```bash
python script/run_videotree_scaling.py score --out workdirs/videotree-scaling-100-300-500-20261016
```

V7c 的 SCRIPT 扩展复用上述同一组 `cohorts.json`，保存于
`workdirs/script-v7c-scaling-100-300-500-20261016-r1/`。保留已完成的 100 题结果，
仅对新增 400 题构建 M0 并推理；四个独立进程分配到两张 H200，每个进程先补
300 题组，再补 500 题组。模型、提示词、600 秒预算、8 轮上限及批大小保持 V7c 设置。
`comparison_300.json`、`comparison_500.json` 分别报告完整组的结果，
`run_summary.json` 汇总进度和四组完整性审计；`jobs/submissions.json` 记录作业号。
VideoTree 复用已有 500 题结果，其中两道失败保留并计错。嵌套分组包含开发用的原 100 题。

```bash
python script/run_script_scaling.py score --out workdirs/script-v7c-scaling-100-300-500-20261016-r1
```

SCRIPT 的基础目录 M0 与问题无关：使用 EVA 特征做精确连续分段，按原始
PTS 建立场景和时间邻接，生成全部窗口描述与摘要，建立 BGE-M3 + BM25/RRF
索引。默认实体、关系和身份表为空。在线阶段在授权短区间内按需检测跟踪、
抽取与核验事实，逐题重置工作层；已知身份索引始终返回 `known_only=true`。
依赖撤销支持组内 AND、组间 OR；摘要、空图和 sampled 覆盖不算充分证据。

```bash
conda activate vts
pip install -r requirements.txt
python script_qa.py doctor --config configs/script.yaml --out workdirs/script/doctor
python script_qa.py build --manifest video_manifest.json --config configs/script.yaml --out workdirs/script/base
python script_qa.py query --base workdirs/script/base --questions questions.jsonl --config configs/script.yaml --out workdirs/script/runs
python script_qa.py evaluate --runs workdirs/script/runs --annotations evaluation.json --out workdirs/script/metrics
python -m unittest discover -s tests -v
```

`build` 和 `query` 的真实模型推理需要 GPU。可在已有 GPU 终端中运行上述命令。
`doctor` 只核验文件并导出模型、提示、源码与软件哈希。
四套本地模型分别位于 `checkpoints/BAAI/EVA-CLIP-8B`、
`checkpoints/Qwen/Qwen2.5-VL-7B-Instruct`、
`checkpoints/IDEA-Research/grounding-dino-tiny` 和 `checkpoints/BAAI/bge-m3`。
所有模型目录默认从此项目根目录解析，目录缺失会直接报错。
Grounding DINO 与 BGE-M3 适配依据各自的
[检测模型卡](https://huggingface.co/IDEA-Research/grounding-dino-tiny) 和
[嵌入模型卡](https://huggingface.co/BAAI/bge-m3)。结构化输出使用
[LM Format Enforcer](https://github.com/noamgat/lm-format-enforcer)。

视频清单是 JSON 数组，不接收问题或答案；`subtitle_uri` 可选，指向按时间
排序的 `[{"start":0,"end":1,"text":"..."}]` JSON。
已有视觉描述另用 `caption_uri` 指向同格式的时间戳文本，并配置
`initialization.caption_source: provided`，即可替代离线窗口描述生成。
它作为未核验的 `caption` 来源保存，不冒充字幕或实际读取的图像，不建立
已验证关系或充分覆盖。受限前缀会拒绝跨越截止时刻生成的描述，文件内容
哈希改变后原 M0 无法继续复用。默认 `caption_source: vlm` 保持原流程。
所有区间均为秒，采用 `[start,end)`；提前提问须使用合法截止时刻重新构建
M0，不能先看完整视频再过滤未来索引。

```json
[{"video_id":"video1","uri":"/absolute/path/video.mp4","cutoff":36}]
```

问题清单是 JSONL，每行一个对象；`options` 可省略以进行开放式问答。
禁止在该文件中放入 truth、标准答案或独立证据标注。输出选项编号为零起始字符串。

```json
{"question_id":"q1","video_id":"video1","question":"这段视频主要在做什么？","options":["做饭","缝纫"],"scope":[0,36]}
```

`configs/script.yaml` 覆盖 `script_core/config.py` 的完整默认参数。
`method` 支持 B0、B1、B1S、B2、B3：平面版本直接在全局状态解释工具返回，
B2/B3 使用限域局部任务及反馈；B3 另启用带版本检查的身份重分配事务。
三种构建策略为 `goal_lazy`、`scene_lazy`、`eager`。切换 eager 时必须同步
启用 `prebuild_relations` 和 `prebuild_identity_links`，细图单独存入 `M_eager`，
成本加入初始化。scene_lazy 每次最多处理两个整场队列窗口，受共同任务和预算限制。
默认在线上限 600 processing_seconds，最后回答保留 30 秒，每个局部任务最多
60 秒、每题最多 8 个任务；可用问题中的 `total_cap` 设置共同端到端总预算。
工具子进程超时会被终止，实际超支会在结果中明确标记。

基础目录下的 `videos.json` 对应视频 ID 与目录；问题目录名使用 ID 哈希，
实际 ID 保存在 `query.json`。SQLite 是事务检查点，JSONL 是可审阅导出。
每题保存对象、构建记录、覆盖、调用原始输入输出、缓存命中、路线与成本。
`result.json` 区分 `protocol_sufficient`、`resource_stop`、`exploration_stop`；
`infeasible` 和 `parse_failed` 是错误，不能伪装为正常停止。匹配的未完成查询
可以使用 `--resume`，未知结算的崩溃调用保留预占成本。

跨题复用须显式设置 `cache.scope: sequence_shared`，并向 query 提供
`--sequence-manifest`：`{"video_id":"video1","cutoff":36,"question_order":["q1","q2"],
"cache_directory":"workdirs/script/shared"}`。只共享有来源的事实与输入缓存，
绑定、需求和答案重新建立。`sequence_cost.json` 报告初始化一次加全部在线成本。

独立评估文件格式为 `{"questions":[{"question_id":"q1","selected_option":"1",
"cluster_id":"video1","sufficient_evidence_sets":[[{"interval":[10,12],"min_frames":3}]]}]}`。
证据标注也可使用精确 `frame_ids`；评估计算实际读取、最终保留、停止比例、
成本和按视频簇配对 bootstrap。开放题使用独立的 `acceptable_answers`；
错误充分停止率需要额外 `run_judgments`，未标注时输出 null。

本实现仍标记为开发运行：模型的事实与身份判断需要独立抽检，短动作协议需要
在隔离开发集校准。CPU 合成记录仅验收控制器，不代表真实感知准确率；
研究结论、正式数据集结果、参数冻结及论文外方法复现需要另外运行。

2026-10-15 本轮已完成 P0–P4 主线及 B3/跨题缓存扩展的代码调试。
60 项 CPU 测试覆盖连续分段、原始 PTS、依赖撤销、缓存与截止时刻隔离、
身份合并/拆分事务及证明失效、受损视频、量词覆盖、预算结算、异常清理、
完成查询只读恢复、回放及评估分组。运行时还校验模型 JSON 的字段、类型、
引用与数量限制，损坏的缓存图像会重新从原视频解码。

当时的验收源码（`source-v1/`）在 H200 上完成一段 36 秒真实视频的 M0 构建和两道不同问题。
M0 的详细实体、出现、关系与身份链接表为空；两题使用相同 M0 哈希。
活动题回答“人正在缝制或缝补一块黑色布料”，返回
`protocol_sufficient / sufficient`；颜色题回答“无法确定”，返回
`exploration_stop / insufficient`。两题均没有工具错误或预算超支。
颜色题的强制选项字段仍为 `0`，不能把它解释成证据充分。
活动题另完成 30 次已记录调用的完整回放，答案、证据引用和停止状态一致，
没有启动模型推理。回放的原始调用成本与本次读取/维护成本分别报告。

本轮记录在 `workdirs/script/validation-20261015/`，其中 `base-v1/` 为基础目录，
`runs-v1/` 为真实运行，`runs-replay-v1/` 为回放；`source-v1/` 与
`source-v1-hashes.json` 保存实际运行的源码快照。验收清单位于
`docs/history/SCRIPT_Implementation_Acceptance_2026-10-15.json`。
之前的暂停检查点与 `gpu-smoke-final/` 运行记录作为历史保留。
本轮申请的 PBS 作业 `666445.hopper-m-02` 已释放。

这些检查完成主线工程验收，尚未完成 P5 正式研究验收。真实跨场景感知质量、
50–100/约 200 题的开发诊断、独立多人标注、参数冻结、多预算重复实验及论文
外方法/系统级对照需要后续实施和运行，不能将这两道开发题作为准确率结果。

固定返回回放示例（输出须使用新目录）：

```bash
python script_qa.py query --base workdirs/script/validation-20261015/base-v1 \
  --questions workdirs/script/validation-20261015/activity-question.jsonl \
  --config configs/script.yaml --out workdirs/script/replay-new \
  --replay-source workdirs/script/validation-20261015/runs-v1 --replay-only
```

仅提供 `--replay-source` 时冻结观察返回并保留在线规划；增加 `--replay-only`
时也重放规划调用。缺失或输入不匹配的记录直接失败，不自动调用感知模型。
两种回放与真实交互运行使用不同评估分组。

消融参数在 `script_core/config.py` 的 `ablations` 默认配置中定义：反馈未决项、
后续线索、显式局部目标、全局回退、实体/时间导航和候选保留可分别关闭；
观察策略支持 `target_driven` 与 `uniform_dense`。检索入口支持 `scene`、
`window`，付费 eager 配置另支持 `event`、`fusion`。
`segmentation.solver` 默认 `exact_dp`，比较配置支持 `fixed_windows`、
`kmeans_runs`、`shot_histogram`；每种分段须单独构建 M0 并报告其实际成本。

主成本轴为完整处理秒数，包括首次模型文件核验、初始化、解码、模型调用、
缓存操作、状态维护、检查点和回答。CPU 秒数与 CUDA 上下文驻留秒数另列，
GPU 驻留时间包含模型加载后调用之间的间隔，不等于 GPU 忙碌时间；
无法测量的调用显式计数。成本账本自身的记账操作不递归计费。

</details>

# [CVPR 2025] VideoTree: Adaptive Tree-based Video Representation for LLM Reasoning on Long Videos

This is the official implementation for VideoTree (CVPR2025)

[![Project Website](https://img.shields.io/badge/Project-Website-blue)](https://videotree2024.github.io/)  [![arXiv](https://img.shields.io/badge/arXiv-2405.19209-b31b1b.svg)](https://arxiv.org/abs/2405.19209)

### Authors: [Ziyang Wang*](https://ziyangw2000.github.io/), [Shoubin Yu*](https://yui010206.github.io/), [Elias Stengel-Eskin*](https://esteng.github.io/), [Jaehong Yoon](https://jaehong31.github.io/), [Feng Cheng](https://klauscc.github.io/), [Gedas Bertasius](https://www.gedasbertasius.com/), [Mohit Bansal](https://www.cs.unc.edu/~mbansal/)

### University of North Carolina at Chapel Hill


We introduce VideoTree, a query-adaptive and hierarchical framework for long-video understanding with LLMs. Specifically, VideoTree dynamically extracts query-related information from the input video and builds a tree-based video representation for LLM reasoning.

<img src="./asset/teaser.png" alt="teaser image" width="800"/>

<img src="./asset/visualization.png" alt="vis image" width="800"/>

## **Installation**

### Local GPU smoke test (PBS)

This checkout includes `demo.py`, which runs the original sequence: 1 FPS
frames, EVA-CLIP-8B visual features, adaptive width expansion, relevance-based
depth expansion, and five-choice video QA. The local smoke test uses the
downloaded Qwen2.5-VL-7B-Instruct language model on the selected captions.
It is a functional test; its scores are not a reproduction of the paper's
GPT-4 results. It answers questions about a supplied video, rather than
searching across the dataset.

EgoSchema data is grouped under `data/EgoSchema/`:

```text
data/EgoSchema/
  egoschema/           # Downloaded videos, annotations, captions, and archives
  egoschema_frames/    # Generated 1 FPS frames
  egoschema_features/  # Generated EVA-CLIP-8B features
```

All 5,031 videos are downloaded. Frames and features currently cover the
initial smoke-test videos; preprocessing creates caches for additional videos
when they are selected.

The prepared environment is the Conda environment `vts` (Python 3.12,
PyTorch 2.7.1/CUDA 12.6, Transformers 4.57.6). Data and weights are shared
through the `data` and `checkpoints` links. To create the environment again,
activate `vts` and install:

```bash
conda activate vts
# Put temporary wheel downloads on scratch, rather than the login node's /tmp.
mkdir -p workdirs/.tmp
export TMPDIR="$PWD/workdirs/.tmp"
pip install torch==2.7.1+cu126 torchvision==0.22.1+cu126 \
  --extra-index-url https://download.pytorch.org/whl/cu126
pip install -r requirements.txt
```

Submit the three-example benchmark test and a separate Chinese question:

```bash
bash script/submit_query.sh
qstat -u "$USER"
```

Submission is asynchronous: PBS first returns a job ID such as
`654175.hopper-m-02`. The script also displays its status and the exact log
path. `Q` means queued, `R` running, and `F` finished. Follow the printed
`tail -F` command to read progress; the log can remain empty until a GPU is
allocated. Check `qstat -xf JOB_ID` for a queue reason or final `Exit_status`.
An exit status of 0 indicates success. Avoid resubmitting while the job is
queued or running. Existing summary files describe the preceding run until
the new job successfully updates them.

The batch job requests one GPU and at most two hours under project
`CFP05-CF-136`. On this cluster, submission hooks expanded the single-GPU
request to 12 CPU cores and 225 GB host memory; `qstat -f JOB_ID` shows the
effective allocation. It exits when the tests finish,
releasing the GPU. Logs are under `workdirs/videotree-smoke/jobs/`, and the
answers and selected frame captions are saved to
`workdirs/videotree-smoke/summary.json` and
`workdirs/videotree-smoke/custom-query/summary.json`.

For manual testing, obtain a GPU shell first:

```bash
qsub -I -l select=1:ngpus=1 -l walltime=02:00:00 -P CFP05-CF-136
cd /home/svu/e1583416/work/projects/VideoAgent/VideoTree
conda activate vts
python demo.py --num-examples 1
```

For VSCode debugging, keep VSCode connected to the login node. Run
**Terminal → Run Task → GPUReserve** to submit a GPU request in
advance; this task returns immediately while PBS allocates the GPU.
`gpu_apply.sh` also starts a background readiness check: once the GPU and
debug adapter are ready, it prints a highlighted message in the terminal
where you applied. Keep that terminal open to see the message. The result is
also recorded in the session's `notification.log`; non-interactive launches
use that log. `gpu_stop.sh` stops the pending readiness check as well. Run
**GPUStatus** to check its queue state and walltime. When ready,
select **VideoTreeSharedGPU**, set breakpoints, and press F5. The pre-launch
task reuses the recorded allocation and forwards the remote debug adapter
through SSH. Apply first: F5 only connects to an existing allocation and
reports an error if no allocation exists.
VSCode starts the configured Python program on the GPU node using Conda
`vts`. Program output appears in the Debug Console.

Stopping debugging leaves the GPU allocation and SSH tunnel in place.
The debug adapter restarts between runs, so you can press F5 repeatedly
without submitting a new PBS job. When finished, run
**Terminal → Run Task → GPUStop** to cancel the allocation and
close the tunnel. Stop waits for PBS to confirm the job has ended and for
the local tunnel/readiness processes to exit, terminating their process
groups if needed. It removes the allocation record only after cleanup
succeeds; a timeout or error preserves that record so you can retry stop.
The shared scripts and controller live under
`/home/svu/e1583416/work/projects/VideoAgent`, independently of VideoTree.
The scripts locate their dependencies from their own paths and use Conda
`vts` without requiring an activated environment. Run them from any directory:

```bash
/home/svu/e1583416/work/projects/VideoAgent/gpu_apply.sh     # submit without waiting
/home/svu/e1583416/work/projects/VideoAgent/gpu_enter.sh     # enter the existing GPU allocation
/home/svu/e1583416/work/projects/VideoAgent/gpu_stop.sh      # manually release
# Inspect the shared allocation; use Python 3 from any environment:
python /home/svu/e1583416/work/projects/VideoAgent/gpu_debug/run_gpu_debug.py status
```

`gpu_apply.sh` only submits or reuses the shared allocation and returns
immediately; it keeps your terminal on the login node. To enter that allocation,
run `gpu_enter.sh` in an interactive terminal. If the job is still queued,
it waits for readiness (up to 30 minutes by default; override with `--timeout`
in seconds). It never submits a new job, and asks you to run `gpu_apply.sh`
if no allocation exists.

The GPU shell activates Conda `vts` and opens your current project directory.
You can run `nvidia-smi` and Python directly there. VSCode remains connected
to the login node, and F5 uses the same allocation. `exit` returns this terminal
to the login node and keeps the GPU and debug tunnel reserved; use
`gpu_stop.sh` when finished. Other login-node terminals remain on the login node.
Plain SSH may be terminated by compute-node job enforcement; `gpu_enter.sh`
attaches its shell to the existing PBS job through `pbs_attach`.

New allocations default to a one-hour walltime, measured from when PBS
starts the job, including idle time between debug runs. For a different
limit, submit with `/home/svu/e1583416/work/projects/VideoAgent/gpu_apply.sh --walltime 04:00:00`
(subject to the cluster's limits). An existing allocation keeps its original
walltime. Reserving again reuses a queued or running job. The shared job is
named `videoagent-debug`, and allocation records, logs, and temporary files
live under `VideoAgent/gpu_debug/workdirs/pbs/`. Other projects can use the
same controller's `start` action as their pre-launch task and connect to
`debugServer: 5678`, while keeping their own program, interpreter, and working
directory in their launch configuration. Configure the shared adapter's
interpreter with `GPU_DEBUG_PYTHON` if needed; it must have `debugpy` installed.

There is one launch configuration. Edit its `program` and `args` in
`.vscode/launch.json` to change what is debugged; the default uses video
`0a01d7d0-11d6-4af6-abd9-2025656d3c63` and cached preprocessing results.
Omit `--skip-preprocess` for videos without cached frames/features. Child
Python processes are also debugged, including the tree-building and QA scripts.

PBS still queues the job when no GPU is free. The preparation task waits up
to 30 minutes. Timeout, interruption, SSH connection failure, or closing
VSCode leaves the request in place; retry F5 later or run `stop` to release it.
PBS releases the GPU when the requested walltime expires. Automatic connection
requires SSH authentication from the login node to the allocated compute node
without a password prompt. Each F5 launches a fresh Python program, so model
loading still happens on each run; the saved time comes from reusing the PBS
allocation and connection. Use one debug session at a time with this allocation.

To supply a question, provide the video ID and five choices. Frames and
features are cached between questions. The tree is rebuilt for each query:

```bash
bash script/submit_query.sh \
  --video-id 0074f737-11cb-497d-8d07-77c3a8127391 \
  --question '这个视频里的人主要在做什么？' \
  --options '清洗碗和锅等厨房用具' '用画笔画画' '和朋友玩桌游' '骑自行车' '修理汽车' \
  --output workdirs/my-query
```

With `vts` activated, `python demo.py --list-examples` displays sample video IDs, questions, and
choices without allocating a GPU. `--video-id` also supports videos outside
the 500-question subset, using the full-set annotations and captions.
For a custom question or a sample without a public answer, ground-truth
accuracy is omitted. All frame indices are zero-based: frame 0 corresponds
to the first caption and the frame sampled at 0 seconds. In this checkout,
relevance scores are aligned to cluster IDs before depth expansion.

Regression checks can be run on CPU:

```bash
conda activate vts
python -m unittest discover -s tests -v
```

Validation on 2026-10-08 used Conda `vts` and one H200: all four regression
checks passed, the first three subset questions were answered correctly,
and the separate Chinese query returned "A. 清洗碗和锅等厨房用具" with 59
selected frames. The three-example run took about 54 seconds with cached
features; the custom query took about 32 seconds. These small-sample
results establish that the pipeline runs, rather than dataset-wide accuracy.

**Install environment.**

Python 3.8 or above is required.

```bash
git clone https://github.com/Ziyang412/VideoTree.git
cd VideoTree

python3 -m venv videetree_env
source activate videetree_env/bin/activate
pip install openai
pip3 install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
pip install pandas
pip install transformers==4.28.1
pip install accelerate
```

**Download dataset annotations and extracted captions.**

Download data.zip from the File [LLoVi](https://drive.google.com/file/d/13M10CB5ePPVlycn754_ff3CwnpPtDfJA/view?usp=drive_link) provided. 

```bash
unzip data.zip
```

Place the extracted **EgoSchema** captions and dataset annotations in
`./data/EgoSchema/egoschema/` alongside the downloaded videos.

Specifically, [LaViLa](https://arxiv.org/pdf/2212.04501.pdf) base model is leveraged to extract EgoSchema captions at 1 FPS.

**Download EgoSchema Videos.**
Please follow [EgoSchema](https://github.com/egoschema/EgoSchema) to download the orginal EgoSchema videos. After downloading, please extract the videos into 1 FPS video frames (save in image format for faster loading speed). Please save in the format of `./data/EgoSchema/egoschema_frames/{video_id}/{frame_id}.jpg`. Then, to further speed up the tree building process, we extract the visual features for each frame using [EVA-CLIP-8B](https://huggingface.co/BAAI/EVA-CLIP-8B#eva-clip-8b) and save the features in `./data/EgoSchema/egoschema_features/{video_id}.pt`.

```bash
python data_extraction/extract_images.py
python data_extraction/extract_features.py
```

## Update Kmeans-pytorch
Since the orginal Kmeans-pytorch package doesn't set a iteration limit and will cause perpetual loop issue, we update the init file of the original kmeans-pytorch package.

```bash
git clone https://github.com/subhadarship/kmeans_pytorch
cd kmeans_pytorch
```

Please replace the init file in "kmeans_pytorch" folder with the file we provide in "./kmeans_pytorch" folder (this repo). And run the following command. 

```bash
pip install --editable .
```

## Future plans
Due to the limit of time, we are still updating the codebase. We will also incorporate the scipts/captions for NeXT-QA and IntentQA in the future. 


## Experiments


### Adaptive Breath Exapnsion
Please update the feature, asgs (in util.py) and output path before running the code.
```bash

sh scripts/breath_expansion.sh

```

### Relevance-based Depth Expansion

Please update the feature, the output of last step (the relevance output path and first level cluster information) and output path before running the code.
```bash

python depth_expansion.py

```

### LLM Reasoning

Please update the tree node index file (output of last step), data files and output path before running the code.

```bash
sh scripts/egoschema_qa.sh
```


## Debug

```bash
--save_info: save more information, e.g. token usage, detailed prompts, etc.
--num_examples_to_run: how many examples to run. -1 (default) to run all.
--start_from_scratch: ignore existing output files. Start from scratch.
```


## Acknowledgments
We thank the developers of [LLoVi](https://github.com/CeeZh/LLoVi), [LifelongMemory](https://github.com/Agentic-Learning-AI-Lab/lifelong-memory), [EVA-CLIP](https://huggingface.co/BAAI/EVA-CLIP-18B#eva-clip-8b), [Kmeans-pytorch](https://github.com/subhadarship/kmeans_pytorch) and [SKlearn Clustering](https://scikit-learn.org/stable/auto_examples/cluster/plot_ward_structured_vs_unstructured.html) for their public code release. We also thank the authors of [VideoAgent](https://arxiv.org/pdf/2403.10517) for the helpful discussion. 

# Reference
Please cite our paper if you use our models in your works:

```bibtex
@InProceedings{Wang_2025_CVPR,
    author    = {Wang, Ziyang and Yu, Shoubin and Stengel-Eskin, Elias and Yoon, Jaehong and Cheng, Feng and Bertasius, Gedas and Bansal, Mohit},
    title     = {VideoTree: Adaptive Tree-based Video Representation for LLM Reasoning on Long Videos},
    booktitle = {Proceedings of the Computer Vision and Pattern Recognition Conference (CVPR)},
    month     = {June},
    year      = {2025},
    pages     = {3272-3283}
}
