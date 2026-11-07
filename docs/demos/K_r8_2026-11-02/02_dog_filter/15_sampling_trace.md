# 从采样点到原文、F解构与K取舍

来源题目：`15bae307-992b-4359-9fe0-3f622f957d42`。只读既有记录，没有重跑模型。索引为1秒采样点，不能当成原视频按原生帧率编号的第N帧。

## 1. 上游实际选中的采样点

K继承上游已选证据，没有重新看图或进行图像选帧。全部索引如下：

```text
0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149, 150, 151, 152, 153, 154, 155, 156, 157, 158, 159, 160, 161, 162, 163, 164, 165, 166, 167, 168, 169, 170, 171, 172, 173, 174, 175, 176, 177, 178, 179
```

## 2. 每个采样点的原始文本与F实际解构

字段列来自已经保存并接受的事实；“仅原文”表示该行没有接受的结构化字段，不臆造actor/verb/tool。它不自动等于从未请求或模型从未尝试抽取。

| 采样索引 | 区间 | 原始caption | F已接受结构化字段 | K直接候选取舍 | 上游时序候选关联 |
|---:|---|---|---|---|---|
| 0 | [0, 1] | #C C walks with the dog | 仅原文，无已接受结构化字段 | R0=keep | — |
| 1 | [1, 2] | #C C walks with the dog | 仅原文，无已接受结构化字段 | R0=keep | — |
| 2 | [2, 3] | #C C moves a hand | 仅原文，无已接受结构化字段 | R1=skip | — |
| 3 | [3, 4] | #C C walks dog along the road | {"actions": [{"actor": "C", "verb": "walks", "patient": "dog"}]} | R2=skip; R3=keep | — |
| 4 | [4, 5] | #C C walks dog on the road | {"actions": [{"actor": "C", "verb": "walks", "patient": "dog"}]} | R3=keep; R4=skip | R53=keep; R54=keep |
| 5 | [5, 6] | #C C walks on a pathway | 仅原文，无已接受结构化字段 | R5=skip | R53=keep |
| 6 | [6, 7] | #C C pulls a dog leash  | {"actions": [{"actor": "C", "verb": "pulls", "patient": "dog leash"}]} | R6=skip; R7=keep | — |
| 7 | [7, 8] | #C C moves hand | 仅原文，无已接受结构化字段 | R8=skip | — |
| 8 | [8, 9] | #C C scratches face | 仅原文，无已接受结构化字段 | R9=skip | — |
| 9 | [9, 10] | #C C scratches face | 仅原文，无已接受结构化字段 | R9=skip | — |
| 10 | [10, 11] | #C C moves the dog leash to the right hand  | {"actions": [{"actor": "C", "verb": "moves", "patient": "dog leash"}]} | R10=skip; R11=skip | — |
| 11 | [11, 12] | #C C holds the bag | {"actions": [{"actor": "C", "verb": "holds", "patient": "bag"}]} | R12=skip; R13=skip | — |
| 12 | [12, 13] | #C C holds the dog leash | {"actions": [{"actor": "C", "verb": "holds", "patient": "dog leash"}]} | R14=skip; R15=keep | — |
| 13 | [13, 14] | #C C puts the carrier bag in the bag | 仅原文，无已接受结构化字段 | R16=skip | — |
| 14 | [14, 15] | #C C walks with the dog | 仅原文，无已接受结构化字段 | R0=keep | R55=skip |
| 15 | [15, 16] | #C C walks with dog on the road | 仅原文，无已接受结构化字段 | R17=skip | R53=keep; R54=keep |
| 16 | [16, 17] | #C C walks with the dog | 仅原文，无已接受结构化字段 | R0=keep | R53=keep; R54=keep |
| 17 | [17, 18] | #C C walks dog | {"actions": [{"actor": "C", "verb": "walks", "patient": "dog"}]} | R3=keep | — |
| 18 | [18, 19] | #C C walks dog with a leash | {"actions": [{"actor": "C", "verb": "walks", "patient": "dog"}]} | R3=keep; R18=skip | — |
| 19 | [19, 20] | #C C walks around | 仅原文，无已接受结构化字段 | R19=skip | — |
| 20 | [20, 21] | #C C walks around | 仅原文，无已接受结构化字段 | R19=skip | — |
| 21 | [21, 22] | #C C moves the dog leash | {"actions": [{"actor": "C", "verb": "moves", "patient": "dog leash"}]} | R11=skip; R20=skip | — |
| 22 | [22, 23] | #C C moves the dog leash in her left hand to her right hand. | {"actions": [{"actor": "C", "verb": "moves", "patient": "dog leash"}]} | R11=skip; R21=skip | — |
| 23 | [23, 24] | #C C walks with a dog | 仅原文，无已接受结构化字段 | R22=skip | — |
| 24 | [24, 25] | #C C walks with dog  | 仅原文，无已接受结构化字段 | R23=skip | — |
| 25 | [25, 26] | #C C walks around the road | 仅原文，无已接受结构化字段 | R24=skip | — |
| 26 | [26, 27] | #C C lifts up his hand | 仅原文，无已接受结构化字段 | R25=skip | — |
| 27 | [27, 28] | #C C moves hand | 仅原文，无已接受结构化字段 | R8=skip | — |
| 28 | [28, 29] | #C C pulls the leash | {"actions": [{"actor": "C", "verb": "pulls", "patient": "leash"}]} | R26=skip; R27=skip | — |
| 29 | [29, 30] | #C C lifts the hand | 仅原文，无已接受结构化字段 | R28=skip | — |
| 30 | [30, 31] | #C C moves the hand | 仅原文，无已接受结构化字段 | R29=skip | — |
| 31 | [31, 32] | #C C walks with the dog | 仅原文，无已接受结构化字段 | R0=keep | — |
| 32 | [32, 33] | #C C walks around the road | 仅原文，无已接受结构化字段 | R24=skip | — |
| 33 | [33, 34] | #C C lifts the hand | 仅原文，无已接受结构化字段 | R28=skip | — |
| 34 | [34, 35] | #C C lifts the hand | 仅原文，无已接受结构化字段 | R28=skip | — |
| 35 | [35, 36] | #C C walks around the road | 仅原文，无已接受结构化字段 | R24=skip | — |
| 36 | [36, 37] | #C C pulls the dog leash | {"actions": [{"actor": "C", "verb": "pulls", "patient": "dog leash"}]} | R7=keep; R30=skip | — |
| 37 | [37, 38] | #C C touches the face | 仅原文，无已接受结构化字段 | R31=skip | — |
| 38 | [38, 39] | #C C walks with the dog | 仅原文，无已接受结构化字段 | R0=keep | — |
| 39 | [39, 40] | #C C holds a dog leash | {"actions": [{"actor": "C", "verb": "holds", "patient": "dog leash"}]} | R15=keep; R32=skip | — |
| 40 | [40, 41] | #C C walks with the dog | 仅原文，无已接受结构化字段 | R0=keep | — |
| 41 | [41, 42] | #C C moves the dog leash | {"actions": [{"actor": "C", "verb": "moves", "patient": "dog leash"}]} | R11=skip; R20=skip | — |
| 42 | [42, 43] | #C C holds the dog leash | {"actions": [{"actor": "C", "verb": "holds", "patient": "dog leash"}]} | R14=skip; R15=keep | — |
| 43 | [43, 44] | #C C walks with the dog | 仅原文，无已接受结构化字段 | R0=keep | — |
| 44 | [44, 45] | #C C walks with the dog | 仅原文，无已接受结构化字段 | R0=keep | — |
| 135 | [135, 136] | #C C walks on the road | 仅原文，无已接受结构化字段 | R33=skip | — |
| 136 | [136, 137] | #C C holds dog leash | {"actions": [{"actor": "C", "verb": "holds", "patient": "dog leash"}]} | R15=keep | — |
| 137 | [137, 138] | #C C stands on the road | 仅原文，无已接受结构化字段 | R34=skip | — |
| 138 | [138, 139] | #C C looks around the road  | 仅原文，无已接受结构化字段 | R35=skip | — |
| 139 | [139, 140] | #C C walks dog on the road  | {"actions": [{"actor": "C", "verb": "walks", "patient": "dog"}]} | R3=keep; R4=skip | — |
| 140 | [140, 141] | #C C pulls dog leash | {"actions": [{"actor": "C", "verb": "pulls", "patient": "dog leash"}]} | R7=keep | — |
| 141 | [141, 142] | #C C looks at dog  | 仅原文，无已接受结构化字段 | R36=skip | — |
| 142 | [142, 143] | #C C pulls a dog leash  | {"actions": [{"actor": "C", "verb": "pulls", "patient": "dog leash"}]} | R6=skip; R7=keep | — |
| 143 | [143, 144] | #C C stands on the road | 仅原文，无已接受结构化字段 | R34=skip | — |
| 144 | [144, 145] | #C C walks on the road with a dog  | {"actions": [{"actor": "C", "verb": "walks", "body_part": "dog"}]} | R23=skip; R37=skip | — |
| 145 | [145, 146] | #C C walks with the dog | 仅原文，无已接受结构化字段 | R0=keep | — |
| 146 | [146, 147] | #C C holds a leash | {"actions": [{"actor": "C", "verb": "holds", "patient": "leash"}]} | R38=skip; R39=skip | — |
| 147 | [147, 148] | #C C stops walking | 仅原文，无已接受结构化字段 | R40=skip | — |
| 148 | [148, 149] | #C C walks the dog along the road | {"actions": [{"actor": "C", "verb": "walks", "patient": "dog"}]} | R3=keep; R41=skip | — |
| 149 | [149, 150] | #C C stops walking | 仅原文，无已接受结构化字段 | R40=skip | — |
| 150 | [150, 151] | #C C stops  | 仅原文，无已接受结构化字段 | R42=skip | — |
| 151 | [151, 152] | #C C pulls dog leash | {"actions": [{"actor": "C", "verb": "pulls", "patient": "dog leash"}]} | R7=keep | — |
| 152 | [152, 153] | #C C walks with a dog | 仅原文，无已接受结构化字段 | R22=skip | — |
| 153 | [153, 154] | #C C walks on the road | 仅原文，无已接受结构化字段 | R33=skip | — |
| 154 | [154, 155] | #C C pulls dog leash | {"actions": [{"actor": "C", "verb": "pulls", "patient": "dog leash"}]} | R7=keep | — |
| 155 | [155, 156] | #C C stops walking | 仅原文，无已接受结构化字段 | R40=skip | — |
| 156 | [156, 157] | #C C walks the dogs | {"actions": [{"actor": "C", "verb": "walks", "patient": "dogs"}]} | R43=skip; R44=skip | — |
| 157 | [157, 158] | #C C walks dog on a road | {"actions": [{"actor": "C", "verb": "walks", "patient": "dog"}]} | R3=keep; R45=skip | — |
| 158 | [158, 159] | #C C walks around | 仅原文，无已接受结构化字段 | R19=skip | — |
| 159 | [159, 160] | #C C holds the leash with both hands | {"actions": [{"actor": "C", "verb": "holds", "patient": "leash"}]} | R39=skip; R46=skip | — |
| 160 | [160, 161] | #C C walks with dog on road | 仅原文，无已接受结构化字段 | R47=skip | — |
| 161 | [161, 162] | #C C stops walking | 仅原文，无已接受结构化字段 | R40=skip | — |
| 162 | [162, 163] | #C C walks around | 仅原文，无已接受结构化字段 | R19=skip | — |
| 163 | [163, 164] | #C C walks around the road | 仅原文，无已接受结构化字段 | R24=skip | — |
| 164 | [164, 165] | #C C walks around the road with the dog | 仅原文，无已接受结构化字段 | R48=skip | — |
| 165 | [165, 166] | #C C walks with dog on the road | 仅原文，无已接受结构化字段 | R17=skip | — |
| 166 | [166, 167] | #C C walks with dog on the road | 仅原文，无已接受结构化字段 | R17=skip | — |
| 167 | [167, 168] | #C C walks with the dog | 仅原文，无已接受结构化字段 | R0=keep | — |
| 168 | [168, 169] | #C C walks around | 仅原文，无已接受结构化字段 | R19=skip | — |
| 169 | [169, 170] | #C C holds dog leash | {"actions": [{"actor": "C", "verb": "holds", "patient": "dog leash"}]} | R15=keep | — |
| 170 | [170, 171] | #C C walks with dog | 仅原文，无已接受结构化字段 | R23=skip | — |
| 171 | [171, 172] | #C C pulls the leash | {"actions": [{"actor": "C", "verb": "pulls", "patient": "leash"}]} | R26=skip; R27=skip | — |
| 172 | [172, 173] | #C C walks with the dog | 仅原文，无已接受结构化字段 | R0=keep | — |
| 173 | [173, 174] | #C C walks the dog | {"actions": [{"actor": "C", "verb": "walks", "patient": "dog"}]} | R3=keep; R49=skip | — |
| 174 | [174, 175] | #C C walks dog along the road | {"actions": [{"actor": "C", "verb": "walks", "patient": "dog"}]} | R2=skip; R3=keep | — |
| 175 | [175, 176] | #C C pulls the dog leash with his left hand | {"actions": [{"actor": "C", "verb": "pulls", "patient": "dog leash"}]} | R7=keep; R50=skip | — |
| 176 | [176, 177] | #C C pulls dog leash | {"actions": [{"actor": "C", "verb": "pulls", "patient": "dog leash"}]} | R7=keep | — |
| 177 | [177, 178] | #C C holds a dog leash  | {"actions": [{"actor": "C", "verb": "holds", "patient": "dog leash"}]} | R15=keep; R32=skip | — |
| 178 | [178, 179] | #C C looks around | 仅原文，无已接受结构化字段 | R51=skip | — |
| 179 | [179, 180] | #C C stares around | 仅原文，无已接受结构化字段 | R52=skip | — |

## 3. K保留证据最终引用到的采样点

```text
0, 1, 3, 4, 5, 6, 12, 14, 15, 16, 17, 18, 31, 36, 38, 39, 40, 42, 43, 44, 136, 139, 140, 142, 145, 148, 151, 154, 157, 167, 169, 172, 173, 174, 175, 176, 177
```

这些是保留条目的来源引用（含上游时序和身份假设）；不能理解为完整保留了这些采样点的全部原始caption。每条映射及全文见[追踪JSON](16_sampling_trace.json)。

