# 按每条原始采样文本查看处理前后

每一行对应上游选中的一个一秒采样点。按时间顺序列出所有已选原文，不只展示K保留的行。

**读法：** 原文 → F已接受的解构字段（原文回退另存） → K的直接及上游时序候选取舍 → 最终输入里的实际完整段落。

**共享段落说明：** 同一候选可以引用多个采样点，因此下表可能在多行重复展示同一段，方便逐帧追踪；模型实际只接收[完整输入](12_actual_chat_template.txt)中的一份。时序关联也不等于本行原文支持该段的所有细节。

## 按帧总表

| 采样点／区间 | 原始文本 | F已接受解构字段 | K取舍 | 最终输入的实际文本（含关联时序段落） |
|---|---|---|---|---|
| 45 / [45, 46] | #C C looks at man P | 没有已接受结构化字段；原文回退 | 直接：R0=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 46 / [46, 47] | #O a woman X walks towards the table | {"actions": [{"actor": "X", "verb": "walks", "patient": "towards the table"}]} | 直接：R1=skip; R2=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 47 / [47, 48] | #C C walks towards the bedroom | 没有已接受结构化字段；原文回退 | 直接：R3=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 48 / [48, 49] | #O A lady Y holds a phone | {"actions": [{"actor": "Y", "verb": "holds", "patient": "phone"}]} | 直接：R4=skip; R5=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 49 / [49, 50] | #C C looks at the table | 没有已接受结构化字段；原文回退 | 直接：R6=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 50 / [50, 51] | #O A woman X sits on the chair | 没有已接受结构化字段；原文回退 | 直接：R7=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 51 / [51, 52] | #C C looks at the table | 没有已接受结构化字段；原文回退 | 直接：R6=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 52 / [52, 53] | #O lady X walks around | 没有已接受结构化字段；原文回退 | 直接：R8=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 53 / [53, 54] | #O a woman Y walks around the room  | 没有已接受结构化字段；原文回退 | 直接：R9=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 54 / [54, 55] | #O person D walks around | 没有已接受结构化字段；原文回退 | 直接：R10=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 55 / [55, 56] | #O A woman X holds a phone | {"actions": [{"actor": "X", "verb": "holds", "patient": "phone"}]} | 直接：R11=skip; R12=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 56 / [56, 57] | #O woman K walks in the room  | 没有已接受结构化字段；原文回退 | 直接：R13=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 57 / [57, 58] | #C C looks at the table | 没有已接受结构化字段；原文回退 | 直接：R6=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 58 / [58, 59] | #C C walks in the room | 没有已接受结构化字段；原文回退 | 直接：R14=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 59 / [59, 60] | #C C walks around the house | 没有已接受结构化字段；原文回退 | 直接：R15=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 60 / [60, 61] | #O woman X walks back to the room | 没有已接受结构化字段；原文回退 | 直接：R16=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 61 / [61, 62] | #O a woman X walks towards C | {"actions": [{"actor": "X", "verb": "walks", "patient": "towards C"}]} | 直接：R17=skip; R18=skip<br>时序：R28=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 62 / [62, 63] | #O woman Y stands at the table  | 没有已接受结构化字段；原文回退 | 直接：R19=skip<br>时序：R29=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 63 / [63, 64] | #C C looks at the coffee maker | 没有已接受结构化字段；原文回退 | 直接：R20=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 64 / [64, 65] | #O A woman Y walks towards C | {"actions": [{"actor": "Y", "verb": "walks", "patient": "towards C"}]} | 直接：R21=skip; R22=skip<br>时序：R30=group:R30 | At sampled intervals [64,65) (unresolved hypothesis), An unresolved source observation states: "A woman Y walks towards C". |
| 65 / [65, 66] | #O A woman K walks in the room | 没有已接受结构化字段；原文回退 | 直接：R23=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 66 / [66, 67] | #O A woman Z walks towards the woman X. | 没有已接受结构化字段；原文回退 | 直接：R24=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 67 / [67, 68] | #C C looks at the phone  | 没有已接受结构化字段；原文回退 | 直接：R25=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 68 / [68, 69] | #C C looks at woman X | 没有已接受结构化字段；原文回退 | 直接：R26=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 69 / [69, 70] | #O woman P walks away from the table | 没有已接受结构化字段；原文回退 | 直接：R27=skip | ∅（本行没有直接证据或时序关联进入最终输入） |

## 逐条展开

下方保留F完整档案，区分“已接受字段”和“保留原文”；最终段落逐字复制，不人工改写。


### 采样点 45，区间 [45, 46]

**原始文本**

```text
#C C looks at man P
```

**F实际保存**

```json
{
  "original_text": "#C C looks at man P"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R0：`skip`；C looks at man P.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 46，区间 [46, 47]

**原始文本**

```text
#O a woman X walks towards the table
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "X",
      "verb": "walks",
      "patient": "towards the table"
    }
  ],
  "original_text": "#O a woman X walks towards the table"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R1：`skip`；a woman X walks towards the table.
- R2：`skip`；X walks towards the table.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 47，区间 [47, 48]

**原始文本**

```text
#C C walks towards the bedroom
```

**F实际保存**

```json
{
  "original_text": "#C C walks towards the bedroom"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R3：`skip`；C walks towards the bedroom.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 48，区间 [48, 49]

**原始文本**

```text
#O A lady Y holds a phone
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "Y",
      "verb": "holds",
      "patient": "phone"
    }
  ],
  "original_text": "#O A lady Y holds a phone"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R4：`skip`；A lady Y holds a phone.
