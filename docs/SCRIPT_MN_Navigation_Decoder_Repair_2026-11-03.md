# M/N 导航 JSON 提前结束修复与接续（2026-11-03）

用户授权：修复 bug 后继续跑。M-r14、N-r1 均在第 13 题 `d85518f1-eee2-4d34-b955-6052f3d7dd2d` 的 `m_transition_next` 调用失败；此前各自 12 题成功、9 题正确。旧运行已经退出，全部保留。

## 原因与修复范围

原始输出仅 399 token，低于 1024 上限。末尾理由停在罕见字符“扥”，JSON 字符串未闭合。CPU 使用真实 Qwen tokenizer 复现：LMFE 把不完整 UTF-8 字节 token 当替换字符放行，拼接成不在字符表中的字符后，`JsonSchemaParser.add_character` 清空解析栈，错误允许 EOS。旧记录未存 token IDs，因此复现使用兼容的逐字符分词路径，并非声称恢复了当时精确 token 序列。

`script_core/backend.py` 的 `navigation_tokenizer_data` 为 `m_transition_next`、`m_sufficiency_check`、`m_resolve_gap` 创建并缓存完整 Unicode token 表，排除解码包含替换字符的 token；字符表包含所有保留 token 内的字符，而非只有首字符。正常中文及 JSON Unicode 转义可继续生成。此限制可能改变罕见字符的生成路径；不会增加语义证据、代写缺失字段或放宽 JSON/来源校验。保留原 1024 token 上限及失败即停策略。

新增导航解码版本 `MN-navigation-complete-unicode-v1`；原始调用增加 generated_token_ids、max_new_tokens、decoder，便于以后追溯。M/N 方法版本仍为各自 v8：问题、选项、提示、schema、初筛、抽取、证据规划、最终回答均沿用各自冻结运行。N 保留源抽取 v2、独立缓存和原文保留规则；不宣称解决其搬砖 case 的 QA 退步。

## 验证

48 项 CPU 回归通过（26.843 秒）：旧提前 EOS 复现、修复后禁止字节碎片、完整 JSON 闭合、中文/转义/长度边界、导航来源和候选边界、M/N 模式及接续成本统计。准备快照前新增运行时文件变更白名单审计，相关接续测试再次通过。测试不等于真实模型验证或准确率改善。

各接续控制器拿到足够显存后，先新生成原第 13 题失败输入的导航调用，检查完整 JSON/schema/来源/动作契约；再执行原有完整真实功能门（N 另核查 n_extract/n_evidence_plan/n_answer、独立缓存和来源保留审计）。通过才进入第 13 题正式执行。任何失败都停，不自动反复尝试。

## 接续与成本

| 方案 | 旧失败运行 | 新接续运行 | 控制器 |
|---|---|---|---|
| M | scene-planned-M-L-300-20261103-r14 | scene-planned-M-L-300-20261103-r15 | 3010373 |
| N | scene-planned-N-L-300-20261103-r1 | scene-planned-N-L-300-20261103-r2 | 3010375 |

入口 `script/run_mn_decoder_resume.py`。各自前 12 题的结果、导航、原始调用和审计逐文件原样继承并纳入冻结哈希；第 13 题不继承失败中间状态，重新执行整题，此后继续到第 300 题。报告显式记录继承 12 题及新解码器，不冒充全部由新运行生成。

旧第 13 题完整目录保存在新目录 `archived_attempt/failed_query/`，旧功能门、加载及报告另归档；原目录不改。继承成功题成本只计一次，旧加载/功能门/失败题成本进入 prior_runtime_seconds，失败题成本另列 prior_failed_query_seconds。新功能门及后续调用另行实测，不隐藏旧成本。

新 runtime 从各自旧 runtime 复制，只替换 backend.py、共享运行入口，并新增接续入口。旧 M 全部 1574 个非 pycache 文件、N 全部 1603 个文件及各自 507/508 个冻结文件哈希保持。

准备时磁盘剩余约 151GB，两份接续快照合计约 150MB，不复制模型或视频帧。两个控制器已启动并独立监听 GPU0–3；当前 waiting_for_gpu，真实门尚未运行，不能称为正式题已恢复。保持可用显存至少 32768MiB、利用率上限 100%、每 10 秒连续 3 次及同卡锁内复查；不要求空卡、不互相等待，也不停止其他进程。满足资源条件后自动执行上述验证和接续。

实时以新目录 queue_status.json、validation/functional.json、progress.json、report.json 为准，不重复启动。记录见 [修复与启动记录](history/MN_Navigation_Decoder_Resume_2026-11-03.json)。默认按需 L、完全版 E 不变；仅使用 vts，无训练或 adapter。

运行更新：09:07:54 现有控制器自动接卡，M-r15 在 GPU2（worker3015984）、N-r2 在 GPU0（worker3015989）启动。两边原失败输入的真实导航调用已通过，剩余完整功能门正在执行；通过后从第13题接续，不重复启动。


## 第45题候选规划解码修复与 M-r16 接续（12:51 更新）

M-r15 在 10:58 于第45题 `943374f4-4514-4e22-827b-7452b91e5559` 停止：44题成功、25题正确，失败题完整保留。出错操作是 `m_candidate_query`，353 token，未到1536上限；`retrieval_question` 在240字符边界以“繫”结束，JSON 未闭合。原调用没有 token IDs，CPU 用保存的末字段及兼容逐字符分词复现旧 LMFE 提前 EOS；不能声称恢复了历史精确 token 序列。

