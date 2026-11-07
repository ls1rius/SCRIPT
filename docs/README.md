# 文档索引

2026-11-06：**默认研究/评测主线为 Q v4 自适应，Q4+ 为首选及主要报告结果，Q4 为同证据对照。** 未训练版500题均316/500（63.2%），后续以Q4+为主要比较基线；固定6轮训练版仍在原300题开发验证。见 [默认方案登记](experiments/Default_Q_V4_Adaptive_2026-11-05.json) 和 [最新结果记录](history/Q4_Completed500_Fixed6_Progress_2026-11-06.json)。v3完整版及L保留历史比较，关闭早停组仅作历史消融。

当前使用方式和默认 L／可选 E、F、I、M、N 入口见 [项目 README](../README.md)。L 是 I 加独立证据规划筛选，完全版默认仍为 E。

| 文档 | 用途 |
| --- | --- |
| [Q6/Q6+最终问答奖励RL](Q6_Outcome_RL_2026-11-06.md) | 从Q5第6轮SFT继续REINFORCE-LOO；200题固定6轮、Q6+正确性奖励、300题后评测；真实概率/语义门及物理显存池 |
| [Q4 扩500部分性能快照](history/Q4_500_Partial_Performance_2026-11-05.json) | 344/500、全worker等显存；新增44的准确率、来源/Q4处理时间和token，非完整500结果 |
| [Q4 自动显存并发监听](history/Q4_Auto_Concurrency_2026-11-05.json) | 每10秒按有效显存观察，保持原4个worker，上限8个；受控交接、在途调用与冻结结果保护 |
| [Q4 信息类别与跳过率](history/Q4_Retention_Type_Analysis_2026-11-05.json) | 完整300题整句保留统计、上游7类事实映射、题级准确率关联；五类概念与实际实现边界 |
| [Q4 workdirs 搬回原盘](history/Q4_Storage_Return_2026-11-05.json) | 两个300题目录及监控恢复实体目录；历史异盘路径保留兼容软链；逐文件校验、监控接续与运行进程核对 |
| [Q4/Q4+ 保留模块训练版](history/Q4_Retention_Training_2026-11-05.json) | 新增200训练/原300开发测试；180/20选epoch、全200重训；只训练贡献筛选LoRA，独立运行与成本记录 |
| [证据保留与筛选：O/O+ 坑点及 Q/Q+ 要求](Evidence_Retention_Lessons_2026-11-05.md) | 后续筛选必读：判断观察是否可能贡献证据；Q v2 仅 keep 入读者、uncertain 只归档；保留事实的否定/不确定性及完整语义绑定，记录来源、角色、呈现、监控和成本归因边界 |
| [统一 vts 环境](VTS_Environment.md) | 全部现存实验的 Python 环境、版本锁定和兼容链接说明 |
| [Memory_Structure.md](Memory_Structure.md) | 用户确认的完整概念结构图 |
| [F 句子呈现](SCRIPT_F_Sentence_Presentation_2026-11-02.md) | F 的结构化事实用完整句子呈现，保留时间、来源和原文回退 |
| [F 独立小模型证据规划讨论](SCRIPT_F_Evidence_Planner_Discussion_2026-11-02.md) | 任务背景与候选设计；现已命名为 K 并实现排队，后续训练待定 |
| [K：F 独立证据规划器](SCRIPT_K_F_Evidence_Planner_2026-11-02.md) | K-r8全面修复：三组统一约束字母回答，复用有效规划、重生成同协议回答，实时进度见K文档；独立选GPU0–3，不等待J |
| [受控记忆实验](Controlled_Memory_Experiment_2026-10-28.md) | A/B/C 对照、固定输入及成本和评分协议 |
| [B/C 历史实验记录](history/BC_Experiment_Record_2026-11-01.md) | 原版及紧凑版 B/C 已退役；仅保留结果、配置、原哈希和成本摘要 |
| [语义记忆数据结构](Semantic_Memory_Data_Structure_2026-10-28.md) | 分类、来源和逐采样呈现合同 |
| [G 设计](SCRIPT_G_Generalized_Memory_Design_2026-10-30.md) | 完全版 E 的通用结构后续版 |
| [G 执行优化](SCRIPT_G_Execution_Optimization_2026-10-30.md) | 独立来源 batch 的实际验证及差异 |
| [G＋GRPO 历史记录](SCRIPT_G_RL_Experiment_2026-10-31.md) | 最终 300 题结果、训练协议及清理范围；权重、环境和运行实现已删除 |
| [H 实现](SCRIPT_H_Generalized_On_Demand_Memory_2026-10-31.md) | 按需版 F 的通用结构后续版 |
| [I 信息合并](SCRIPT_I_Merged_On_Demand_Memory_2026-10-31.md) | H 的信息合并后续版（F → H → I）；新版与旧 F→I 历史结果分开记录 |
| [L：按需默认，I＋独立证据规划](SCRIPT_L_I_Evidence_Planner_2026-11-02.md) | 普通query默认L，显式I保留；继承H开放状态／未知执行者和I相邻区间，再规划筛选；冻结三组实验与新在线呈现分开 |
| [N：M的后续来源角色约束](SCRIPT_N_Source_Role_Binding_2026-11-02.md) | 逐角色审查及原文依赖保留；109项CPU检查通过，单题未修改父流程B正确，N v1/v2均D错；必要字段规划待改进；M-r13不动 |
| [N：场景初筛修复尝试](SCRIPT_N_Option_Scene_Selection_2026-11-03.md) | 五次诊断仍未通过语义修复：两题不再全排除，但打包仍过度推断且排序失败；M继续不动 |
| [M：在 L 上逐场景研究](SCRIPT_M_L_Gap_Driven_Scenes_2026-11-02.md) | 先看全部总描述，每次只打开一个相关场景；明确当前无法补足的证据缺口才扩展，不强制时间覆盖；CPU验证完成，真实模型待验证 |
| [Case 展示规范](Case_Presentation_Protocol_2026-11-02.md) | 用户确认的原题、场景选择、具体选帧、逐帧原文→解构→筛选合并→实际决策输入的完整展示方式 |
| [L 的真实 bad case](demos/L_r1_2026-11-02/README.md) | 婴儿摇椅：I与全保留答对，L筛选后答错；73个采样点完整前后链路、真实输入及新格式重呈现 |

