# 按每条原始采样文本查看处理前后

每一行对应上游选中的一个一秒采样点。按时间顺序列出所有已选原文，不只展示K保留的行。

**读法：** 原文 → F已接受的解构字段（原文回退另存） → K的直接及上游时序候选取舍 → 最终输入里的实际完整段落。

**共享段落说明：** 同一候选可以引用多个采样点，因此下表可能在多行重复展示同一段，方便逐帧追踪；模型实际只接收[完整输入](12_actual_chat_template.txt)中的一份。时序关联也不等于本行原文支持该段的所有细节。

## 按帧总表

| 采样点／区间 | 原始文本 | F已接受解构字段 | K取舍 | 最终输入的实际文本（含关联时序段落） |
|---|---|---|---|---|
| 0 / [0, 1] | #C C walks with the dog | 没有已接受结构化字段；原文回退 | 直接：R0=keep | At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog. |
| 1 / [1, 2] | #C C walks with the dog | 没有已接受结构化字段；原文回退 | 直接：R0=keep | At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog. |
| 2 / [2, 3] | #C C moves a hand | 没有已接受结构化字段；原文回退 | 直接：R1=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 3 / [3, 4] | #C C walks dog along the road | {"actions": [{"actor": "C", "verb": "walks", "patient": "dog"}]} | 直接：R2=skip; R3=keep | At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog. |
| 4 / [4, 5] | #C C walks dog on the road | {"actions": [{"actor": "C", "verb": "walks", "patient": "dog"}]} | 直接：R3=keep; R4=skip<br>时序：R53=keep; R54=keep | At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog.<br><br>At sampled intervals [4,5), [5,6), [15,16), [16,17) (source assertion), C walks dog on the road. The text memory associates this observation with C (text reference E0).<br><br>At sampled intervals [4,5), [15,16), [16,17) (source assertion), C walks dog on the road. The text memory associates this observation with dog (text reference E1). |
| 5 / [5, 6] | #C C walks on a pathway | 没有已接受结构化字段；原文回退 | 直接：R5=skip<br>时序：R53=keep | At sampled intervals [4,5), [5,6), [15,16), [16,17) (source assertion), C walks dog on the road. The text memory associates this observation with C (text reference E0). |
| 6 / [6, 7] | #C C pulls a dog leash  | {"actions": [{"actor": "C", "verb": "pulls", "patient": "dog leash"}]} | 直接：R6=skip; R7=keep | At sampled intervals [6,7), [36,37), [140,141), [142,143), [151,152), [154,155), [175,176), [176,177) (source assertion), C pulls dog leash. |
| 7 / [7, 8] | #C C moves hand | 没有已接受结构化字段；原文回退 | 直接：R8=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 8 / [8, 9] | #C C scratches face | 没有已接受结构化字段；原文回退 | 直接：R9=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 9 / [9, 10] | #C C scratches face | 没有已接受结构化字段；原文回退 | 直接：R9=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 10 / [10, 11] | #C C moves the dog leash to the right hand  | {"actions": [{"actor": "C", "verb": "moves", "patient": "dog leash"}]} | 直接：R10=skip; R11=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 11 / [11, 12] | #C C holds the bag | {"actions": [{"actor": "C", "verb": "holds", "patient": "bag"}]} | 直接：R12=skip; R13=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 12 / [12, 13] | #C C holds the dog leash | {"actions": [{"actor": "C", "verb": "holds", "patient": "dog leash"}]} | 直接：R14=skip; R15=keep | At sampled intervals [12,13), [39,40), [42,43), [136,137), [169,170), [177,178) (source assertion), C holds dog leash. |
| 13 / [13, 14] | #C C puts the carrier bag in the bag | 没有已接受结构化字段；原文回退 | 直接：R16=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 14 / [14, 15] | #C C walks with the dog | 没有已接受结构化字段；原文回退 | 直接：R0=keep<br>时序：R55=skip | At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog. |
| 15 / [15, 16] | #C C walks with dog on the road | 没有已接受结构化字段；原文回退 | 直接：R17=skip<br>时序：R53=keep; R54=keep | At sampled intervals [4,5), [5,6), [15,16), [16,17) (source assertion), C walks dog on the road. The text memory associates this observation with C (text reference E0).<br><br>At sampled intervals [4,5), [15,16), [16,17) (source assertion), C walks dog on the road. The text memory associates this observation with dog (text reference E1). |
| 16 / [16, 17] | #C C walks with the dog | 没有已接受结构化字段；原文回退 | 直接：R0=keep<br>时序：R53=keep; R54=keep | At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog.<br><br>At sampled intervals [4,5), [5,6), [15,16), [16,17) (source assertion), C walks dog on the road. The text memory associates this observation with C (text reference E0).<br><br>At sampled intervals [4,5), [15,16), [16,17) (source assertion), C walks dog on the road. The text memory associates this observation with dog (text reference E1). |
| 17 / [17, 18] | #C C walks dog | {"actions": [{"actor": "C", "verb": "walks", "patient": "dog"}]} | 直接：R3=keep | At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog. |
| 18 / [18, 19] | #C C walks dog with a leash | {"actions": [{"actor": "C", "verb": "walks", "patient": "dog"}]} | 直接：R3=keep; R18=skip | At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog. |
| 19 / [19, 20] | #C C walks around | 没有已接受结构化字段；原文回退 | 直接：R19=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 20 / [20, 21] | #C C walks around | 没有已接受结构化字段；原文回退 | 直接：R19=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 21 / [21, 22] | #C C moves the dog leash | {"actions": [{"actor": "C", "verb": "moves", "patient": "dog leash"}]} | 直接：R11=skip; R20=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 22 / [22, 23] | #C C moves the dog leash in her left hand to her right hand. | {"actions": [{"actor": "C", "verb": "moves", "patient": "dog leash"}]} | 直接：R11=skip; R21=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 23 / [23, 24] | #C C walks with a dog | 没有已接受结构化字段；原文回退 | 直接：R22=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 24 / [24, 25] | #C C walks with dog  | 没有已接受结构化字段；原文回退 | 直接：R23=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 25 / [25, 26] | #C C walks around the road | 没有已接受结构化字段；原文回退 | 直接：R24=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 26 / [26, 27] | #C C lifts up his hand | 没有已接受结构化字段；原文回退 | 直接：R25=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 27 / [27, 28] | #C C moves hand | 没有已接受结构化字段；原文回退 | 直接：R8=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 28 / [28, 29] | #C C pulls the leash | {"actions": [{"actor": "C", "verb": "pulls", "patient": "leash"}]} | 直接：R26=skip; R27=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 29 / [29, 30] | #C C lifts the hand | 没有已接受结构化字段；原文回退 | 直接：R28=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 30 / [30, 31] | #C C moves the hand | 没有已接受结构化字段；原文回退 | 直接：R29=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 31 / [31, 32] | #C C walks with the dog | 没有已接受结构化字段；原文回退 | 直接：R0=keep | At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog. |
| 32 / [32, 33] | #C C walks around the road | 没有已接受结构化字段；原文回退 | 直接：R24=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 33 / [33, 34] | #C C lifts the hand | 没有已接受结构化字段；原文回退 | 直接：R28=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 34 / [34, 35] | #C C lifts the hand | 没有已接受结构化字段；原文回退 | 直接：R28=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 35 / [35, 36] | #C C walks around the road | 没有已接受结构化字段；原文回退 | 直接：R24=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 36 / [36, 37] | #C C pulls the dog leash | {"actions": [{"actor": "C", "verb": "pulls", "patient": "dog leash"}]} | 直接：R7=keep; R30=skip | At sampled intervals [6,7), [36,37), [140,141), [142,143), [151,152), [154,155), [175,176), [176,177) (source assertion), C pulls dog leash. |
| 37 / [37, 38] | #C C touches the face | 没有已接受结构化字段；原文回退 | 直接：R31=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 38 / [38, 39] | #C C walks with the dog | 没有已接受结构化字段；原文回退 | 直接：R0=keep | At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog. |
| 39 / [39, 40] | #C C holds a dog leash | {"actions": [{"actor": "C", "verb": "holds", "patient": "dog leash"}]} | 直接：R15=keep; R32=skip | At sampled intervals [12,13), [39,40), [42,43), [136,137), [169,170), [177,178) (source assertion), C holds dog leash. |
| 40 / [40, 41] | #C C walks with the dog | 没有已接受结构化字段；原文回退 | 直接：R0=keep | At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog. |
| 41 / [41, 42] | #C C moves the dog leash | {"actions": [{"actor": "C", "verb": "moves", "patient": "dog leash"}]} | 直接：R11=skip; R20=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 42 / [42, 43] | #C C holds the dog leash | {"actions": [{"actor": "C", "verb": "holds", "patient": "dog leash"}]} | 直接：R14=skip; R15=keep | At sampled intervals [12,13), [39,40), [42,43), [136,137), [169,170), [177,178) (source assertion), C holds dog leash. |
| 43 / [43, 44] | #C C walks with the dog | 没有已接受结构化字段；原文回退 | 直接：R0=keep | At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog. |
| 44 / [44, 45] | #C C walks with the dog | 没有已接受结构化字段；原文回退 | 直接：R0=keep | At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog. |
| 135 / [135, 136] | #C C walks on the road | 没有已接受结构化字段；原文回退 | 直接：R33=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 136 / [136, 137] | #C C holds dog leash | {"actions": [{"actor": "C", "verb": "holds", "patient": "dog leash"}]} | 直接：R15=keep | At sampled intervals [12,13), [39,40), [42,43), [136,137), [169,170), [177,178) (source assertion), C holds dog leash. |
| 137 / [137, 138] | #C C stands on the road | 没有已接受结构化字段；原文回退 | 直接：R34=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 138 / [138, 139] | #C C looks around the road  | 没有已接受结构化字段；原文回退 | 直接：R35=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 139 / [139, 140] | #C C walks dog on the road  | {"actions": [{"actor": "C", "verb": "walks", "patient": "dog"}]} | 直接：R3=keep; R4=skip | At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog. |
| 140 / [140, 141] | #C C pulls dog leash | {"actions": [{"actor": "C", "verb": "pulls", "patient": "dog leash"}]} | 直接：R7=keep | At sampled intervals [6,7), [36,37), [140,141), [142,143), [151,152), [154,155), [175,176), [176,177) (source assertion), C pulls dog leash. |
| 141 / [141, 142] | #C C looks at dog  | 没有已接受结构化字段；原文回退 | 直接：R36=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 142 / [142, 143] | #C C pulls a dog leash  | {"actions": [{"actor": "C", "verb": "pulls", "patient": "dog leash"}]} | 直接：R6=skip; R7=keep | At sampled intervals [6,7), [36,37), [140,141), [142,143), [151,152), [154,155), [175,176), [176,177) (source assertion), C pulls dog leash. |
| 143 / [143, 144] | #C C stands on the road | 没有已接受结构化字段；原文回退 | 直接：R34=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 144 / [144, 145] | #C C walks on the road with a dog  | {"actions": [{"actor": "C", "verb": "walks", "body_part": "dog"}]} | 直接：R23=skip; R37=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 145 / [145, 146] | #C C walks with the dog | 没有已接受结构化字段；原文回退 | 直接：R0=keep | At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog. |
| 146 / [146, 147] | #C C holds a leash | {"actions": [{"actor": "C", "verb": "holds", "patient": "leash"}]} | 直接：R38=skip; R39=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 147 / [147, 148] | #C C stops walking | 没有已接受结构化字段；原文回退 | 直接：R40=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 148 / [148, 149] | #C C walks the dog along the road | {"actions": [{"actor": "C", "verb": "walks", "patient": "dog"}]} | 直接：R3=keep; R41=skip | At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog. |
| 149 / [149, 150] | #C C stops walking | 没有已接受结构化字段；原文回退 | 直接：R40=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 150 / [150, 151] | #C C stops  | 没有已接受结构化字段；原文回退 | 直接：R42=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 151 / [151, 152] | #C C pulls dog leash | {"actions": [{"actor": "C", "verb": "pulls", "patient": "dog leash"}]} | 直接：R7=keep | At sampled intervals [6,7), [36,37), [140,141), [142,143), [151,152), [154,155), [175,176), [176,177) (source assertion), C pulls dog leash. |
| 152 / [152, 153] | #C C walks with a dog | 没有已接受结构化字段；原文回退 | 直接：R22=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 153 / [153, 154] | #C C walks on the road | 没有已接受结构化字段；原文回退 | 直接：R33=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 154 / [154, 155] | #C C pulls dog leash | {"actions": [{"actor": "C", "verb": "pulls", "patient": "dog leash"}]} | 直接：R7=keep | At sampled intervals [6,7), [36,37), [140,141), [142,143), [151,152), [154,155), [175,176), [176,177) (source assertion), C pulls dog leash. |
| 155 / [155, 156] | #C C stops walking | 没有已接受结构化字段；原文回退 | 直接：R40=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 156 / [156, 157] | #C C walks the dogs | {"actions": [{"actor": "C", "verb": "walks", "patient": "dogs"}]} | 直接：R43=skip; R44=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 157 / [157, 158] | #C C walks dog on a road | {"actions": [{"actor": "C", "verb": "walks", "patient": "dog"}]} | 直接：R3=keep; R45=skip | At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog. |
| 158 / [158, 159] | #C C walks around | 没有已接受结构化字段；原文回退 | 直接：R19=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 159 / [159, 160] | #C C holds the leash with both hands | {"actions": [{"actor": "C", "verb": "holds", "patient": "leash"}]} | 直接：R39=skip; R46=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 160 / [160, 161] | #C C walks with dog on road | 没有已接受结构化字段；原文回退 | 直接：R47=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 161 / [161, 162] | #C C stops walking | 没有已接受结构化字段；原文回退 | 直接：R40=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 162 / [162, 163] | #C C walks around | 没有已接受结构化字段；原文回退 | 直接：R19=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 163 / [163, 164] | #C C walks around the road | 没有已接受结构化字段；原文回退 | 直接：R24=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 164 / [164, 165] | #C C walks around the road with the dog | 没有已接受结构化字段；原文回退 | 直接：R48=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 165 / [165, 166] | #C C walks with dog on the road | 没有已接受结构化字段；原文回退 | 直接：R17=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 166 / [166, 167] | #C C walks with dog on the road | 没有已接受结构化字段；原文回退 | 直接：R17=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 167 / [167, 168] | #C C walks with the dog | 没有已接受结构化字段；原文回退 | 直接：R0=keep | At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog. |
| 168 / [168, 169] | #C C walks around | 没有已接受结构化字段；原文回退 | 直接：R19=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 169 / [169, 170] | #C C holds dog leash | {"actions": [{"actor": "C", "verb": "holds", "patient": "dog leash"}]} | 直接：R15=keep | At sampled intervals [12,13), [39,40), [42,43), [136,137), [169,170), [177,178) (source assertion), C holds dog leash. |
| 170 / [170, 171] | #C C walks with dog | 没有已接受结构化字段；原文回退 | 直接：R23=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 171 / [171, 172] | #C C pulls the leash | {"actions": [{"actor": "C", "verb": "pulls", "patient": "leash"}]} | 直接：R26=skip; R27=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 172 / [172, 173] | #C C walks with the dog | 没有已接受结构化字段；原文回退 | 直接：R0=keep | At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog. |
| 173 / [173, 174] | #C C walks the dog | {"actions": [{"actor": "C", "verb": "walks", "patient": "dog"}]} | 直接：R3=keep; R49=skip | At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog. |
| 174 / [174, 175] | #C C walks dog along the road | {"actions": [{"actor": "C", "verb": "walks", "patient": "dog"}]} | 直接：R2=skip; R3=keep | At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog. |
| 175 / [175, 176] | #C C pulls the dog leash with his left hand | {"actions": [{"actor": "C", "verb": "pulls", "patient": "dog leash"}]} | 直接：R7=keep; R50=skip | At sampled intervals [6,7), [36,37), [140,141), [142,143), [151,152), [154,155), [175,176), [176,177) (source assertion), C pulls dog leash. |
| 176 / [176, 177] | #C C pulls dog leash | {"actions": [{"actor": "C", "verb": "pulls", "patient": "dog leash"}]} | 直接：R7=keep | At sampled intervals [6,7), [36,37), [140,141), [142,143), [151,152), [154,155), [175,176), [176,177) (source assertion), C pulls dog leash. |
| 177 / [177, 178] | #C C holds a dog leash  | {"actions": [{"actor": "C", "verb": "holds", "patient": "dog leash"}]} | 直接：R15=keep; R32=skip | At sampled intervals [12,13), [39,40), [42,43), [136,137), [169,170), [177,178) (source assertion), C holds dog leash. |
| 178 / [178, 179] | #C C looks around | 没有已接受结构化字段；原文回退 | 直接：R51=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 179 / [179, 180] | #C C stares around | 没有已接受结构化字段；原文回退 | 直接：R52=skip | ∅（本行没有直接证据或时序关联进入最终输入） |

