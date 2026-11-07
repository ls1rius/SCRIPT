# SCRIPT

2026-11-06：**默认研究/评测方案为 Q v4 自适应，首选 Q4+（主结果），Q4 保留为同证据对照。** 后续改进和汇报优先 Q4+。未训练版500题已完成，Q4+/Q4均316/500（63.2%）；Q4+最终回答平均输入3086 token、0.535秒，Q4为4194 token、0.696秒，耗时为共卡观测值且仅指最终读者。完整结果见 [500题报告](workdirs/Q4-adaptive-500-20261105-r1/report.json)，默认登记见 [默认方案](docs/experiments/Default_Q_V4_Adaptive_2026-11-05.json)。固定6轮LoRA版仍在原300题开发验证，暂未替换基线。v3完整版及L保留历史比较，关闭早停组仅作历史消融。旧方案退役范围见 [清理审计](docs/history/Retired_Schemes_Cleanup_2026-11-05.json)。研究首选更新不改变当前冻结运行；普通query入口目前仍为L。

2026-11-02，**普通查询默认使用 L（按需版），完全版默认仍为 E**。L 在 H→I 开放状态、按需抽取、来源缓存和相同陈述／相邻区间合并的基础上，增加独立证据规划筛选，再生成最终答案。
通用 CLI、Python 默认配置、`configs/script.yaml` / `configs/script_local.yaml` 已统一为 L；E/F/I 和研发版本 M 仍可显式选择。

```bash
# 默认 L；也可显式添加 --memory L。
python script_qa.py query --base <base> --questions <questions.jsonl> --out workdirs/query-L
# 显式选择不加证据规划的 I，使用独立输出目录。
python script_qa.py query --base <base> --questions <questions.jsonl> --out workdirs/query-I --memory I
# 切换 E，使用独立输出目录。
python script_qa.py query --base <base> --questions <questions.jsonl> --out workdirs/query-E --memory E
# 显式选择旧按需版 F，使用独立输出目录。
python script_qa.py query --base <base> --questions <questions.jsonl> --out workdirs/query-F --memory F
# M：L 的后续版，按明确证据缺口逐场景研究，不强制时间覆盖。
python script_qa.py query --base <base> --questions <questions.jsonl> --out workdirs/query-M --memory M
python script_qa.py query --base <base> --questions <questions.jsonl> --out workdirs/query-N --memory N
```

配置方式：`query.temporal_qa_evidence: planned_lazy_script` 为 L（默认），`merged_lazy_script` 为 I，`lazy_script` 为 F，`joint_entities` 为 E；也可使用
`--config configs/script_L.yaml`、`configs/script_I.yaml`、`configs/script_F.yaml` 或 `configs/script_E.yaml`。D 通过
`--config configs/script_D.yaml` 显式启用，历史完整输入使用 `configs/script_temporal_full.yaml`。
`--memory E/F/I/L/M/N` 要求 summary 来源的 `adaptive_partial_temporal` 管线，不能与 `--scheme4` 同用；
单独 `--scheme4` 仍切换到原方案 4。

M 也可通过 `--config configs/script_M.yaml` 显式选择。所有场景总描述用于选择一个首场景；细查后明确当前无法补足的缺口，才打开下一个相关场景，信息足够立即停止。当前冻结M继续运行，普通查询默认L。真实功能门及正式进度见 [M 文档](docs/SCRIPT_M_L_Gap_Driven_Scenes_2026-11-02.md)。本次来源抽取修复独立放在后续N，通过 `--memory N` 或 `configs/script_N.yaml` 选择；见 [N 文档](docs/SCRIPT_N_Source_Role_Binding_2026-11-02.md)。N v2通过109项CPU检查，固定来源单题已抽出位置/左手但仍答错（原L正确）；字段必要性尚未修复，未启动300题评测。 后续[N场景初筛修复尝试](docs/SCRIPT_N_Option_Scene_Selection_2026-11-03.md)仍未通过语义验证，不作为已修复版本或准确率提升。

默认 L 的规划联合读取问题、全部选项和统一候选池，对每条证据决定保留／跳过／不确定／分组，经来源审计后再回答一次。新查询沿用 `Frames [4,6): ...`，省略默认来源标签，保留推断标签和全部原始区间。L 不启用 M 的新检索策略；本次默认切换不改任何已封存的输入、结果或运行进程。

