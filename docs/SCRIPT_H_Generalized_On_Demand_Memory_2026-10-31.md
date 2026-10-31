# H：基于 F 的通用按需记忆

2026-10-31，新加坡时间。用户确认两条路线分别为 E → G（完全版）和 F → H（按需版），并要求先实现 H。默认方案仍为 E/F；G/H 是各自的后续实验版本。

## 实现范围

| 部分 | H 的行为 |
| --- | --- |
| 字段规划 | 规划器看问题、选项和字段目录；只输出需要检查的字段，始终补入 `actions.base` |
| 来源范围 | 沿用上游已选来源，不进一步缩短时段、不新增检索或看图 |
| 来源抽取 | 只接收单条原文、来源 ID 和缺失字段，不接收问题、选项、正确标签或图像 |
| 属性 | 保留八类 `PROPERTIES`，属性值受当前来源约束，无额外封闭值表 |
| 状态 | 使用单个请求字段 `states`，抽取记录为 `entity/description`；没有固定维度，也不保存 `dimension/value` |
| 未知执行者 | 抽取输出可为 `actor=null`；事实存储省略未知 actor，不虚构身份；主动动作的明确主语不能遗漏 |
| 实体／时间组织 | 延续 F 的来源内动作字段拼接和逐采样呈现，不加入 E/G 的实体身份时间线或 D 的时间段合并 |
| 角色复核 | 延续 F，不增加 E/G 的额外角色复核调用；拒收或不确定信息保留原文 |
| 执行 | 默认两条独立来源组成 batch，支持 1/2/4；每条来源有自己的 schema、原文词表与校验 |
| 回答 | 保持冻结 F 的原 QA 模型、精度、提示固定区域、生成与主解析协议；每题只生成一次 H 答案 |

开放状态按整类缓存是有意的：取消固定维度后，规划器只决定是否需要状态信息；一旦请求 `states`，就在既有 schema 和输出上限内抽取同源明确状态，不按问题或某个候选状态值再筛选。这让同一来源的状态缓存可被不同问题复用，也避免重新引入隐藏的封闭状态表。未可靠覆盖的复杂信息仍保留完整原文。

状态的采样时间、caption ID 和来源映射保存在观测记录中，状态事实本身保存实体和描述。相同原文可共享字段缓存，但不同采样时刻各自保留；不据此推断连续状态或事件次数。开放描述沿用 G 的原文片段和同句绑定检查，这些检查不等于完整语义证明。

## 缓存与冻结隔离

核心为 `script_core/generalized_lazy_memory.py`，版本为 `generalized-lazy-memory-H-v1`；规划／抽取操作分别为 `h_plan_fields`、`h_extract_fields`。实验另将模型配置和相关源码 hash 纳入缓存版本。

H 数据库为 `database/h_script.sqlite3`，包含独立 H 元数据。打开既有 F 数据库时先只读检查并拒绝，不自动迁移或改写。字段区分未抽取、`value`、`checked_absent`、`uncertain`、`failed`；后两者不自动重试，`checked_absent` 只表示来源没有明确陈述该请求字段。

呈现复用 F 的紧凑容器：只有全句覆盖检查通过时才省略原文；否则逐条保留原句。缓存中其他任务的来源不会自动进入当前问题的视图，当前未请求字段也不投影。基础动作仍作为支持信息保留，目的／变化字段不能替代实际观测。

执行模块为 `script/generalized_lazy_execution.py`，只处理 H 操作；复用公共 `script_core/batch_extraction.py` 的独立来源解码器。模型故障立即停止，不拆批重试或静默降级。批次时间只计一次，来源请求数另列；批量浮点差异可能改变输出，不声称与串行逐字一致。

## 实验入口与比较

`script/run_generalized_lazy_experiment.py` 支持 `prepare`、`worker`、`queue`、`report`。准备时核验冻结 F 的 hash 和功能门，复制其 QA 运行时及相同问题、来源、原始输入，在副本中添加 H。F/G 抽取、数据库及预测不会复制为 H 的结果，原有 `LocalModels.call` 的 AST 保持一致。

主要比较为 H 对 F；可指定 G 作同题比较，并保留 A/B/C/D/E 的各自配对分母。尚未执行的题不计错，完成前不报告全量准确率。报告单列规划／抽取请求数、开放状态与未知执行者事实观测数、来源覆盖、缓存状态、最终输入 token、回答耗时及各阶段账本成本。

每题沿用 F 的 1500 秒预算、45 秒回答预留和 1024 token 抽取输出上限。执行队列要求所选 GPU 连续三次至少有 32768 MiB 空闲，允许共享，不停止其他任务。共享环境中的耗时不是受控速度比较。

```bash
# 仅做 CPU 准备；使用新目录，不启动模型。
workdirs/query-runtime-env/bin/python script/run_generalized_lazy_experiment.py prepare \
  --reference workdirs/lazy-script-F-300-20261030-r3 \
  --G workdirs/generalized-entities-G-300-20261030-r3 \
  --out workdirs/generalized-lazy-H-NEW --batch-size 2

# 需要启动评测时，使用该目录已冻结的入口；先执行真实模型功能门。
workdirs/query-runtime-env/bin/python \
  workdirs/generalized-lazy-H-NEW/control/run_generalized_lazy_experiment.py queue \
  --out workdirs/generalized-lazy-H-NEW
```

## 验证状态

H 专项检查位于 `tests/test_generalized_lazy_memory.py`、`tests/test_generalized_lazy_execution.py` 和 `tests/test_generalized_lazy_experiment.py`，覆盖开放状态、未知执行者、真实 LMFE 解码器、按需字段投影、增量及重复读取、多条件来源和重复采样、旧数据库只读拒绝、来源保留、独立批量约束、费用及预算失败、冻结运行时接入和逐方案配对报告。

已通过 35 项 H 专项 CPU 检查；相关 F/G 及公共批量解码回归另有 52 项。还用真实冻结 F 运行时的副本核验 H 的 schema 分派、提示、开放状态和未知执行者解码，以及保留的 F 固定维度 schema。

准备目录为 `workdirs/generalized-lazy-H-300-20261031-r2/`，固定同一批 300 题，状态仅为 `prepared`。r1 是此前的 CPU 准备快照；r2 放宽功能门对合法状态表述的匹配，允许带冠词的实体名称及包含完整条件的描述，未修改结构规则，也未读取 benchmark 正确率选版本。r1 不启动 GPU，原快照保留。

本轮没有加载 7B 模型、运行 GPU 功能门或生成 H 的 benchmark 答案；尚无 H 准确率或实测推理性能。实现及 CPU 验证不等于真实模型效果已验证，H 实现时通用在线入口仍为 D；后续已按用户要求接入 E/F 切换并默认 F，H 仍使用独立实验入口。