## 逐条展开

下方保留F完整档案，区分“已接受字段”和“保留原文”；最终段落逐字复制，不人工改写。


### 采样点 0，区间 [0, 1]

**原始文本**

```text
#C C walks with the dog
```

**F实际保存**

```json
{
  "original_text": "#C C walks with the dog"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R0：`keep`；C walks with the dog.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R0：

```text
At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog.
```


### 采样点 1，区间 [1, 2]

**原始文本**

```text
#C C walks with the dog
```

**F实际保存**

```json
{
  "original_text": "#C C walks with the dog"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R0：`keep`；C walks with the dog.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R0：

```text
At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog.
```


### 采样点 2，区间 [2, 3]

**原始文本**

```text
#C C moves a hand
```

**F实际保存**

```json
{
  "original_text": "#C C moves a hand"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R1：`skip`；C moves a hand.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 3，区间 [3, 4]

**原始文本**

```text
#C C walks dog along the road
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "walks",
      "patient": "dog"
    }
  ],
  "original_text": "#C C walks dog along the road"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R2：`skip`；C walks dog along the road.
- R3：`keep`；C walks dog.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R3：

```text
At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog.
```


### 采样点 4，区间 [4, 5]

**原始文本**

```text
#C C walks dog on the road
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "walks",
      "patient": "dog"
    }
  ],
  "original_text": "#C C walks dog on the road"
}
```

