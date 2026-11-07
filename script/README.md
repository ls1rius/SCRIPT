# 脚本索引

研究/评测默认首选 **Q4+**，Q4保留为同证据对照；后续新Q4及固定6轮训练实验元数据使用 `primary_arm: Qplus`、`comparison_arms: [Q]`，优先报告Q4+。详见[默认登记](../docs/experiments/Default_Q_V4_Adaptive_2026-11-05.json)。

普通用户入口为仓库根的 `script_qa.py`；默认 L（I记忆＋独立证据规划筛选），可显式 `query --memory L`，通过 `--memory I` 使用不加筛选的I，`--memory E` 使用完全版E，`--memory F` 选择原按需版F。
`query --memory M` 选择 L 的后续 M：基于全部场景总描述，按当前场景无法补足的明确缺口逐一扩展，不强制时间覆盖。M 的后续改进独立进行，详见 [M 文档](../docs/SCRIPT_M_L_Gap_Driven_Scenes_2026-11-02.md)。

统一 Python 为 `/mnt/ssd2/hli/work/miniconda3/envs/vts/bin/python`，激活 `vts` 后使用 `python` 即可运行本页入口。`workdirs` 内旧环境和兼容链接均已删除；不再创建独立项目环境。详见[环境说明](../docs/VTS_Environment.md)。

| 分类 | 入口或模块 |
| --- | --- |
| Q6/Q6+ 在线RL | `train_q6_rl.py prepare/supervise/worker`；从Q5第6轮继续，独立物理池/6轮/奖励隔离；见[Q6说明](../docs/Q6_Outcome_RL_2026-11-06.md) |
| 普通查询和本地运行 | `run_local_smoke.sh`、`run_local_four_arm.py` |
| E/F 独立实验 | `run_joint_entity_experiment.py`、`run_lazy_script_experiment.py` |
| G/H 独立实验 | `run_generalized_entity_experiment.py`、`run_generalized_lazy_experiment.py` |
| I 信息合并实验 | `run_merged_lazy_experiment.py`（从封存 H 准备可逆合并输入并生成独立 I 回答；当前 I-v2，旧 F→I 诊断已删除） |
| K 独立证据规划 | `run_evidence_planner_experiment.py`（冻结 F 候选、独立等空闲 GPU 后规划保留／合并／跳过，生成 F 句子版、全保留控制及 K 三组新回答；见 [K 文档](../docs/SCRIPT_K_F_Evidence_Planner_2026-11-02.md)） |
| G＋GRPO 历史记录 | [最终结果与协议](../docs/SCRIPT_G_RL_Experiment_2026-10-31.md)；训练、评估和调度入口已删除 |
| G 执行验证 | `benchmark_generalized_execution.py` |
| 公共来源及执行模块 | `controlled_memory_io.py`、`lazy_script_execution.py`、`generalized_lazy_execution.py` |
| A/D 对照与公共 QA 支持 | `run_controlled_memory_experiment.py`、`run_compact_evidence_experiment.py`、`run_state_segment_experiment.py` 及对应依赖；B/C 独立产物已清理 |

其余仍被当前入口、冻结准备工具或回归测试引用的辅助模块保留；不按文件日期删除这些依赖。

B/C 仅保留[实验文档记录](../docs/history/BC_Experiment_Record_2026-11-01.md)。`final_inputs/B_lossless_layout` 的公共来源观测仍被 E/F 准备工具读取；保留该来源副本和公共 QA 模块，不保留 B/C 独立答案、C 抽取记忆及专用投影。历史混合实验不得恢复为 B/C 活动队列。

早期五方案、时序增强及 VideoTree 的专用运行入口与运行目录已清理，只保留[实验文档记录](../docs/history/Early_Schemes_Experiment_Record_2026-11-01.md)。`run_followup_validation.py` 的 `direct_input` 仍被当前 `partial_temporal.py` 使用；其导入链上的公共辅助模块继续保留。A–J 的代码及有效冻结快照不在本次清理范围。

`script_core/g_rl.py` 仅保留 J／demo 对照评分使用的公共答案解析器；历史文件名用于兼容准备工具，已移除所有训练、奖励、LoRA 和优化实现。现有 J 冻结快照保持原样。

已结束的迁移、一次性调度、旧归一化和旧 GPU smoke 入口移至
[archive/legacy_20261031](archive/legacy_20261031/README.md)。
原有 `archive/` 中的历史 PBS 文件继续保留。

已有实验继续使用自身运行目录中的冻结源码；归档工具不替代当前入口，不自动重启旧实验。

- J：全部合并与证据约束回答，包含原子主张、原文引用校验及严格解析；独立实验入口 `script/run_grounded_reader_experiment.py`，队列入口 `script/queue_grounded_reader_after_i.py`。设计、独立功能门及 I→J 全量排队状态见 [J 方案说明](../docs/SCRIPT_J_Grounded_Reader_2026-11-01.md)。

L：基于I迁移K的独立证据规划，现为普通query按需默认；显式 `--memory L` 或 `configs/script_L.yaml`，I保留为 `--memory I`。离线入口 `run_i_evidence_planner_experiment.py`；[方案及运行状态](../docs/SCRIPT_L_I_Evidence_Planner_2026-11-02.md)。

### P：O 的原始问题直接筛选版本

`run_p_sentence_experiment.py` 提供独立 `prepare/queue/worker/report` 入口。P 继承 O 的抽取与结构化证据，移除生成式任务说明，不调用 O+ 重组。准备需要显式 `--parent` O 运行和一个或多个 `--case-id`；默认冻结复用这些题的 O 抽取，`--fresh-extraction` 可新抽取。筛选及答案始终新生成，结果独立保存。详见 [P 说明](../docs/SCRIPT_P_O_Direct_Question_2026-11-03.md)。
