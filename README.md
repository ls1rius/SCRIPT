# SCRIPT implementation (2026-10-14)

此工作区新增了方案文档中的 SCRIPT 研究原型，入口为 `script_qa.py`。
原有 VideoTree 的 `demo.py` 与实验脚本继续保留在下文。

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
修复清单位于 `docs/SCRIPT_Evidence_Repairs_V7c_2026-10-16.json`。

2026-10-15 的修复保持原模型、600 秒在线预算和最多 8 轮任务。
全局活动题先按预算安排默认 6 个分散时间点，优先观察前、中、后段，剩余轮次
用于选项区分；真正要求全部、计数或最后一次的题仍保留严格覆盖条件。
反复或转折问题另需跨时间事实支持，选择题停止前逐项核验选项及事实引用。
整题证据不足时，最终回答仍可读取独立核验的局部事实，内部不足状态继续保留。
不在实际观察区间中的实体角色不能进入已支持事实；单题异常单独记录并继续后续问题，
有答案标注的失败题计为错误。

模型上下文省略存储元数据和无关实体历史，保留事实文本与所有输入帧；图像仍逐帧标注
实体 ID。必要时按实际 token 数缩小图像并记录模型实际输入尺寸，覆盖检查使用这些尺寸；
文本评估超限则显式分批，不静默截断事实。后续方案实验限于已固定的 100 道开发题，
修复结果使用新目录和源码快照，不覆盖旧实验。少量定向诊断题用于运行检查，不能代表整批准确率。

观察模块现在接收全部候选选项和检查目标，优先提取工具、左右手、准备步骤及顺序等区别；
候选文字不代表已见事实，提出的事实仍由不接收选项的视觉核验模块独立检查。
观察缺口带原视频区间保存，并传给后续规划、语义核验和最终回答；缺口不作为反证。
输出缓存同时区分问题、选项、检查目标和观察目标，防止把泛化描述复用于不同的细节检查。

`configs/script_guided.yaml` 提供独立的剧本引导读取策略：普通概括选择题先规划关键缺口，
只细看 0–3 个不同窗口，不机械完成六点覆盖或反复 assess；粗描述上下文限制为 12 个完整窗口，
已核验事实保留。成本停止仍记录证据不足，计数、最后一次、否定及穷尽类问题沿用原覆盖流程。
M0 摘要字段在约束解码时限制长度，短单窗口描述直接复用；新建库成本与在线问答分别验收。
实现与诊断安排见 [少看精看与建库耗时](docs/SCRIPT_Selective_Reading_2026-10-16.md)。

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
`docs/SCRIPT_Implementation_Acceptance_2026-10-15.json`。
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
