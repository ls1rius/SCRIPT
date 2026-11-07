# I：H 的信息合并后续版

> 2026-11-02 最新决定：用户将 I 设为按需版默认，完全版默认仍为 E。普通查询、Python默认配置、script.yaml和script_local.yaml已切换merged_lazy_script；显式E/F/I配置仍可选择。I的正式300题已于13:52完成，190/300（63.33%），有1题输出错误。此前默认F及等待运行描述为历史。使用新查询输出目录，缓存为memory_cache/I/h_script.sqlite3，不迁移或改写F/H/旧I缓存、冻结实验或K。

2026-11-01 清理更新：停止的阶段实验和七题诊断记录已删除。当前 H-r5 → I-r4-queue（输出 I-r3）→ J-r7-queue（输出 J-r5）继续监听；J 已改用独立封存的功能门凭据，不再依赖诊断目录。以下较早的调度描述为历史安排。

> 用户最新决定（2026-11-01）：修复代码但暂不运行，释放显卡。H-r4 功能门失败后 I-r3-queue 已退出，I-r2 正式输出尚未创建。当前不启动四卡监听或恢复 I；H 的新谓词绑定修复仅通过 CPU 检查，待用户明确恢复后另建快照。I 合并方法保持。

> 2026-11-01 修复并重新排队：H-r3 的真实功能门失败（角色增量输出丢失明确 actor、工具被挂到 `with` 伪动作），未进行正式评测。新 H-r4 使用 `generalized-lazy-memory-H-v2-action-binding`，动作角色请求同时包含动作骨架，强化角色绑定提示并排除独立功能词作动词；来源校验保持。51 项 CPU 检查通过，真实功能门等空闲卡后执行。当前链：H `generalized-lazy-H-300-20261101-r4` → I 控制器 `merged-lazy-I-H-adjacent-300-20261101-r3-queue`（输出 I-r2）→ J 控制器 `grounded-reader-J-300-20261101-r5-queue`（输出 J-r4）。各阶段均监听 GPU 0/1/2/3，每 10 秒采样，同卡连续 3 次空闲显存 ≥32 GiB、利用率 ≤10%，获取锁并复查后择一卡运行。H/I/J 无 GRPO；I 合并方法、J-v3 方法保持，旧失败记录与成本保留。下文旧调度路径均为历史。

调度更新（2026-11-01 01:41，新加坡时间）：H/I/J 不使用 GRPO，也不等待 G＋GRPO。当前 I 控制器为 `workdirs/merged-lazy-I-H-adjacent-300-20261101-r2-queue/`，H 活跃快照为 `workdirs/generalized-lazy-H-300-20261101-r3/`（GPU 2），随后独立运行 I→J。I 输出仍为 `workdirs/merged-lazy-I-H-adjacent-300-20261101-r1/`，区间合并代码、原始 QA 模型及解析不变。旧 I-r1-queue 的等待控制器已在启动 H 前停止，旧快照保留；本文更早的等待 G＋GRPO 的描述为历史安排。

2026-10-31，新加坡时间。用户明确将 I 改为 H 的接续，当前路线为 **F → H → I**；完全版路线仍为 E → G，普通查询默认仍为 F。

## 当前实现：I-v2-H

版本为 `merged-lazy-memory-I-v2-H`。I 直接使用 H 的 `generalized_lazy_memory.ScriptDB`、`h_plan_fields`、`h_extract_fields` 及独立来源 batch 执行，然后对当前问题的可见陈述做精确合并。来源抽取不接收问题或选项，规划器单独看问题和选项。

继承 H 的全部结构规则：

- 属性保留颜色、材料、形状、大小、纹理、图案、外观、其他八类，值受原文约束。
- 状态按整类 `states` 规划与缓存，记录为实体及 `description`，没有固定 `dimension/value`；一旦请求，不按问题裁减同源明确状态。
- 未知执行者允许模型输出 `actor=null`，存储省略 actor；明确主动主语不能遗漏。
- 按需增量字段缓存、整句覆盖判定、失败时保留原文、当前问题选中来源隔离均沿用 H。

合并规则保持：仅合并最终可见正文完全相同的陈述，允许非相邻重复；保留全部离散采样区间、caption ID、frame index 及可恢复的原顺序。不做同义句合并，不推断连续动作、持续状态、物理身份或事件次数。原文保留优先于存档事实。