E 复用联合七类抽取、最多一次角色复核、8 秒局部实体关联及 2 秒状态支持间隙；
F 复用问题字段规划、增量字段缓存及独立来源 batch 2（可配置 `field_extraction_batch_size: 1/2/4`）。
来源抽取不接收问题或选项；两种模式只替换最终 Description，保持问题、选项、旧时序记忆及已接受视觉观察。
抽取预算不足时保留原句并标记失败字段；上下文超限显式报错，不裁剪或改用另一模式重答。

显式 F 的缓存位于 `<out>/memory_cache/F/script.sqlite3`，可用 `query.memory_cache_directory` 指定共享缓存根目录；
按视频描述版本、来源原文和模型／代码版本隔离，跨问题改变选中片段也可复用同源字段，
当前问题只呈现已选来源与请求字段。缓存命中节省的是后续查询的实际抽取，已有构建成本仍属于原查询。
每题 `partial_temporal/` 保存 `semantic_sources.json`、`entity_evidence_audit.json`、`qa_context.json`，
以及 E 的 `joint_entity_memory.json` 或 F 的 `field_plan.json`、`lazy_script_memory.json`。
`result.json` 中的 `memory_variant` 和 `qa_evidence.mode` 标明实际执行版本。

显式 I：`--memory I` / `configs/script_I.yaml` / `merged_lazy_script`，继承 H 的开放状态、八类属性、未知执行者及按需抽取，
将相同可见陈述合并一行并保留每次独立采样时间；不推断连续动作、实体身份或事件次数。
I 也要求上述 summary 管线并使用新输出目录，新版缓存为 `memory_cache/I/h_script.sqlite3`，不迁移旧 I/F/H 缓存；另保存
`generalized_lazy_memory.json` 和 `merged_lazy_memory.json`。旧 F→I 的准备与 7 题演示保留为历史 I-v1，不能作为新版结果。默认切换仅影响未来普通查询，不改写冻结实验。实现与验证范围见 [I 文档](docs/SCRIPT_I_Merged_On_Demand_Memory_2026-10-31.md)。

新在线运行沿用共享上游取证和组织流程，其预算含上游开销，不等同于已封存的离线 300 题最终输入干预评测。
切换 E/F/I/L/M/N 后应使用新输出目录；原配置下的 `--resume` 才能复用原答案。L 使用独立 `memory_cache/L/I/h_script.sqlite3`，并在 `partial_temporal/L/` 保存候选池、原始规划输入输出、计划、审计与实际回答输入。N对应独立的 `memory_cache/N/I/h_script.sqlite3` 与 `partial_temporal/N/`。G/H 暂保留独立实验入口。

## 当前方案与入口

| 路线 | 默认版本 | 通用结构后续版 |
| --- | --- | --- |
| 完全版 | E：七类联合抽取、局部实体关联与实体时间线 | G：开放状态描述、未知执行者规则 |
| 按需版 | L：I 的 H 按需记忆与合并＋独立证据规划筛选；普通查询默认 | 路线 F → H → I → L → M；K 的规划方法迁移至 L |

G/H 保留独立实验入口；完全版默认 E，按需版默认 L。实现、CPU 检查、真实模型功能验证和全量评测分别记录。
“完全版”指上游已选证据内的全类别抽取；上游取证仍依赖问题。

| 用途 | 入口 |
| --- | --- |
| 构建基础缓存、普通查询、独立评分、模型文件检查 | [script_qa.py](script_qa.py) |
| E 离线实验 | [run_joint_entity_experiment.py](script/run_joint_entity_experiment.py) |
| F 离线实验 | [run_lazy_script_experiment.py](script/run_lazy_script_experiment.py) |
| G 离线实验 | [run_generalized_entity_experiment.py](script/run_generalized_entity_experiment.py) |
| H 离线实验 | [run_generalized_lazy_experiment.py](script/run_generalized_lazy_experiment.py) |
| I：H 的信息合并后续版 | [run_merged_lazy_experiment.py](script/run_merged_lazy_experiment.py) |
| K：F 的独立证据规划器 | [run_evidence_planner_experiment.py](script/run_evidence_planner_experiment.py)，[实现与排队状态](docs/SCRIPT_K_F_Evidence_Planner_2026-11-02.md) |
| L：I 的独立证据规划器 | [run_i_evidence_planner_experiment.py](script/run_i_evidence_planner_experiment.py)，[I→L迁移与运行状态](docs/SCRIPT_L_I_Evidence_Planner_2026-11-02.md)；默认仍为I |
| G＋GRPO 历史实验记录（已清理运行产物） | [实验协议与最终结果](docs/SCRIPT_G_RL_Experiment_2026-10-31.md) |

