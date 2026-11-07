# L bad case：组装婴儿摇椅时的附加活动

来源：正式L-r1，题目 `73ecc613-5e39-4348-94ba-ee6cb197367a`。I对照和L统一候选全保留均答B；L规划版答D；标准答案B。真实输入逐字导出，本次没有模型推理或重答。

## 原题与全部选项

Based on the video, what additional activity was c engaged in while primarily focusing on working on the baby swing chair?

A. C was also listening to music while assembling the baby swing chair.

B. C was also talking to a woman while assembling the baby swing chair.

C. Concurrently, c was attentively watching tv while skillfully assembling the comfy baby swing chair.

D. Concurrently, c was eating as well as meticulously assembling the baby's comfortable swing chair.

E. Curiously, c was also dozing off intermittently while diligently assembling the baby swing chair.

## 场景与读取范围

边界由保留特征和归档参数CPU重建；summary由本次依据原始caption补写，不是当时模型summary。原始场景summary和逐轮导航理由未保留；本次不补造旧日志。L继承H→I的固定已选来源，没有重新检索。

| 场景 | 区间／秒 | 本次依据caption补写的summary | 原运行读取 |
|---|---|---|---|
| S0 | [0,17) | C在厨房、餐厅与房间间移动，接近餐桌并移动相机；其他人在屋内走动。 | 否 |
| S1 | [17,34) | 餐桌附近多人移动椅子、布和其他物品；C持厨房毛巾、离开桌边，并移动容器中的手电。 | 是，17个采样点 |
| S2 | [34,79) | C围绕婴儿、椅子和桌面活动，拿放枕头、布、线缆、容器等物品；其他人也在附近活动。 | 否 |
| S3 | [79,105) | C抱起椅子上的婴儿、放枕头、持书或椅子并拿放布和袋子；女士触碰手机、拿布并放下玩具。 | 是，26个采样点 |
| S4 | [105,150) | C反复拿放纸张、容器和布，打开小册子，查看婴儿，并与男子交谈；其他人在桌边拿放物品。 | 否 |
| S5 | [150,180) | C持书、读说明，照看婴儿并拿放布和物品；取出饼干、拿食物；一位女士与C交谈，并触碰书、读纸张。 | 是，30个采样点 |

选择17–33、79–104、150–179秒，共73个采样点。[全部逐帧原文、实际解构、候选和最终段落](18_per_frame_before_after.md)。

## 关键帧逐条处理

### 18秒

#O The woman X pushes a chair aside with her left hand.

实际结构化字段：`{"actions": [{"actor": "X", "verb": "pushes", "patient": "chair"}]}`。

- R2：`The woman X pushes a chair aside with her left hand.`；来源 description；跳过。
- R3：`X pushes chair.`；来源 H fact / actions；跳过。

最终没有对应证据进入回答输入。

### 31秒

#O woman X picks the cloth

实际结构化字段：`{"actions": [{"actor": "woman X", "verb": "picks", "patient": "cloth"}]}`。

- R26：`woman X picks the cloth.`；来源 description；跳过。
- R27：`woman X picks cloth.`；来源 H fact / actions；跳过。

最终没有对应证据进入回答输入。

### 165秒

#C C removes the biscuit from the pack with both hands.

实际结构化字段：`{"actions": [{"actor": "C", "verb": "removes", "patient": "biscuit", "body_part": "both hands"}]}`。

- R95：`C removes the biscuit from the pack with both hands.`；来源 description；保留。
- R96：`C removes biscuit with both hands.`；来源 H fact / actions；保留。

新格式对应段落：

```text
Frames [165,166): C removes the biscuit from the pack with both hands.
Frames [165,166): C removes biscuit with both hands.
```

### 166秒

#C C takes food

实际结构化字段：`{"actions": [{"actor": "C", "verb": "takes", "patient": "food"}]}`。

- R97：`C takes food.`；来源 description, H fact / actions；保留。

新格式对应段落：

```text
Frames [166,167): C takes food.
```

### 167秒

#C C reads the instructions 

实际结构化字段：`{"actions": [{"actor": "C", "verb": "reads", "patient": "instructions"}]}`。

- R98：`C reads the instructions.`；来源 description；保留。
- R99：`C reads instructions.`；来源 H fact / actions；保留。

新格式对应段落：

```text
Frames [167,168): C reads the instructions.
Frames [167,168): C reads instructions.
```

### 168秒

#O A woman X talks to C

实际结构化字段：`{"attributes": [{"entity": "X", "property": "color", "value": "woman"}]}`。

- R100：`A woman X talks to C.`；来源 description；保留。
- R101：`The color of X is woman.`；来源 H fact / attributes；保留。

新格式对应段落：

```text
Frames [168,169): A woman X talks to C.
Frames [168,169): The color of X is woman.
```

## 完整筛选和最终输入

共122条候选，保留93条、跳过29条，不确定0条、合并组0组。被跳过的29条全部来自18–33秒；32/33秒仍有上游时序关联候选保留，所以不能把跳过直接候选说成删除了该时刻所有信息。规划器仅输出逐ID枚举决定，没有逐条自然语言理由。

[全部候选及来源](06_candidate_pool.json) · [实际规划输入](07_actual_planner_input.json) · [原始规划输出](07_raw_planner_output.json) · [实际决策分区](08_plan.json)

[原始完整Description](11_actual_description.txt) · [原始完整system/user聊天模板](16_actual_chat_template.txt) · [新格式完整Description](14_reformatted_description.txt) · [新格式完整聊天模板](17_reformatted_chat_template.txt)

新格式仅将At sampled intervals改成Frames、移除默认来源标签、保留相邻区间合并及推断标签；新格式没有重新生成答案，不能称作旧调用实际读取的文本。原始输入经真实本地tokenizer复核为2479token，与保存调用一致。

## 实际回答与可确认的问题

| 本题组别 | 实际预测 | 标准答案 | 本题输入token |
|---|---|---|---|
| I对照 | B | B | 2168 |
| L全保留 | B | B | 3093 |
| L规划 | D | B | 2479 |

R100的“A woman X talks to C.”明确保留，因此不能归因为删掉了与女士交谈的关键句。R95/R96的拆饼干包装、R97的拿食物也保留，但这些文字不直接等同于吃东西。R101的“The color of X is woman.”是实际存档中的错误属性抽取，规划后也保留。

可确认：这个同题对照中，筛选后的输入令原本答对的结果变错；有更直接支持B的原文仍在，却最终选择D。可能与剩余上下文、重复、噪声以及选项比较有关，但该调用没有生成解释，无法从单例确认模型内部的具体原因。不能声称多检索一定修复，也不能把新显示格式视为已验证的修复。