**K处理状态：** 直接证据及上游时序关联均保留。


直接候选：

- R3：`keep`；C walks dog.
- R4：`skip`；C walks dog on the road.

上游时序关联候选：

- R53：`keep`；C walks dog on the road. The text memory associates this observation with C (text reference E0).
- R54：`keep`；C walks dog on the road. The text memory associates this observation with dog (text reference E1).

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R3：

```text
At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog.
```

来自引用本行的上游时序条目；对应候选 R53：

```text
At sampled intervals [4,5), [5,6), [15,16), [16,17) (source assertion), C walks dog on the road. The text memory associates this observation with C (text reference E0).
```

来自引用本行的上游时序条目；对应候选 R54：

```text
At sampled intervals [4,5), [15,16), [16,17) (source assertion), C walks dog on the road. The text memory associates this observation with dog (text reference E1).
```


### 采样点 5，区间 [5, 6]

**原始文本**

```text
#C C walks on a pathway
```

**F实际保存**

```json
{
  "original_text": "#C C walks on a pathway"
}
```

**K处理状态：** 直接候选跳过，仅保留上游时序关联。


直接候选：

- R5：`skip`；C walks on a pathway.

上游时序关联候选：

- R53：`keep`；C walks dog on the road. The text memory associates this observation with C (text reference E0).