完整脚本分类见 [script/README.md](script/README.md)。历史实验应使用各自 workdirs 中的冻结源码和配置。

## 环境与数据

依赖清单为 [requirements.txt](requirements.txt)，模型路径见 [configs/script.yaml](configs/script.yaml)。
统一使用 `/mnt/ssd2/hli/work/miniconda3/envs/vts`，所有现存实验和普通查询均使用此环境。
`workdirs` 中的旧环境及兼容链接均已移除，现有队列直接使用 vts 解释器。
版本锁定和验证结果见 [统一环境说明](docs/VTS_Environment.md)。G＋GRPO 已退役，其训练环境和权重已删除。

```bash
conda activate /mnt/ssd2/hli/work/miniconda3/envs/vts
# 检查现有模型文件并导出指纹，不加载权重。
python script_qa.py doctor
# 查看构建与查询参数。
python script_qa.py build --help
python script_qa.py query --help
```

`data`、`checkpoints`、`workdirs` 是指向外部数据、模型和实验产物的链接；统计或清理时不要跟随链接删除目标。
新查询需要兼容的原始秒级描述和已有基础特征，不能把重叠窗口摘要冒充秒级原文。
旧 VideoTree 安装、PBS 调度和基线说明保存在 [历史 README](docs/history/SCRIPT_Readme_Before_Cleanup_2026-10-31.md)。

## 文档与实验记录

- [已确认的记忆结构](docs/Memory_Structure.md)
- [文档索引](docs/README.md)
- [受控 A/B/C 实验协议](docs/Controlled_Memory_Experiment_2026-10-28.md)
- [B/C 历史实验记录](docs/history/BC_Experiment_Record_2026-11-01.md)（独立产物已清理，A 与 D–J 保留）
- [语义记忆组织与来源合同](docs/Semantic_Memory_Data_Structure_2026-10-28.md)
- [G 结构设计](docs/SCRIPT_G_Generalized_Memory_Design_2026-10-30.md)
- [G 执行优化及验证结果](docs/SCRIPT_G_Execution_Optimization_2026-10-30.md)
- [G＋RL 实验协议](docs/SCRIPT_G_RL_Experiment_2026-10-31.md)
- [H 实现与验证状态](docs/SCRIPT_H_Generalized_On_Demand_Memory_2026-10-31.md)
- [I 信息合并实现与验证状态](docs/SCRIPT_I_Merged_On_Demand_Memory_2026-10-31.md)
- [本次清理记录](docs/history/Cleanup_2026-10-31.md)
- [早期方案实验记录](docs/history/Early_Schemes_Experiment_Record_2026-11-01.md)（旧运行产物和专用入口已清理）

实际进度及性能以对应运行目录的 `report.json`、`progress.json` 和队列状态为准。未完成方案使用共同完成题目比较，
待执行题不计错；共享 GPU 时间不作为受控速度比较。已封存结果与当前在线查询分开报告。

原项目：VideoTree（CVPR 2025），[论文](https://arxiv.org/abs/2405.19209)。完整历史引用和安装说明见上述历史 README。

- 2026-11-03：[M/N 候选筛选修订与优化后 M 独立重跑](docs/SCRIPT_MN_Candidate_Repair_2026-11-03.md)；旧 M-r13 第 95 题自行失败，r14 已通过真实功能门并进入全300题重跑，N 同步选择器。

- 2026-11-03：[N 全300题独立启动记录](docs/history/N_300_Launch_2026-11-03.json)；GPU0真实功能门通过、进入正式第1题，M继续GPU2。
