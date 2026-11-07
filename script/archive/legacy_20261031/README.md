# 已退出主入口的历史工具

2026-10-31 归档。当前普通查询使用仓库根 `script_qa.py`（默认 F、可选 E）。
这些文件保留历史实现与排错依据；重现实验时优先使用对应 workdirs 内的冻结副本。

| 工具 | 归档原因 |
| --- | --- |
| `run_compact_gpu3_shared.py`、`run_shared_evidence_qa.py` | 已完成 A/B/C/D 的一次性共享调度 |
| `optimize_lazy_script_execution.py` | F 首次接入 batch 的一次性迁移；当前 F 已直接支持 batch |
| `run_normalized_memory_comparison.py` | 已退出有效主对照的旧规范化实验入口 |
| `run_partial_preflight.py`、`run_scheme4_preflight.py` | 旧 partial／scheme4 修复阶段的独立功能门 |
| `preview_memory_quotes.py` | 旧稀疏引用与去动作预览工具 |
| `submit_query.sh`、`gpu_smoke.pbs`、`run_gpu_query.py` | 旧 VideoTree demo 的 PBS 提交入口 |
| `script_smoke.pbs`、`run_script_args.py`、`smoke_script.py` | 早期 SCRIPT smoke 工具 |

文件移动清单和校验值见 `docs/history/Cleanup_2026-10-31.json`。

2026-11-01：旧五方案缓存接续入口 `continue_with_cache.py` 随早期实验清理删除；结果、协议与成本摘要见 [早期方案实验记录](../../../docs/history/Early_Schemes_Experiment_Record_2026-11-01.md)。