**处理到最后的实际文本**

来自引用本行的上游时序条目；对应候选 R53：

```text
At sampled intervals [4,5), [5,6), [15,16), [16,17) (source assertion), C walks dog on the road. The text memory associates this observation with C (text reference E0).
```


### 采样点 6，区间 [6, 7]

**原始文本**

```text
#C C pulls a dog leash 
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "pulls",
      "patient": "dog leash"
    }
  ],
  "original_text": "#C C pulls a dog leash "
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R6：`skip`；C pulls a dog leash.
- R7：`keep`；C pulls dog leash.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R7：

```text
At sampled intervals [6,7), [36,37), [140,141), [142,143), [151,152), [154,155), [175,176), [176,177) (source assertion), C pulls dog leash.
```


### 采样点 7，区间 [7, 8]

**原始文本**

```text
#C C moves hand
```

**F实际保存**

```json
{
  "original_text": "#C C moves hand"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R8：`skip`；C moves hand.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 8，区间 [8, 9]

**原始文本**

```text
#C C scratches face
```

**F实际保存**

```json
{
  "original_text": "#C C scratches face"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R9：`skip`；C scratches face.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 9，区间 [9, 10]

**原始文本**

```text
#C C scratches face
```

**F实际保存**

```json
{
  "original_text": "#C C scratches face"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R9：`skip`；C scratches face.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 10，区间 [10, 11]

**原始文本**

```text
#C C moves the dog leash to the right hand 
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "moves",
      "patient": "dog leash"
    }
  ],
  "original_text": "#C C moves the dog leash to the right hand "
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R10：`skip`；C moves the dog leash to the right hand.
- R11：`skip`；C moves dog leash.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 11，区间 [11, 12]

**原始文本**

```text
#C C holds the bag
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "holds",
      "patient": "bag"
    }
  ],
  "original_text": "#C C holds the bag"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R12：`skip`；C holds the bag.
- R13：`skip`；C holds bag.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 12，区间 [12, 13]

**原始文本**

