# 从采样点到原文、F解构与K取舍

来源题目：`3e700f0b-b4a9-4000-92b7-f0fa3be55161`。只读既有记录，没有重跑模型。索引为1秒采样点，不能当成原视频按原生帧率编号的第N帧。

## 1. 上游实际选中的采样点

K继承上游已选证据，没有重新看图或进行图像选帧。全部索引如下：

```text
45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69
```

## 2. 每个采样点的原始文本与F实际解构

字段列来自已经保存并接受的事实；“仅原文”表示该行没有接受的结构化字段，不臆造actor/verb/tool。它不自动等于从未请求或模型从未尝试抽取。

| 采样索引 | 区间 | 原始caption | F已接受结构化字段 | K直接候选取舍 | 上游时序候选关联 |
|---:|---|---|---|---|---|
| 45 | [45, 46] | #C C looks at man P | 仅原文，无已接受结构化字段 | R0=skip | — |
| 46 | [46, 47] | #O a woman X walks towards the table | {"actions": [{"actor": "X", "verb": "walks", "patient": "towards the table"}]} | R1=skip; R2=skip | — |
| 47 | [47, 48] | #C C walks towards the bedroom | 仅原文，无已接受结构化字段 | R3=skip | — |
| 48 | [48, 49] | #O A lady Y holds a phone | {"actions": [{"actor": "Y", "verb": "holds", "patient": "phone"}]} | R4=skip; R5=skip | — |
| 49 | [49, 50] | #C C looks at the table | 仅原文，无已接受结构化字段 | R6=skip | — |
| 50 | [50, 51] | #O A woman X sits on the chair | 仅原文，无已接受结构化字段 | R7=skip | — |
| 51 | [51, 52] | #C C looks at the table | 仅原文，无已接受结构化字段 | R6=skip | — |
| 52 | [52, 53] | #O lady X walks around | 仅原文，无已接受结构化字段 | R8=skip | — |
| 53 | [53, 54] | #O a woman Y walks around the room  | 仅原文，无已接受结构化字段 | R9=skip | — |
| 54 | [54, 55] | #O person D walks around | 仅原文，无已接受结构化字段 | R10=skip | — |
| 55 | [55, 56] | #O A woman X holds a phone | {"actions": [{"actor": "X", "verb": "holds", "patient": "phone"}]} | R11=skip; R12=skip | — |
| 56 | [56, 57] | #O woman K walks in the room  | 仅原文，无已接受结构化字段 | R13=skip | — |
| 57 | [57, 58] | #C C looks at the table | 仅原文，无已接受结构化字段 | R6=skip | — |
| 58 | [58, 59] | #C C walks in the room | 仅原文，无已接受结构化字段 | R14=skip | — |
| 59 | [59, 60] | #C C walks around the house | 仅原文，无已接受结构化字段 | R15=skip | — |
| 60 | [60, 61] | #O woman X walks back to the room | 仅原文，无已接受结构化字段 | R16=skip | — |
| 61 | [61, 62] | #O a woman X walks towards C | {"actions": [{"actor": "X", "verb": "walks", "patient": "towards C"}]} | R17=skip; R18=skip | R28=skip |
| 62 | [62, 63] | #O woman Y stands at the table  | 仅原文，无已接受结构化字段 | R19=skip | R29=skip |
| 63 | [63, 64] | #C C looks at the coffee maker | 仅原文，无已接受结构化字段 | R20=skip | — |
| 64 | [64, 65] | #O A woman Y walks towards C | {"actions": [{"actor": "Y", "verb": "walks", "patient": "towards C"}]} | R21=skip; R22=skip | R30=group:R30 |
| 65 | [65, 66] | #O A woman K walks in the room | 仅原文，无已接受结构化字段 | R23=skip | — |
| 66 | [66, 67] | #O A woman Z walks towards the woman X. | 仅原文，无已接受结构化字段 | R24=skip | — |
| 67 | [67, 68] | #C C looks at the phone  | 仅原文，无已接受结构化字段 | R25=skip | — |
| 68 | [68, 69] | #C C looks at woman X | 仅原文，无已接受结构化字段 | R26=skip | — |
| 69 | [69, 70] | #O woman P walks away from the table | 仅原文，无已接受结构化字段 | R27=skip | — |

## 3. K保留证据最终引用到的采样点

```text
64
```

这些是保留条目的来源引用（含上游时序和身份假设）；不能理解为完整保留了这些采样点的全部原始caption。每条映射及全文见[追踪JSON](16_sampling_trace.json)。