- R5：`skip`；Y holds phone.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 49，区间 [49, 50]

**原始文本**

```text
#C C looks at the table
```

**F实际保存**

```json
{
  "original_text": "#C C looks at the table"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R6：`skip`；C looks at the table.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 50，区间 [50, 51]

**原始文本**

```text
#O A woman X sits on the chair
```

**F实际保存**

```json
{
  "original_text": "#O A woman X sits on the chair"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R7：`skip`；A woman X sits on the chair.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 51，区间 [51, 52]

**原始文本**

```text
#C C looks at the table
```

**F实际保存**

```json
{
  "original_text": "#C C looks at the table"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R6：`skip`；C looks at the table.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 52，区间 [52, 53]

**原始文本**

```text
#O lady X walks around
```

**F实际保存**

```json
{
  "original_text": "#O lady X walks around"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R8：`skip`；lady X walks around.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 53，区间 [53, 54]

**原始文本**

```text
#O a woman Y walks around the room 
```

**F实际保存**

```json
{
  "original_text": "#O a woman Y walks around the room "
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R9：`skip`；a woman Y walks around the room.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 54，区间 [54, 55]

**原始文本**

```text
#O person D walks around
```

**F实际保存**

```json
{
  "original_text": "#O person D walks around"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R10：`skip`；person D walks around.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 55，区间 [55, 56]

**原始文本**

```text
#O A woman X holds a phone
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "X",
      "verb": "holds",
      "patient": "phone"
    }
  ],
  "original_text": "#O A woman X holds a phone"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R11：`skip`；A woman X holds a phone.
- R12：`skip`；X holds phone.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 56，区间 [56, 57]

**原始文本**

```text
#O woman K walks in the room 
```

**F实际保存**

```json
{
  "original_text": "#O woman K walks in the room "
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R13：`skip`；woman K walks in the room.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 57，区间 [57, 58]

**原始文本**

```text
#C C looks at the table
```

**F实际保存**

```json
{
  "original_text": "#C C looks at the table"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R6：`skip`；C looks at the table.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 58，区间 [58, 59]

**原始文本**

```text
#C C walks in the room
```

**F实际保存**

```json
{
  "original_text": "#C C walks in the room"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R14：`skip`；C walks in the room.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 59，区间 [59, 60]

**原始文本**

```text
#C C walks around the house
```

**F实际保存**

```json
{
  "original_text": "#C C walks around the house"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R15：`skip`；C walks around the house.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 60，区间 [60, 61]

**原始文本**

```text
#O woman X walks back to the room
```

**F实际保存**

```json
{
  "original_text": "#O woman X walks back to the room"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R16：`skip`；woman X walks back to the room.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 61，区间 [61, 62]

**原始文本**

```text
#O a woman X walks towards C
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "X",
      "verb": "walks",
      "patient": "towards C"
    }
  ],
  "original_text": "#O a woman X walks towards C"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R17：`skip`；a woman X walks towards C.
- R18：`skip`；X walks towards C.

上游时序关联候选：

- R28：`skip`；a woman X walks towards C. The text memory associates this observation with C (text reference E0).

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 62，区间 [62, 63]

**原始文本**

```text
#O woman Y stands at the table 
```

**F实际保存**

```json
{
  "original_text": "#O woman Y stands at the table "
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R19：`skip`；woman Y stands at the table.

上游时序关联候选：

- R29：`skip`；woman Y stands at the table. The text memory associates this observation with the table (text reference E1).

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 63，区间 [63, 64]

**原始文本**

```text
#C C looks at the coffee maker
```

**F实际保存**

```json
{
  "original_text": "#C C looks at the coffee maker"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R20：`skip`；C looks at the coffee maker.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 64，区间 [64, 65]

**原始文本**

```text
#O A woman Y walks towards C
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "Y",
      "verb": "walks",
      "patient": "towards C"
    }
  ],
  "original_text": "#O A woman Y walks towards C"
}
```

**K处理状态：** 直接候选跳过，仅保留上游时序关联。


直接候选：

- R21：`skip`；A woman Y walks towards C.
- R22：`skip`；Y walks towards C.

上游时序关联候选：

- R30：`group:R30`；An unresolved source observation states: "A woman Y walks towards C".

**处理到最后的实际文本**

来自引用本行的上游时序条目；对应候选 R30：

```text
At sampled intervals [64,65) (unresolved hypothesis), An unresolved source observation states: "A woman Y walks towards C".
```


### 采样点 65，区间 [65, 66]

**原始文本**

```text
#O A woman K walks in the room
```

**F实际保存**

```json
{
  "original_text": "#O A woman K walks in the room"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R23：`skip`；A woman K walks in the room.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 66，区间 [66, 67]

**原始文本**

```text
#O A woman Z walks towards the woman X.
```

**F实际保存**

```json
{
  "original_text": "#O A woman Z walks towards the woman X."
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R24：`skip`；A woman Z walks towards the woman X.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 67，区间 [67, 68]

**原始文本**

```text
#C C looks at the phone 
```

**F实际保存**

```json
{
  "original_text": "#C C looks at the phone "
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R25：`skip`；C looks at the phone.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 68，区间 [68, 69]

**原始文本**

```text
#C C looks at woman X
```

**F实际保存**

```json
{
  "original_text": "#C C looks at woman X"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R26：`skip`；C looks at woman X.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 69，区间 [69, 70]

**原始文本**

```text
#O woman P walks away from the table
```

**F实际保存**

```json
{
  "original_text": "#O woman P walks away from the table"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R27：`skip`；woman P walks away from the table.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。