```text
#C C holds the dog leash
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "holds",
      "patient": "dog leash"
    }
  ],
  "original_text": "#C C holds the dog leash"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R14：`skip`；C holds the dog leash.
- R15：`keep`；C holds dog leash.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R15：

```text
At sampled intervals [12,13), [39,40), [42,43), [136,137), [169,170), [177,178) (source assertion), C holds dog leash.
```


### 采样点 13，区间 [13, 14]

**原始文本**

```text
#C C puts the carrier bag in the bag
```

**F实际保存**

```json
{
  "original_text": "#C C puts the carrier bag in the bag"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R16：`skip`；C puts the carrier bag in the bag.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 14，区间 [14, 15]

**原始文本**

```text
#C C walks with the dog
```

**F实际保存**

```json
{
  "original_text": "#C C walks with the dog"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R0：`keep`；C walks with the dog.

上游时序关联候选：

- R55：`skip`；An unresolved source observation states: "C walks with the dog".

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R0：

```text
At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog.
```


### 采样点 15，区间 [15, 16]

**原始文本**

```text
#C C walks with dog on the road
```

**F实际保存**

```json
{
  "original_text": "#C C walks with dog on the road"
}
```

**K处理状态：** 直接候选跳过，仅保留上游时序关联。


直接候选：

- R17：`skip`；C walks with dog on the road.

上游时序关联候选：

- R53：`keep`；C walks dog on the road. The text memory associates this observation with C (text reference E0).
- R54：`keep`；C walks dog on the road. The text memory associates this observation with dog (text reference E1).

**处理到最后的实际文本**

来自引用本行的上游时序条目；对应候选 R53：

```text
At sampled intervals [4,5), [5,6), [15,16), [16,17) (source assertion), C walks dog on the road. The text memory associates this observation with C (text reference E0).
```

来自引用本行的上游时序条目；对应候选 R54：

```text
At sampled intervals [4,5), [15,16), [16,17) (source assertion), C walks dog on the road. The text memory associates this observation with dog (text reference E1).
```


### 采样点 16，区间 [16, 17]

**原始文本**

```text
#C C walks with the dog
```

**F实际保存**

```json
{
  "original_text": "#C C walks with the dog"
}
```

**K处理状态：** 直接证据及上游时序关联均保留。


直接候选：

- R0：`keep`；C walks with the dog.

上游时序关联候选：

- R53：`keep`；C walks dog on the road. The text memory associates this observation with C (text reference E0).
- R54：`keep`；C walks dog on the road. The text memory associates this observation with dog (text reference E1).

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R0：

```text
At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog.
```

来自引用本行的上游时序条目；对应候选 R53：

```text
At sampled intervals [4,5), [5,6), [15,16), [16,17) (source assertion), C walks dog on the road. The text memory associates this observation with C (text reference E0).
```

来自引用本行的上游时序条目；对应候选 R54：

```text
At sampled intervals [4,5), [15,16), [16,17) (source assertion), C walks dog on the road. The text memory associates this observation with dog (text reference E1).
```


### 采样点 17，区间 [17, 18]

**原始文本**

```text
#C C walks dog
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "walks",
      "patient": "dog"
    }
  ],
  "original_text": "#C C walks dog"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R3：`keep`；C walks dog.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R3：

```text
At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog.
```


### 采样点 18，区间 [18, 19]

**原始文本**

```text
#C C walks dog with a leash
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "walks",
      "patient": "dog"
    }
  ],
  "original_text": "#C C walks dog with a leash"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R3：`keep`；C walks dog.
- R18：`skip`；C walks dog with a leash.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R3：

```text
At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog.
```


### 采样点 19，区间 [19, 20]

**原始文本**

```text
#C C walks around
```

**F实际保存**

```json
{
  "original_text": "#C C walks around"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R19：`skip`；C walks around.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 20，区间 [20, 21]

**原始文本**

```text
#C C walks around
```

**F实际保存**

```json
{
  "original_text": "#C C walks around"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R19：`skip`；C walks around.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 21，区间 [21, 22]

**原始文本**

```text
#C C moves the dog leash
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "moves",
      "patient": "dog leash"
    }
  ],
  "original_text": "#C C moves the dog leash"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R11：`skip`；C moves dog leash.
- R20：`skip`；C moves the dog leash.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 22，区间 [22, 23]

**原始文本**

