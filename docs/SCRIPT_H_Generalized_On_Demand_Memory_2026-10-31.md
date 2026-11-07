# H：基于 F 的通用按需记忆

> 2026-11-01 依赖清理更新：H-r5 与 I-r4-queue 保持运行；J 改为 J-r7-queue（输出仍为 J-r5），只解除已删除七题诊断目录的前置依赖。J 方法和 H→I→J 顺序不变，下文旧 J 队列编号为历史。

> 2026-11-01 用户明确恢复监听与运行，覆盖下文暂停记录。新封存 H-r5 使用 `generalized-lazy-memory-H-v3-predicate-binding`，保留 H-r3/r4 失败输出与成本；H-r5 → I-r4-queue（输出 I-r3）→ J-r6-queue（输出 J-r5）。I/J 方法保持，使用原始 Qwen，I 队列不再要求独立 G＋GRPO 前置任务。四卡按原阈值监听并择一卡串行执行；真实功能门通过后才开始 H 的 300 题评测，失败停止，不自动重试。运行状态见 `workdirs/generalized-lazy-H-300-20261101-r5/queue_status.json`，排队不等于真实模型验证通过。

> 用户最新决定：修复代码，暂不运行并释放显卡。H-r4 于 2026-11-01 14:39 再次未通过功能门：左手和执行者已通过，工具仍被挂到 `with a crane` 伪动词。原失败输出、报告和成本保留。工作区新版本 `generalized-lazy-memory-H-v3-predicate-binding` 用有限谓词格式限制解码；显式 actor 分支与 null actor 分支分别约束，null 分支的谓词需通过既有来源语法检查；串行和批量接受路径均使用 H 合约。该语法检查不是完整英文语义解析，仍保留无法完整表达时的原句回退。73 项 CPU 检查通过，包含 LMFE 对旧错误输出的拒绝以及合法主动／被动／祈使、短语动词、否定句等检查；未运行模型，不声称真实功能门已通过。当前 H/I/J 模型和控制器均已退出，不恢复监听、不启动新快照，待用户明确恢复。

> 2026-11-01 修复并重新排队：H-r3 的真实功能门失败（角色增量输出丢失明确 actor、工具被挂到 `with` 伪动作），未进行正式评测。新 H-r4 使用 `generalized-lazy-memory-H-v2-action-binding`，动作角色请求同时包含动作骨架，强化角色绑定提示并排除独立功能词作动词；来源校验保持。51 项 CPU 检查通过，真实功能门等空闲卡后执行。当前链：H `generalized-lazy-H-300-20261101-r4` → I 控制器 `merged-lazy-I-H-adjacent-300-20261101-r3-queue`（输出 I-r2）→ J 控制器 `grounded-reader-J-300-20261101-r5-queue`（输出 J-r4）。各阶段均监听 GPU 0/1/2/3，每 10 秒采样，同卡连续 3 次空闲显存 ≥32 GiB、利用率 ≤10%，获取锁并复查后择一卡运行。H/I/J 无 GRPO；I 合并方法、J-v3 方法保持，旧失败记录与成本保留。下文旧调度路径均为历史。

调度更新（2026-11-01 01:41，新加坡时间）：用户明确 H/I/J 不使用 GRPO，且不依赖 G＋GRPO 完成。H 活跃目录为 `workdirs/generalized-lazy-H-300-20261101-r3/`，从尚未启动的旧 H-r2 复制，1385 个冻结方法文件保持一致，只将独立调度改为 GPU 2。旧 H-r2 标记为未启动的调度快照，源代码与数据库不改写。H 真实功能门和逐题评测由 I-r2-queue 控制器启动；后续 I/J 都读取原始 Qwen2.5-VL-7B-Instruct，不加载训练 adapter。本文此前仅 CPU 准备的记录是历史状态，当前进度以新目录的状态和报告为准。

2026-10-31，新加坡时间。用户确认两条路线分别为 E → G（完全版）和 F → H（按需版），并要求先实现 H。默认方案仍为 E/F；G/H 是各自的后续实验版本。用户随后将 I 调整为 H 的接续，当前按需路线为 F → H → I；I 继承 H 的抽取与状态规则，再合并完全相同的可见陈述。旧 F→I 结果仅作为历史 I-v1 保留。

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
/mnt/ssd2/hli/work/miniconda3/envs/vts/bin/python script/run_generalized_lazy_experiment.py prepare \
  --reference workdirs/lazy-script-F-300-20261030-r3 \
  --G workdirs/generalized-entities-G-300-20261030-r3 \
  --out workdirs/generalized-lazy-H-NEW --batch-size 2

# 需要启动评测时，使用该目录已冻结的入口；先执行真实模型功能门。
/mnt/ssd2/hli/work/miniconda3/envs/vts/bin/python \
  workdirs/generalized-lazy-H-NEW/control/run_generalized_lazy_experiment.py queue \
  --out workdirs/generalized-lazy-H-NEW
```

## 验证状态

H 专项检查位于 `tests/test_generalized_lazy_memory.py`、`tests/test_generalized_lazy_execution.py` 和 `tests/test_generalized_lazy_experiment.py`，覆盖开放状态、未知执行者、真实 LMFE 解码器、按需字段投影、增量及重复读取、多条件来源和重复采样、旧数据库只读拒绝、来源保留、独立批量约束、费用及预算失败、冻结运行时接入和逐方案配对报告。

已通过 35 项 H 专项 CPU 检查；相关 F/G 及公共批量解码回归另有 52 项。还用真实冻结 F 运行时的副本核验 H 的 schema 分派、提示、开放状态和未知执行者解码，以及保留的 F 固定维度 schema。

准备目录为 `workdirs/generalized-lazy-H-300-20261031-r2/`，固定同一批 300 题，状态仅为 `prepared`。r1 是此前的 CPU 准备快照；r2 放宽功能门对合法状态表述的匹配，允许带冠词的实体名称及包含完整条件的描述，未修改结构规则，也未读取 benchmark 正确率选版本。r1 不启动 GPU，原快照保留。

本轮没有加载 7B 模型、运行 GPU 功能门或生成 H 的 benchmark 答案；尚无 H 准确率或实测推理性能。实现及 CPU 验证不等于真实模型效果已验证，H 实现时通用在线入口仍为 D；后续已按用户要求接入 E/F 切换并默认 F，H 仍使用独立实验入口。