上次完整 Unicode token 修复仅覆盖三个导航操作，漏了有自由文本长度限制的候选规划。此次 `MN-navigation-candidate-complete-unicode-v2` 将相同保护加入 `m_candidate_query`，并为其保存 generated_token_ids/max_new_tokens/decoder。提示、schema、问题和全部选项、选源、抽取、规划及回答规则不变；不补写 JSON，不增生成重试。

候选排序 `m_candidate_rank` 仍用原解码路径：其 anchor 是原文枚举，不能简单施加自由文本的 token 过滤。开发检查发现合成罕见字符引用 `C 修理繫绳。` 在旧 LMFE 枚举路径也不可走通，已单独保留为原有局限；本次不声称修复它。普通中文引用、自由文本中文和 Unicode 转义测试通过。两个较早 CPU 检查因尝试扩大排序解码范围而失败，之后收窄补丁；未产生模型调用或模型开销。

52项 CPU 回归通过（31.218秒），包含两次历史提前 EOS 复现、修复后禁止字节碎片及正常 JSON 闭合、长度边界、原文枚举路径不变、按原操作执行真实门的调度、失败禁止进入下一阶段、继承来源与成本核对。报告元数据调整后，7项接续/报告检查再次通过（0.229秒）。CPU 通过不等于真实模型门通过或 QA 提升。

新运行 `workdirs/scene-planned-M-L-300-20261103-r16/`，控制器3125972，于12:51:45启动排卡。原 M-r15 的4452个非 pycache 文件哈希不变；前44题连同所有导航和原始调用逐字继承。runtime 复制自 M-r15，只对 backend 应用精确解码补丁、更新接续入口与报告元数据，不将工作区 O/O+ 等其他修改带入。第45题从整题开始重新执行，不沿用失败中间状态；先新生成同一失败输入的 `m_candidate_query`，再跑完整真实功能门，通过才正式接续到300题。

继承成功题处理成本10950.229218秒，累计旧加载/门/失败开销424.018771秒分列，其中历史失败题成本累计111.439178秒（本次第45题20.933599秒）。原失败及原成本不删除。完整审计见新目录 `validation/continuation.json`、`archived_attempt/`、`validation/failed_decoder_call.json` 和 [候选解码修复记录](history/M_Candidate_Decoder_Resume_2026-11-03.json)。

N-r2 原控制器3010375/worker3015989继续运行，1601个冻结文件哈希保持，未热更新或重启。共享源码已有未来可用修复，但当前 N 冻结 runtime 未变。本次默认 L/E 不变，只用 vts，无训练或 adapter。排卡仍要求至少32768MiB、利用率上限100%、每10秒同卡连续3次和共享锁内复查；等待中的 M 不抢占 O/O+ 或 N。

当前 M-r16 等待 GPU 共享锁，真实门尚未运行；最新状态读取 queue_status.json、validation/functional.json、progress.json、report.json，不重复启动。


## 用户明确要求 M/N 均用修复版恢复（13:03 更新）

用户本轮要求“MN在bug修复后重新跑起来”，覆盖此前保持运行 N-r2 不切换的限制。M-r16 原控制器3125972保持，不重复启动；它已在GPU2启动worker3131077。N-r2 在13:00:56于模型调用间隙受控停止：先暂停协调进程，确认全部 runs 下没有待完成 `.pending.json`，再向协调进程发送 SIGINT 并恢复，由既有 finally 关闭模型进程。1601个冻结文件未变，已有原始调用均保留，没有中断正在进行的模型调用。该停止属于用户要求升级，不冒充新的模型bug。

N-r3 为 `workdirs/scene-planned-N-L-300-20261103-r3/`，控制器3132371，worker3132622，在GPU0运行；完整继承前40题（23正确）和全部导航/原始调用，从未完成的第41题 `e04cd624-17e1-4986-b344-55aa92d7c0c3` 整题重做。原第41题目录保存在 `archived_attempt/interrupted_query/`，135.024237秒记入 prior_interrupted_query_seconds；旧失败开销88.358786秒仍单列，累计旧加载/门/失败/中断开销698.584530秒。原N停止后的4274个非pycache文件核对不变，新N4128个冻结文件核对通过。

接续入口新增显式 `--upgrade-record` 和 `--decoder-probe`，只接受已停止进程和确认没有未记账模型调用的升级记录。成功前缀逐字继承；中断题和原失败题分别归档与计费。12项相关CPU检查通过（0.221秒），覆盖中断成本、完整题间隙切换、未落盘调用拒绝和原失败接续。新 N 使用同一共享候选规划故障输入做解码器回归，不复用 M 的导航、证据或答案；随后执行完整 N 专用路径功能门。

截至13:03，M/N原失败输入真实回放均已通过；两边完整功能门仍在执行，尚不声称正式题已恢复。M通过后从第45题继续，N通过后从第41题继续到300。失败即停，没有隐藏重试。详细进度及最终验证状态见 [M/N修复后启动记录](history/MN_Repaired_Restart_2026-11-03.json)，停止审计见 [N受控停止记录](history/N_Decoder_Upgrade_Interruption_2026-11-03.json)。

实际恢复更新（13:07）：M-r16 于13:04:37、N-r3 于13:06:12均通过完整真实功能门，前后状态比较诊断均通过；N独立调用/缓存/来源保留检查全部通过。M在GPU2正式第45题，N在GPU0正式第41题，均running。M正式第45题的 m_candidate_query 和 m_candidate_rank 已生成完整合法JSON，越过原失败点；两边回归输入均原样使用，输出352token、上限1536。前44/40题与旧档案hash再次核对不变。以上仅为功能验证和恢复运行，不是300题最终结果或普遍语义可靠性的保证。
