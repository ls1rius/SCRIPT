# L/M/N/O/O+ 三例处理对照

来自当前正式300题运行，O/O+均为不带选项筛选/重组，最终回答统一带全部选项。三例按已知错误类型挑选，仅用于诊断，不用于估计整体准确率。L是归档L-r1；L与M/N取证范围不完全相同。

| 案例 | 标注 | L | M | N | O | O+ |
|---|---|---|---|---|---|---|
| [动作协调与平衡](01_coordination/case_review.md) | B | B | A | A | A | E |
| [熨衣还是折衣](02_ironing/case_review.md) | C | C | A | A | C | C |
| [车库木工还是寻找物品](03_woodwork/case_review.md) | E | E | C | E | E | E |

三例的O批输入逐行映射核验通过；发现的是当前题目内的筛选/重组语义问题，不能据此推断全部失败原因。原始文件hash和只读审计见[audit.json](audit.json)。
