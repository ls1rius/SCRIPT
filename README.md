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

## 当前方案与入口

| 路线 | 默认版本 | 通用结构后续版 |
| --- | --- | --- |
| 完全版 | E：七类联合抽取、局部实体关联与实体时间线 | G：开放状态描述、未知执行者规则 |
| 按需版 | F：字段规划、增量抽取及 SQLite 缓存；普通查询默认 | H：把 G 的通用结构适配到 F 的按需存取 |

G/H 保留独立实验入口，不自动替换 E/F。实现、CPU 检查、真实模型功能验证和全量评测分别记录。
“完全版”指上游已选证据内的全类别抽取；上游取证仍依赖问题。

| 用途 | 入口 |
| --- | --- |
| 构建基础缓存、普通查询、独立评分、模型文件检查 | [script_qa.py](script_qa.py) |
| E 离线实验 | [run_joint_entity_experiment.py](script/run_joint_entity_experiment.py) |
| F 离线实验 | [run_lazy_script_experiment.py](script/run_lazy_script_experiment.py) |
| G 离线实验 | [run_generalized_entity_experiment.py](script/run_generalized_entity_experiment.py) |
| H 离线实验 | [run_generalized_lazy_experiment.py](script/run_generalized_lazy_experiment.py) |
| G＋RL | [run_g_rl_experiment.py](script/run_g_rl_experiment.py) |

完整脚本分类见 [script/README.md](script/README.md)。历史实验应使用各自 workdirs 中的冻结源码和配置。

## 环境与数据

依赖清单为 [requirements.txt](requirements.txt)，模型路径见 [configs/script.yaml](configs/script.yaml)。
此工作区现有推理环境为 `workdirs/query-runtime-env/`，RL 环境为 `workdirs/g-rl-env/`。

```bash
# 检查现有模型文件并导出指纹，不加载权重。
workdirs/query-runtime-env/bin/python script_qa.py doctor
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
- [语义记忆组织与来源合同](docs/Semantic_Memory_Data_Structure_2026-10-28.md)
- [G 结构设计](docs/SCRIPT_G_Generalized_Memory_Design_2026-10-30.md)
- [G 执行优化及验证结果](docs/SCRIPT_G_Execution_Optimization_2026-10-30.md)
- [G＋RL 实验协议](docs/SCRIPT_G_RL_Experiment_2026-10-31.md)
- [H 实现与验证状态](docs/SCRIPT_H_Generalized_On_Demand_Memory_2026-10-31.md)
- [本次清理记录](docs/history/Cleanup_2026-10-31.md)

实际进度及性能以对应运行目录的 `report.json`、`progress.json` 和队列状态为准。未完成方案使用共同完成题目比较，
待执行题不计错；共享 GPU 时间不作为受控速度比较。已封存结果与当前在线查询分开报告。

原项目：VideoTree（CVPR 2025），[论文](https://arxiv.org/abs/2405.19209)。完整历史引用和安装说明见上述历史 README。