历史结构、四方案对照和抽取优化调研已移入 `history/`，不能用其中的旧默认设置指导当前查询：

- [早期方案最终实验记录与清理范围](history/Early_Schemes_Experiment_Record_2026-11-01.md)（旧运行产物已删除，结果、配置与成本摘要保留）
- [10 月 19 日时序实体方案](history/SCRIPT_Temporal_Entity_Relation_Plan_2026-10-19.md)
- [10 月 23 日抽取优化计划](history/SCRIPT_Entity_Extraction_Optimization_Plan_2026-10-23.md)
- [10 月 23 日四方案记录](history/SCRIPT_Four_Arm_Experiment_Record_2026-10-23.md)
- [10 月 14 日完整研究计划原稿](history/SCRIPT_Complete_Research_and_Implementation_Plan_2026-10-14.docx)
- [清理前 README](history/SCRIPT_Readme_Before_Cleanup_2026-10-31.md)
- [清理清单及保留边界](history/Cleanup_2026-10-31.md)

`experiments/` 保留原始审计快照；历史 JSON 中的来源路径与 hash 原样保留，不因文件归档重写。

- J：全部合并与证据约束回答，包含原子主张、原文引用校验及严格解析；独立实验入口 `script/run_grounded_reader_experiment.py`，队列入口 `script/queue_grounded_reader_after_i.py`。设计、七题验证及 I→J 全量排队状态见 [J 方案说明](SCRIPT_J_Grounded_Reader_2026-11-01.md)。

K-r8真实逐阶段示例：[原问题、选项、结构化档案、规划决策、保留证据及完整最终模型输入](demos/K_r8_2026-11-02/README.md)。包含真实分组、筛选和过度筛选反例。

- 2026-11-03：[M/N 候选筛选修订与优化后 M 独立重跑](SCRIPT_MN_Candidate_Repair_2026-11-03.md)；旧 M-r13 第 95 题自行失败，r14 已通过真实功能门并进入全300题重跑，N 同步选择器。

- 2026-11-03：[N 全300题独立启动记录](history/N_300_Launch_2026-11-03.json)；GPU0真实功能门通过、进入正式第1题，M继续GPU2。

- [M/N 导航 JSON 提前结束修复与接续（2026-11-03）](SCRIPT_MN_Navigation_Decoder_Repair_2026-11-03.md)：CPU 复现并阻断 UTF-8 字节 token 导致的错误 EOS，保留各 12 题，M-r15/N-r2 独立等待显存后验证并接续。

- [O/O+ 逐句二元筛选试验（2026-11-03）](SCRIPT_O_Oplus_Sentence_Filter_2026-11-03.md)：保留各轮方法与失败历史；全量 O/O+ 已完成，当前 O+ v15 采用来源约束有限表达。后续改进要求及 Q/Q+ 边界见 [证据保留经验记录](Evidence_Retention_Lessons_2026-11-05.md)。
