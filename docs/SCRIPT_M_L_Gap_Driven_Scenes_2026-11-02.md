> 当前修订见 [M/N 候选筛选修复与 M 重跑](SCRIPT_MN_Candidate_Repair_2026-11-03.md)。本文既有运行与失败记录保留为历史；共享选择器已更新，M-r14 已过真实功能门，开始全300题重跑。

# M：基于 L 的有界候选场景检索

2026-11-02，新加坡时间。当前方法 `M-L-bounded-candidates-v7-ranked-decisions`，显式模式 `scene_planned_script` / `--memory M`。按需默认仍为 **L**，完全版默认仍为 **E**；M是L之后的独立研发分支。

## 已确认的流程

**全部场景总描述 → 确定相关候选及优先级 → 固定候选队列 → 先查最可能的场景 → 细查当前原文／局部抽取 → 明确缺口 → 当前无法补足才查队列下一项 → 足够即停止，候选耗尽仍不足也停止。**

初筛 `m_select_scene` 联合读取问题、全部选项和全部场景summary。每个场景都有一个必填决策：candidate及1–100优先级，或excluded及0优先级，以及原始理由。优先级是模型排序分数，不是校准概率。程序只收集candidate，按优先级降序排队；同分按原目录顺序稳定排序，仅在已选候选内部使用该规则。模型不再独立生成第二份名单，因此“全部排除但名单里又有场景”的矛盾不能通过解码。

候选集可以为空、只有一个或有多个，不设最少数量，不为凑数选场景。初筛完成后集合封闭；后续目录仅含候选，allowed_scene_ids仅含队列中下一未打开场景，解码器与程序共同拒绝越界。不会因为仍查不到就回退到全目录，不按时间点补场景，不强制至少两个或覆盖全视频。初筛本身仍可能误选／漏选：硬边界保证不越出名单，不保证模型判断的相关性必然正确。

每轮保留当前和此前打开场景的完整caption，只在当前来源中做局部抽取。信息不足且仍有候选时，不能以no_useful_step提前放弃整条队列；局部请求已检查且没有进展时，转入明确缺口解决阶段，再检查下一候选。队列耗尽且信息不足记candidate_exhausted；初筛无候选记no_relevant_candidates。两者都不表示全视频不存在所需信息，只表示本次相关候选没有提供足够支持。

在线1500秒、最终回答保留45秒、最多10个导航／局部抽取步骤等资源上限保留。M不再沿用固定最多4个场景的旧默认，而以候选集大小作为访问边界；显式资源上限、step_limit及预算耗尽单列，不能记作候选已查完。coverage_check保持enforced=false、met=null。

## L证据链与呈现修复

检索之后仍沿用H开放状态／八类属性／未知执行者、来源盲按需抽取和独立M缓存，再经I合并完全相同陈述及相邻区间，再经L统一候选池、逐ID保留／跳过／不确定／分组、完整来源审计及一次严格字母回答。不复用历史L的选源、事实或答案；缓存为当前输出根下独立memory_cache/M/I/h_script.sqlite3。

旧M-r10第3题在时序实体含属性时抛出“New temporal attribute schema needs explicit verbalization”。M现在通过显式回调将field/value/source_caption_ids/observed_at记录转为“文本记忆记录某属性及值”的句子，保留实体文本引用、原始记录、全部来源和时间，标为text-inferred hypothesis。未知schema或引用未读caption仍拒收。默认L及K旧接口保持原行为，冻结实验完全不改写。此修复处理呈现缺口，不声称纠正了上游属性语义或实体归属错误。

呈现沿用用户最新偏好：Frames、同陈述的相邻区间合并、不跨空档；省略默认source assertion标签，保留推断／未解决标签及完整来源审计。原始Qwen2.5-VL-7B-Instruct、无adapter／训练、bf16贪心解码。初筛最多4096token，L规划8192token，最终回答1024token；无隐藏截断或失败后自动重试。

