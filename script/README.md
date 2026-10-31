# 脚本索引

普通用户入口为仓库根的 `script_qa.py`；默认 F，可通过 `query --memory E` 使用完全版 E。

| 分类 | 入口或模块 |
| --- | --- |
| 普通查询和本地运行 | `run_local_smoke.sh`、`run_local_four_arm.py` |
| E/F 独立实验 | `run_joint_entity_experiment.py`、`run_lazy_script_experiment.py` |
| G/H 独立实验 | `run_generalized_entity_experiment.py`、`run_generalized_lazy_experiment.py` |
| G＋RL | `run_g_rl_experiment.py`、`build_g_rl_memories.py`、`parallel_g_rl_memories.py` |
| G 执行验证 | `benchmark_generalized_execution.py` |
| 公共来源及执行模块 | `controlled_memory_io.py`、`lazy_script_execution.py`、`generalized_lazy_execution.py` |
| A/B/C/D 对照及历史重放 | `run_controlled_memory_experiment.py`、`run_compact_evidence_experiment.py`、`run_state_segment_experiment.py` 及对应依赖 |

其余仍被当前入口、冻结准备工具或回归测试引用的辅助模块保留；不按文件日期删除这些依赖。

已结束的迁移、一次性调度、旧归一化和旧 GPU smoke 入口移至
[archive/legacy_20261031](archive/legacy_20261031/README.md)。
原有 `archive/` 中的历史 PBS 文件继续保留。

已有实验继续使用自身运行目录中的冻结源码；归档工具不替代当前入口，不自动重启旧实验。