2026-11-01 补充：同一陈述内首尾相接的来源区间在最终输入中合并显示，例如 `[165,166), [166,167)` → `[165,167)`；有空档的区间继续分列。这里只压缩已有区间的写法，不额外推断动作持续性。原始 `sample_intervals`、caption ID 和 frame index 保留，审计另存 `display_intervals`；呈现版本为 `merged-lazy-memory-I-v2-H-adjacent-v1`，存储 schema 不变。旧 I-v1 演示及输入已清理；正式 H→I 结果以当前运行目录为准。

```bash
python script_qa.py query --base <base> --questions <questions.jsonl> \
  --out workdirs/query-I-v2-H --memory I
```

也可使用 `configs/script_I.yaml`，模式仍为 `merged_lazy_script`。新版缓存为 `<out>/memory_cache/I/h_script.sqlite3`；其中使用 H 元数据及版本 namespace，包含新版合并器与 H 抽取代码 hash。旧 `I/script.sqlite3`、F 和独立 H 数据库不迁移、不改写。必须使用新的问题输出目录。

输出保留 `field_plan.json`、`lazy_script_memory.json`（兼容观测存档）、`generalized_lazy_memory.json`、`merged_lazy_memory.json` 和 QA 审计。结果方法名为 `merged-lazy-script-evidence-I-v2-H`，审计明确 `parent_memory_variant: H`、开放状态格式及合并版本。旧 I-v1 存储与固定维度状态不能作为新版 H 证据读取。

## 区间合并版排队状态

2026-11-01 01:08（新加坡时间）：用户授权将区间合并版 I 加入 300 题队列。持久控制器位于 `workdirs/merged-lazy-I-H-adjacent-300-20261101-r1-queue/`，已封存本轮合并器、I 实验入口及调度源码。

顺序为现有 `G-RL-train200-eval300-20261031-r3` 全量流程完成 → 启动已准备的 `generalized-lazy-H-300-20261031-r2`（含真实模型功能门）→ 从完整封存 H 准备 I → 在 GPU 3 达到原有空闲门槛后执行 I。I 输出目录为 `workdirs/merged-lazy-I-H-adjacent-300-20261101-r1/`，H 就绪前尚不创建。

状态看队列目录的 `queue_status.json`，各阶段日志和启动记录留存；依赖或子任务失败即停止，不自动重试。H/I 的问题、选项、模型、回答提示和解析保持原样，仅 I Description 加入精确重复正文及相邻区间合并。27 项相关 CPU 检查通过；排队不代表已完成真实模型验证或已有准确率结果。调度入口为 `script/queue_merged_lazy_after_h.py`。

## 离线受控比较

`script/run_merged_lazy_experiment.py` 现在只接受 **H** 的封存快照，主比较为 `I_vs_H_generalized_lazy`。逐题核验 H 源码 hash、materialized seal、H 原呈现及回答对应输入 hash，再仅替换 Description。模型、生成、主解析、问题、选项、旧记忆和视觉补充保持原样。I 独立生成答案，不重放 H 答案；继承 H 的规划／抽取成本单列。

```bash
# H 功能门、全部题目的封存证据及答案齐备后，才可准备。
/mnt/ssd2/hli/work/miniconda3/envs/vts/bin/python script/run_merged_lazy_experiment.py prepare \
  --reference workdirs/generalized-lazy-H-300-20261031-r2 \
  --out workdirs/merged-lazy-I-H-NEW
```

H 目前只有 CPU 准备快照，没有真实模型功能门和封存回答，因此本轮不能从 H 生成正式 300 题 I 输入。入口对此显式报错，不创建输出，不改用 F 证据。代码接通及 CPU 验证不等于 H→I 真实模型验证或全量评测完成。

旧 F→I 准备目录和七题诊断已按用户要求删除；当前 I 仅使用 H→I 正式链，不继承旧版成绩。

## 本轮验证

I-v2-H 及相关回归共 **105 项 CPU 检查通过**，包含：H 的开放 `states` 请求与 schema 分派、未知执行者存储、H 批量解码路径与 token 记账、精确合并与原顺序恢复、旧固定状态及 I-v1 存储拒绝、旧 I 缓存保留、来源盲抽取、字段复用、独立一次最终回答、输入固定区域、失败路径、只接受已封存 H、I 对 H 的配对报告及冻结独立入口。E/F、H、公共批量和 D 相关回归同时通过。CLI 帮助及差异空白检查通过。

上述为实现时的 CPU 验证；后续清理已删除旧版准备与诊断目录。

旧版实验结果与诊断记录已清理。