空候选时不打开场景，不调用H/I抽取或L规划，不伪造非空证据池。固定选择题评测仍生成一次约束字母回答，同时显式标记证据不足、零已观察来源及planner_skipped_no_evidence；不能把该字母视作有证据支持的结论。

## 逐轮记录与已知限制

每题保留directory.json中的全部原始summary、区间与来源；partial_search/navigation_trace.json随选择、打开场景、检查提议、校验、局部抽取提交及停止逐步落盘，包含原始场景目录、每个候选／排除决定和分数、排序规则、实际打开的caption原文与帧索引、每轮缺口／无法补足理由、提议与程序实际行为。calls.jsonl和raw_calls保留原始调用；失败／中断也保留已产生记录。最终M池、计划、来源审计及实际输入位于partial_temporal/M。遵循[Case展示规范](Case_Presentation_Protocol_2026-11-02.md)。

技术门与语义诊断分别报告。技术门检查来源／解码、130条规划、明确缺口执行、候选边界、空候选、属性呈现和完整流水线。自主充分性仍可能仅匹配一个选项的一部分就提前停止；逐选项与独立两两核对不证明语义正确。该诊断失败如实保留，不强制补场景或声称推理问题已修复。

## 正式运行

当前 `workdirs/scene-planned-M-L-300-20261102-r13/`，控制器PID2749971，worker PID2750068，21:58:27在GPU0启动，22:01:54全部真实技术门通过，开始同一固定300题的第1题。无候选用例初筛为空且实际打开0个场景；单场景与前后比较用例分别只打开初筛相关候选。当前合成语义诊断也通过，但不能据此宣称充分性判断已普遍可靠。46项相关CPU检查通过，原第3题保存数据已在CPU复现旧错误并验证修复，136条候选及1条时序属性完整审计。实时状态以运行目录为准，不重复启动。

M-r10完成的2题、第三题失败及全部旧成本归档；流程已改变，因此新版300题全部新生成，不将旧2题混入主结果。r11空候选验证发现独立名单与排除决策矛盾，在正式题前停止；r12改成单一逐场景判定及确定性排序，旧快照和成本完整保留。r12又在无候选用例中暴露LMFE非空字符串的旧字符集缓存问题：中文首字后错误允许EOS。r13在M解码器安装真实tokenizer字符集后刷新对应缓存，真实tokenizer回放确认未闭合JSON不会提前结束；没有改变候选边界或添加重试。所有版本使用vts，K/L/H/I冻结结果与默认L／E保持。

调度仍为GPU0–3按可用显存优先、至少32768MiB、maximum_utilization=100、10秒同卡连续3次、共享锁和锁内复查。控制器独立后台运行，失败即停，不自动重试。

报告每题更新，待执行不计错；平均初始候选数、实际打开数、outside_candidate_openings及停止原因另列。目录、初筛、检索、局部抽取、H/I构建、L规划、充分性核对、最终回答分别计token与耗时，历史成功／失败调用和准备成本累计继承。继承来源预处理未重测的成本不称作零，日志外完整性复查和序列化不计入逐操作耗时。共享GPU耗时不表述为受控加速比。

```bash
cat workdirs/scene-planned-M-L-300-20261102-r13/queue_status.json
cat workdirs/scene-planned-M-L-300-20261102-r13/validation/functional.json
cat workdirs/scene-planned-M-L-300-20261102-r13/report.json
```

实现：script_core/m_scene_memory.py、script_core/adaptive_transition.py、script/run_m_scene_experiment.py。记录：[候选集流程更新](history/M_Bounded_Candidates_2026-11-02.json)、[实际失败case修复回放](history/M_Bounded_Candidates_Attribute_Replay_2026-11-02.json)。旧版本详情仍保存在各冻结目录及[此前充分性／接口修复记录](history/M_Option_Sufficiency_Repair_2026-11-02.json)。