```text
#C C moves the dog leash in her left hand to her right hand.
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "moves",
      "patient": "dog leash"
    }
  ],
  "original_text": "#C C moves the dog leash in her left hand to her right hand."
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R11：`skip`；C moves dog leash.
- R21：`skip`；C moves the dog leash in her left hand to her right hand.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 23，区间 [23, 24]

**原始文本**

```text
#C C walks with a dog
```

**F实际保存**

```json
{
  "original_text": "#C C walks with a dog"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R22：`skip`；C walks with a dog.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 24，区间 [24, 25]

**原始文本**

```text
#C C walks with dog 
```

**F实际保存**

```json
{
  "original_text": "#C C walks with dog "
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R23：`skip`；C walks with dog.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 25，区间 [25, 26]

**原始文本**

```text
#C C walks around the road
```

**F实际保存**

```json
{
  "original_text": "#C C walks around the road"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R24：`skip`；C walks around the road.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 26，区间 [26, 27]

**原始文本**

```text
#C C lifts up his hand
```

**F实际保存**

```json
{
  "original_text": "#C C lifts up his hand"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R25：`skip`；C lifts up his hand.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 27，区间 [27, 28]

**原始文本**

```text
#C C moves hand
```

**F实际保存**

```json
{
  "original_text": "#C C moves hand"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R8：`skip`；C moves hand.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 28，区间 [28, 29]

**原始文本**

```text
#C C pulls the leash
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "pulls",
      "patient": "leash"
    }
  ],
  "original_text": "#C C pulls the leash"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R26：`skip`；C pulls the leash.
- R27：`skip`；C pulls leash.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 29，区间 [29, 30]

**原始文本**

```text
#C C lifts the hand
```

**F实际保存**

```json
{
  "original_text": "#C C lifts the hand"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R28：`skip`；C lifts the hand.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 30，区间 [30, 31]

**原始文本**

```text
#C C moves the hand
```

**F实际保存**

```json
{
  "original_text": "#C C moves the hand"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R29：`skip`；C moves the hand.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 31，区间 [31, 32]

**原始文本**

```text
#C C walks with the dog
```

**F实际保存**

```json
{
  "original_text": "#C C walks with the dog"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R0：`keep`；C walks with the dog.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R0：

```text
At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog.
```


### 采样点 32，区间 [32, 33]

**原始文本**

```text
#C C walks around the road
```

**F实际保存**

```json
{
  "original_text": "#C C walks around the road"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R24：`skip`；C walks around the road.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 33，区间 [33, 34]

**原始文本**

```text
#C C lifts the hand
```

**F实际保存**

```json
{
  "original_text": "#C C lifts the hand"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R28：`skip`；C lifts the hand.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 34，区间 [34, 35]

**原始文本**

```text
#C C lifts the hand
```

**F实际保存**

```json
{
  "original_text": "#C C lifts the hand"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R28：`skip`；C lifts the hand.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 35，区间 [35, 36]

**原始文本**

```text
#C C walks around the road
```

**F实际保存**

```json
{
  "original_text": "#C C walks around the road"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R24：`skip`；C walks around the road.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 36，区间 [36, 37]

**原始文本**

```text
#C C pulls the dog leash
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "pulls",
      "patient": "dog leash"
    }
  ],
  "original_text": "#C C pulls the dog leash"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R7：`keep`；C pulls dog leash.
- R30：`skip`；C pulls the dog leash.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R7：

```text
At sampled intervals [6,7), [36,37), [140,141), [142,143), [151,152), [154,155), [175,176), [176,177) (source assertion), C pulls dog leash.
```


### 采样点 37，区间 [37, 38]

**原始文本**

```text
#C C touches the face
```

**F实际保存**

```json
{
  "original_text": "#C C touches the face"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R31：`skip`；C touches the face.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 38，区间 [38, 39]

**原始文本**

```text
#C C walks with the dog
```

**F实际保存**

```json
{
  "original_text": "#C C walks with the dog"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R0：`keep`；C walks with the dog.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R0：

```text
At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog.
```


### 采样点 39，区间 [39, 40]

**原始文本**

```text
#C C holds a dog leash
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "holds",
      "patient": "dog leash"
    }
  ],
  "original_text": "#C C holds a dog leash"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R15：`keep`；C holds dog leash.
- R32：`skip`；C holds a dog leash.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R15：

```text
At sampled intervals [12,13), [39,40), [42,43), [136,137), [169,170), [177,178) (source assertion), C holds dog leash.
```


### 采样点 40，区间 [40, 41]

**原始文本**

```text
#C C walks with the dog
```

**F实际保存**

```json
{
  "original_text": "#C C walks with the dog"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R0：`keep`；C walks with the dog.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R0：

```text
At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog.
```


### 采样点 41，区间 [41, 42]

**原始文本**

```text
#C C moves the dog leash
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "moves",
      "patient": "dog leash"
    }
  ],
  "original_text": "#C C moves the dog leash"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R11：`skip`；C moves dog leash.
- R20：`skip`；C moves the dog leash.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 42，区间 [42, 43]

**原始文本**

```text
#C C holds the dog leash
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "holds",
      "patient": "dog leash"
    }
  ],
  "original_text": "#C C holds the dog leash"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R14：`skip`；C holds the dog leash.
- R15：`keep`；C holds dog leash.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R15：

```text
At sampled intervals [12,13), [39,40), [42,43), [136,137), [169,170), [177,178) (source assertion), C holds dog leash.
```


### 采样点 43，区间 [43, 44]

**原始文本**

```text
#C C walks with the dog
```

**F实际保存**

```json
{
  "original_text": "#C C walks with the dog"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R0：`keep`；C walks with the dog.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R0：

```text
At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog.
```


### 采样点 44，区间 [44, 45]

**原始文本**

```text
#C C walks with the dog
```

**F实际保存**

```json
{
  "original_text": "#C C walks with the dog"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R0：`keep`；C walks with the dog.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R0：

```text
At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog.
```


### 采样点 135，区间 [135, 136]

**原始文本**

```text
#C C walks on the road
```

**F实际保存**

```json
{
  "original_text": "#C C walks on the road"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R33：`skip`；C walks on the road.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 136，区间 [136, 137]

**原始文本**

```text
#C C holds dog leash
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "holds",
      "patient": "dog leash"
    }
  ],
  "original_text": "#C C holds dog leash"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R15：`keep`；C holds dog leash.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R15：

```text
At sampled intervals [12,13), [39,40), [42,43), [136,137), [169,170), [177,178) (source assertion), C holds dog leash.
```


### 采样点 137，区间 [137, 138]

**原始文本**

```text
#C C stands on the road
```

**F实际保存**

```json
{
  "original_text": "#C C stands on the road"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R34：`skip`；C stands on the road.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 138，区间 [138, 139]

**原始文本**

```text
#C C looks around the road 
```

**F实际保存**

```json
{
  "original_text": "#C C looks around the road "
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R35：`skip`；C looks around the road.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 139，区间 [139, 140]

**原始文本**

```text
#C C walks dog on the road 
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "walks",
      "patient": "dog"
    }
  ],
  "original_text": "#C C walks dog on the road "
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R3：`keep`；C walks dog.
- R4：`skip`；C walks dog on the road.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R3：

```text
At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog.
```


### 采样点 140，区间 [140, 141]

**原始文本**

```text
#C C pulls dog leash
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "pulls",
      "patient": "dog leash"
    }
  ],
  "original_text": "#C C pulls dog leash"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R7：`keep`；C pulls dog leash.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R7：

```text
At sampled intervals [6,7), [36,37), [140,141), [142,143), [151,152), [154,155), [175,176), [176,177) (source assertion), C pulls dog leash.
```


### 采样点 141，区间 [141, 142]

**原始文本**

```text
#C C looks at dog 
```

**F实际保存**

```json
{
  "original_text": "#C C looks at dog "
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R36：`skip`；C looks at dog.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 142，区间 [142, 143]

**原始文本**

```text
#C C pulls a dog leash 
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "pulls",
      "patient": "dog leash"
    }
  ],
  "original_text": "#C C pulls a dog leash "
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R6：`skip`；C pulls a dog leash.
- R7：`keep`；C pulls dog leash.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R7：

```text
At sampled intervals [6,7), [36,37), [140,141), [142,143), [151,152), [154,155), [175,176), [176,177) (source assertion), C pulls dog leash.
```


### 采样点 143，区间 [143, 144]

**原始文本**

```text
#C C stands on the road
```

**F实际保存**

```json
{
  "original_text": "#C C stands on the road"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R34：`skip`；C stands on the road.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 144，区间 [144, 145]

**原始文本**

```text
#C C walks on the road with a dog 
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "walks",
      "body_part": "dog"
    }
  ],
  "original_text": "#C C walks on the road with a dog "
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R23：`skip`；C walks with dog.
- R37：`skip`；C walks on the road with a dog.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 145，区间 [145, 146]

**原始文本**

```text
#C C walks with the dog
```

**F实际保存**

```json
{
  "original_text": "#C C walks with the dog"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R0：`keep`；C walks with the dog.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R0：

```text
At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog.
```


### 采样点 146，区间 [146, 147]

**原始文本**

```text
#C C holds a leash
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "holds",
      "patient": "leash"
    }
  ],
  "original_text": "#C C holds a leash"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R38：`skip`；C holds a leash.
- R39：`skip`；C holds leash.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 147，区间 [147, 148]

**原始文本**

```text
#C C stops walking
```

**F实际保存**

```json
{
  "original_text": "#C C stops walking"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R40：`skip`；C stops walking.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 148，区间 [148, 149]

**原始文本**

```text
#C C walks the dog along the road
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "walks",
      "patient": "dog"
    }
  ],
  "original_text": "#C C walks the dog along the road"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R3：`keep`；C walks dog.
- R41：`skip`；C walks the dog along the road.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R3：

```text
At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog.
```


### 采样点 149，区间 [149, 150]

**原始文本**

```text
#C C stops walking
```

**F实际保存**

```json
{
  "original_text": "#C C stops walking"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R40：`skip`；C stops walking.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 150，区间 [150, 151]

**原始文本**

```text
#C C stops 
```

**F实际保存**

```json
{
  "original_text": "#C C stops "
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R42：`skip`；C stops.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 151，区间 [151, 152]

**原始文本**

```text
#C C pulls dog leash
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "pulls",
      "patient": "dog leash"
    }
  ],
  "original_text": "#C C pulls dog leash"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R7：`keep`；C pulls dog leash.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R7：

```text
At sampled intervals [6,7), [36,37), [140,141), [142,143), [151,152), [154,155), [175,176), [176,177) (source assertion), C pulls dog leash.
```


### 采样点 152，区间 [152, 153]

**原始文本**

```text
#C C walks with a dog
```

**F实际保存**

```json
{
  "original_text": "#C C walks with a dog"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R22：`skip`；C walks with a dog.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 153，区间 [153, 154]

**原始文本**

```text
#C C walks on the road
```

**F实际保存**

```json
{
  "original_text": "#C C walks on the road"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R33：`skip`；C walks on the road.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 154，区间 [154, 155]

**原始文本**

```text
#C C pulls dog leash
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "pulls",
      "patient": "dog leash"
    }
  ],
  "original_text": "#C C pulls dog leash"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R7：`keep`；C pulls dog leash.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R7：

```text
At sampled intervals [6,7), [36,37), [140,141), [142,143), [151,152), [154,155), [175,176), [176,177) (source assertion), C pulls dog leash.
```


### 采样点 155，区间 [155, 156]

**原始文本**

```text
#C C stops walking
```

**F实际保存**

```json
{
  "original_text": "#C C stops walking"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R40：`skip`；C stops walking.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 156，区间 [156, 157]

**原始文本**

```text
#C C walks the dogs
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "walks",
      "patient": "dogs"
    }
  ],
  "original_text": "#C C walks the dogs"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R43：`skip`；C walks the dogs.
- R44：`skip`；C walks dogs.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 157，区间 [157, 158]

**原始文本**

```text
#C C walks dog on a road
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "walks",
      "patient": "dog"
    }
  ],
  "original_text": "#C C walks dog on a road"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R3：`keep`；C walks dog.
- R45：`skip`；C walks dog on a road.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R3：

```text
At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog.
```


### 采样点 158，区间 [158, 159]

**原始文本**

```text
#C C walks around
```

**F实际保存**

```json
{
  "original_text": "#C C walks around"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R19：`skip`；C walks around.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 159，区间 [159, 160]

**原始文本**

```text
#C C holds the leash with both hands
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "holds",
      "patient": "leash"
    }
  ],
  "original_text": "#C C holds the leash with both hands"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R39：`skip`；C holds leash.
- R46：`skip`；C holds the leash with both hands.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 160，区间 [160, 161]

**原始文本**

```text
#C C walks with dog on road
```

**F实际保存**

```json
{
  "original_text": "#C C walks with dog on road"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R47：`skip`；C walks with dog on road.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 161，区间 [161, 162]

**原始文本**

```text
#C C stops walking
```

**F实际保存**

```json
{
  "original_text": "#C C stops walking"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R40：`skip`；C stops walking.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 162，区间 [162, 163]

**原始文本**

```text
#C C walks around
```

**F实际保存**

```json
{
  "original_text": "#C C walks around"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R19：`skip`；C walks around.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 163，区间 [163, 164]

**原始文本**

```text
#C C walks around the road
```

**F实际保存**

```json
{
  "original_text": "#C C walks around the road"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R24：`skip`；C walks around the road.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 164，区间 [164, 165]

**原始文本**

```text
#C C walks around the road with the dog
```

**F实际保存**

```json
{
  "original_text": "#C C walks around the road with the dog"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R48：`skip`；C walks around the road with the dog.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 165，区间 [165, 166]

**原始文本**

```text
#C C walks with dog on the road
```

**F实际保存**

```json
{
  "original_text": "#C C walks with dog on the road"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R17：`skip`；C walks with dog on the road.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 166，区间 [166, 167]

**原始文本**

```text
#C C walks with dog on the road
```

**F实际保存**

```json
{
  "original_text": "#C C walks with dog on the road"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R17：`skip`；C walks with dog on the road.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 167，区间 [167, 168]

**原始文本**

```text
#C C walks with the dog
```

**F实际保存**

```json
{
  "original_text": "#C C walks with the dog"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R0：`keep`；C walks with the dog.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R0：

```text
At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog.
```


### 采样点 168，区间 [168, 169]

**原始文本**

```text
#C C walks around
```

**F实际保存**

```json
{
  "original_text": "#C C walks around"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R19：`skip`；C walks around.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 169，区间 [169, 170]

**原始文本**

```text
#C C holds dog leash
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "holds",
      "patient": "dog leash"
    }
  ],
  "original_text": "#C C holds dog leash"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R15：`keep`；C holds dog leash.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R15：

```text
At sampled intervals [12,13), [39,40), [42,43), [136,137), [169,170), [177,178) (source assertion), C holds dog leash.
```


### 采样点 170，区间 [170, 171]

**原始文本**

```text
#C C walks with dog
```

**F实际保存**

```json
{
  "original_text": "#C C walks with dog"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R23：`skip`；C walks with dog.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 171，区间 [171, 172]

**原始文本**

```text
#C C pulls the leash
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "pulls",
      "patient": "leash"
    }
  ],
  "original_text": "#C C pulls the leash"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R26：`skip`；C pulls the leash.
- R27：`skip`；C pulls leash.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 172，区间 [172, 173]

**原始文本**

```text
#C C walks with the dog
```

**F实际保存**

```json
{
  "original_text": "#C C walks with the dog"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R0：`keep`；C walks with the dog.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R0：

```text
At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog.
```


### 采样点 173，区间 [173, 174]

**原始文本**

```text
#C C walks the dog
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "walks",
      "patient": "dog"
    }
  ],
  "original_text": "#C C walks the dog"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R3：`keep`；C walks dog.
- R49：`skip`；C walks the dog.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R3：

```text
At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog.
```


### 采样点 174，区间 [174, 175]

**原始文本**

```text
#C C walks dog along the road
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "walks",
      "patient": "dog"
    }
  ],
  "original_text": "#C C walks dog along the road"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R2：`skip`；C walks dog along the road.
- R3：`keep`；C walks dog.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R3：

```text
At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog.
```


### 采样点 175，区间 [175, 176]

**原始文本**

```text
#C C pulls the dog leash with his left hand
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "pulls",
      "patient": "dog leash"
    }
  ],
  "original_text": "#C C pulls the dog leash with his left hand"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R7：`keep`；C pulls dog leash.
- R50：`skip`；C pulls the dog leash with his left hand.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R7：

```text
At sampled intervals [6,7), [36,37), [140,141), [142,143), [151,152), [154,155), [175,176), [176,177) (source assertion), C pulls dog leash.
```


### 采样点 176，区间 [176, 177]

**原始文本**

```text
#C C pulls dog leash
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "pulls",
      "patient": "dog leash"
    }
  ],
  "original_text": "#C C pulls dog leash"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R7：`keep`；C pulls dog leash.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R7：

```text
At sampled intervals [6,7), [36,37), [140,141), [142,143), [151,152), [154,155), [175,176), [176,177) (source assertion), C pulls dog leash.
```


### 采样点 177，区间 [177, 178]

**原始文本**

```text
#C C holds a dog leash 
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "holds",
      "patient": "dog leash"
    }
  ],
  "original_text": "#C C holds a dog leash "
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R15：`keep`；C holds dog leash.
- R32：`skip`；C holds a dog leash.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R15：

```text
At sampled intervals [12,13), [39,40), [42,43), [136,137), [169,170), [177,178) (source assertion), C holds dog leash.
```


### 采样点 178，区间 [178, 179]

**原始文本**

```text
#C C looks around
```

**F实际保存**

```json
{
  "original_text": "#C C looks around"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R51：`skip`；C looks around.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 179，区间 [179, 180]

**原始文本**

```text
#C C stares around
```

**F实际保存**

```json
{
  "original_text": "#C C stares around"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R52：`skip`；C stares around.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。
