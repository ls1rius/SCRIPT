# 按每条原始采样文本查看处理前后

每一行对应上游选中的一个一秒采样点。按时间顺序列出所有已选原文，不只展示K保留的行。

**读法：** 原文 → F已接受的解构字段（原文回退另存） → K的直接及上游时序候选取舍 → 最终输入里的实际完整段落。

**共享段落说明：** 同一候选可以引用多个采样点，因此下表可能在多行重复展示同一段，方便逐帧追踪；模型实际只接收[完整输入](12_actual_chat_template.txt)中的一份。时序关联也不等于本行原文支持该段的所有细节。

## 按帧总表

| 采样点／区间 | 原始文本 | F已接受解构字段 | K取舍 | 最终输入的实际文本（含关联时序段落） |
|---|---|---|---|---|
| 0 / [0, 1] | #C C moves book | {"actions": [{"actor": "C", "verb": "moves", "patient": "book"}]} | 直接：R0=keep | At sampled intervals [0,1), [6,7), [34,35) (source assertion), C moves book. |
| 1 / [1, 2] | #C C rubs the paint | {"actions": [{"actor": "C", "verb": "rubs", "patient": "paint"}]} | 直接：R1=skip; R2=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 2 / [2, 3] | #C C draws on the paper | 没有已接受结构化字段；原文回退 | 直接：R3=keep | At sampled intervals [2,3), [26,27), [35,36), [36,37), [41,42) (source assertion), C draws on the paper. |
| 3 / [3, 4] | #C C draws on the painting paper | 没有已接受结构化字段；原文回退 | 直接：R4=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 4 / [4, 5] | #C C draws a picture | {"actions": [{"actor": "C", "verb": "draws", "patient": "picture"}]} | 直接：R5=keep; R6=keep | At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.<br><br>At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture. |
| 5 / [5, 6] | #C C adjusts the drawing | {"actions": [{"actor": "C", "verb": "adjusts", "patient": "drawing"}]} | 直接：R7=skip; R8=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 6 / [6, 7] | #C C moves the book | {"actions": [{"actor": "C", "verb": "moves", "patient": "book"}]} | 直接：R0=keep; R9=keep | At sampled intervals [0,1), [6,7), [34,35) (source assertion), C moves book.<br><br>At sampled intervals [6,7), [34,35) (source assertion), C moves the book. |
| 7 / [7, 8] | #C C moves hand on the table | 没有已接受结构化字段；原文回退 | 直接：R10=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 8 / [8, 9] | #C C moves a rubber on the table | {"actions": [{"actor": "C", "verb": "moves", "patient": "rubber"}]} | 直接：R11=skip; R12=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 9 / [9, 10] | #C C draws on the drawing book with a pencil | 没有已接受结构化字段；原文回退 | 直接：R13=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 10 / [10, 11] | #C C moves pen | {"actions": [{"actor": "C", "verb": "moves", "patient": "pen"}]} | 直接：R14=skip<br>时序：R92=group:R92; R94=group:R94 | At sampled intervals [10,11), [22,23), [24,25), [148,149) (text-inferred hypothesis), The text memory proposes "different_instance" between "C (text reference E2)" and "C (text reference E0)"; the recorded rationale is "Captions describe different actions on books in different scenes".<br><br>At sampled intervals [10,11), [22,23), [24,25), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "C (text reference E2)" and "book (text reference E1)"; the recorded rationale is "Captions describe different actions on books in different scenes". |
| 11 / [11, 12] | #C C moves the pencil to the right | {"actions": [{"actor": "C", "verb": "moves", "patient": "pencil"}]} | 直接：R15=skip; R16=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 12 / [12, 13] | #C C moves the drawing book  | {"actions": [{"actor": "C", "verb": "moves", "patient": "drawing book"}]} | 直接：R17=keep; R18=keep | At sampled intervals [12,13), [135,136) (source assertion), C moves the drawing book.<br><br>At sampled intervals [12,13), [135,136) (source assertion), C moves drawing book. |
| 13 / [13, 14] | #C C rubs a drawing | {"actions": [{"actor": "C", "verb": "rubs", "patient": "drawing"}]} | 直接：R19=skip; R20=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 14 / [14, 15] | #C C rubs a drawing with a rubber | 没有已接受结构化字段；原文回退 | 直接：R21=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 15 / [15, 16] | #C C rubs a drawing | {"actions": [{"actor": "C", "verb": "rubs", "patient": "drawing"}]} | 直接：R19=skip; R20=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 16 / [16, 17] | #C C rubs drawing | {"actions": [{"actor": "C", "verb": "rubs", "patient": "drawing"}]} | 直接：R20=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 17 / [17, 18] | #C C  moves hands  | 没有已接受结构化字段；原文回退 | 直接：R22=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 18 / [18, 19] | #C C scratches the book | {"actions": [{"actor": "C", "verb": "scratches", "patient": "book"}]} | 直接：R23=skip; R24=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 19 / [19, 20] | #C C wipes the face | 没有已接受结构化字段；原文回退 | 直接：R25=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 20 / [20, 21] | #C C scratches her face | {"actions": [{"actor": "C", "verb": "scratches", "patient": "face"}]} | 直接：R26=skip; R27=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 21 / [21, 22] | #C C drops pencil | {"actions": [{"actor": "C", "verb": "drops", "patient": "pencil"}]} | 直接：R28=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 22 / [22, 23] | #C C draws on the book with a pen  | 没有已接受结构化字段；原文回退 | 直接：R29=skip<br>时序：R86=group:R86; R87=group:R86; R90=group:R90; R92=group:R92; R93=group:R93; R94=group:R94; R95=group:R95 | At sampled intervals [22,23), [25,26) (source assertion), C draws on the book with a pen. The text memory associates this observation with C (text reference E2). At sampled intervals [22,23), [25,26) (source assertion), C draws on the book with a pen. The text memory associates this observation with book (text reference E1).<br><br>At sampled intervals [22,23), [24,25), [25,26) (source assertion), The source describes "C (text reference E2)" as the subject of "draws on", with "book (text reference E1)" as its object.<br><br>At sampled intervals [10,11), [22,23), [24,25), [148,149) (text-inferred hypothesis), The text memory proposes "different_instance" between "C (text reference E2)" and "C (text reference E0)"; the recorded rationale is "Captions describe different actions on books in different scenes".<br><br>At sampled intervals [22,23), [24,25), [25,26), [149,150) (text-inferred hypothesis), The text memory proposes "same_instance" between "book (text reference E1)" and "book (text reference E1)"; the recorded rationale is "Both captions mention drawing on a book labeled 'book' in scene S0 and S4".<br><br>At sampled intervals [10,11), [22,23), [24,25), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "C (text reference E2)" and "book (text reference E1)"; the recorded rationale is "Captions describe different actions on books in different scenes".<br><br>At sampled intervals [22,23), [24,25), [25,26), [148,149), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "book (text reference E1)" and "C (text reference E0)"; the recorded rationale is "C draws on different objects (book vs. art paper).". |
| 23 / [23, 24] | #C C draws on a paper | 没有已接受结构化字段；原文回退 | 直接：R30=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 24 / [24, 25] | #C C draws on the book | 没有已接受结构化字段；原文回退 | 直接：R31=keep<br>时序：R90=group:R90; R92=group:R92; R93=group:R93; R94=group:R94; R95=group:R95; R96=skip | At sampled intervals [24,25), [25,26), [27,28), [115,116), [123,124), [128,129), [131,132), [149,150) (source assertion), C draws on the book.<br><br>At sampled intervals [22,23), [24,25), [25,26) (source assertion), The source describes "C (text reference E2)" as the subject of "draws on", with "book (text reference E1)" as its object.<br><br>At sampled intervals [10,11), [22,23), [24,25), [148,149) (text-inferred hypothesis), The text memory proposes "different_instance" between "C (text reference E2)" and "C (text reference E0)"; the recorded rationale is "Captions describe different actions on books in different scenes".<br><br>At sampled intervals [22,23), [24,25), [25,26), [149,150) (text-inferred hypothesis), The text memory proposes "same_instance" between "book (text reference E1)" and "book (text reference E1)"; the recorded rationale is "Both captions mention drawing on a book labeled 'book' in scene S0 and S4".<br><br>At sampled intervals [10,11), [22,23), [24,25), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "C (text reference E2)" and "book (text reference E1)"; the recorded rationale is "Captions describe different actions on books in different scenes".<br><br>At sampled intervals [22,23), [24,25), [25,26), [148,149), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "book (text reference E1)" and "C (text reference E0)"; the recorded rationale is "C draws on different objects (book vs. art paper).". |
| 25 / [25, 26] | #C C draws on the book | 没有已接受结构化字段；原文回退 | 直接：R31=keep<br>时序：R86=group:R86; R87=group:R86; R90=group:R90; R93=group:R93; R95=group:R95 | At sampled intervals [24,25), [25,26), [27,28), [115,116), [123,124), [128,129), [131,132), [149,150) (source assertion), C draws on the book.<br><br>At sampled intervals [22,23), [25,26) (source assertion), C draws on the book with a pen. The text memory associates this observation with C (text reference E2). At sampled intervals [22,23), [25,26) (source assertion), C draws on the book with a pen. The text memory associates this observation with book (text reference E1).<br><br>At sampled intervals [22,23), [24,25), [25,26) (source assertion), The source describes "C (text reference E2)" as the subject of "draws on", with "book (text reference E1)" as its object.<br><br>At sampled intervals [22,23), [24,25), [25,26), [149,150) (text-inferred hypothesis), The text memory proposes "same_instance" between "book (text reference E1)" and "book (text reference E1)"; the recorded rationale is "Both captions mention drawing on a book labeled 'book' in scene S0 and S4".<br><br>At sampled intervals [22,23), [24,25), [25,26), [148,149), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "book (text reference E1)" and "C (text reference E0)"; the recorded rationale is "C draws on different objects (book vs. art paper).". |
| 26 / [26, 27] | #C C draws on the paper | 没有已接受结构化字段；原文回退 | 直接：R3=keep | At sampled intervals [2,3), [26,27), [35,36), [36,37), [41,42) (source assertion), C draws on the paper. |
| 27 / [27, 28] | #C C draws on the book | 没有已接受结构化字段；原文回退 | 直接：R31=keep | At sampled intervals [24,25), [25,26), [27,28), [115,116), [123,124), [128,129), [131,132), [149,150) (source assertion), C draws on the book. |
| 28 / [28, 29] | #C C draws on the drawing book | 没有已接受结构化字段；原文回退 | 直接：R32=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 29 / [29, 30] | #C C draws on a paper | 没有已接受结构化字段；原文回退 | 直接：R30=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 30 / [30, 31] | #C C draws a picture | {"actions": [{"actor": "C", "verb": "draws", "patient": "picture"}]} | 直接：R5=keep; R6=keep | At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.<br><br>At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture. |
| 31 / [31, 32] | #C C lifts the hand | 没有已接受结构化字段；原文回退 | 直接：R33=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 32 / [32, 33] | #C C holds the pencil | {"actions": [{"actor": "C", "verb": "holds", "patient": "pencil"}]} | 直接：R34=skip; R35=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 33 / [33, 34] | #C C wipes the face | 没有已接受结构化字段；原文回退 | 直接：R25=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 34 / [34, 35] | #C C moves the book | {"actions": [{"actor": "C", "verb": "moves", "patient": "book"}]} | 直接：R0=keep; R9=keep | At sampled intervals [0,1), [6,7), [34,35) (source assertion), C moves book.<br><br>At sampled intervals [6,7), [34,35) (source assertion), C moves the book. |
| 35 / [35, 36] | #C C draws on the paper | 没有已接受结构化字段；原文回退 | 直接：R3=keep | At sampled intervals [2,3), [26,27), [35,36), [36,37), [41,42) (source assertion), C draws on the paper. |
| 36 / [36, 37] | #C C draws on the paper  | 没有已接受结构化字段；原文回退 | 直接：R3=keep | At sampled intervals [2,3), [26,27), [35,36), [36,37), [41,42) (source assertion), C draws on the paper. |
| 37 / [37, 38] | #C C draws a picture of a dog  | {"actions": [{"actor": "C", "verb": "draws", "patient": "picture"}]} | 直接：R6=keep; R36=skip | At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture. |
| 38 / [38, 39] | #C C draws the sketch with the marker pen | {"actions": [{"actor": "C", "verb": "draws", "patient": "sketch"}]} | 直接：R37=skip; R38=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 39 / [39, 40] | #C C draws a picture | {"actions": [{"actor": "C", "verb": "draws", "patient": "picture"}]} | 直接：R5=keep; R6=keep | At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.<br><br>At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture. |
| 40 / [40, 41] | #C C colors a drawing | {"actions": [{"actor": "C", "verb": "colors", "patient": "drawing"}]} | 直接：R39=skip; R40=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 41 / [41, 42] | #C C draws on the paper | 没有已接受结构化字段；原文回退 | 直接：R3=keep | At sampled intervals [2,3), [26,27), [35,36), [36,37), [41,42) (source assertion), C draws on the paper. |
| 42 / [42, 43] | #C C holds the book | 没有已接受结构化字段；原文回退 | 直接：R41=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 88 / [88, 89] | #C C turns the art book | {"actions": [{"actor": "C", "verb": "turns", "patient": "art book"}]} | 直接：R42=skip; R43=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 89 / [89, 90] | #C C picks a pen | {"actions": [{"actor": "C", "verb": "picks", "patient": "pen"}]} | 直接：R44=skip; R45=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 90 / [90, 91] | #C C moves hand | 没有已接受结构化字段；原文回退 | 直接：R46=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 107 / [107, 108] | #C C holds the drawing book | {"actions": [{"actor": "C", "verb": "holds", "patient": "drawing book"}]} | 直接：R47=skip; R48=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 108 / [108, 109] | #C C draws a picture of a man  | {"actions": [{"actor": "C", "verb": "draws", "patient": "picture"}]} | 直接：R6=keep; R49=skip | At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture. |
| 109 / [109, 110] | #C C draws on the art with a pen  | 没有已接受结构化字段；原文回退 | 直接：R50=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 110 / [110, 111] | #C C draws on a drawing | 没有已接受结构化字段；原文回退 | 直接：R51=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 111 / [111, 112] | #C C holds the picture | {"actions": [{"actor": "C", "verb": "holds", "patient": "picture"}]} | 直接：R52=skip; R53=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 112 / [112, 113] | #C C turns around | 没有已接受结构化字段；原文回退 | 直接：R54=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 113 / [113, 114] | #C C draws the picture | {"actions": [{"actor": "C", "verb": "draws", "patient": "picture"}]} | 直接：R6=keep; R55=keep | At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.<br><br>At sampled intervals [113,114), [122,123), [125,126), [130,131) (source assertion), C draws the picture. |
| 114 / [114, 115] | #C C draws on a paper | 没有已接受结构化字段；原文回退 | 直接：R30=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 115 / [115, 116] | #C C draws on the book | 没有已接受结构化字段；原文回退 | 直接：R31=keep | At sampled intervals [24,25), [25,26), [27,28), [115,116), [123,124), [128,129), [131,132), [149,150) (source assertion), C draws on the book. |
| 116 / [116, 117] | #C C draws on a paper  | 没有已接受结构化字段；原文回退 | 直接：R30=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 117 / [117, 118] | #C C draws a picture | {"actions": [{"actor": "C", "verb": "draws", "patient": "picture"}]} | 直接：R5=keep; R6=keep | At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.<br><br>At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture. |
| 118 / [118, 119] | #C C holds the art | {"actions": [{"actor": "C", "verb": "holds", "patient": "art"}]} | 直接：R56=skip; R57=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 119 / [119, 120] | #C C draws on a paper  | 没有已接受结构化字段；原文回退 | 直接：R30=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 120 / [120, 121] | #C C paints a picture with a pen | {"actions": [{"actor": "C", "verb": "paints", "patient": "picture"}]} | 直接：R58=skip; R59=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 121 / [121, 122] | #C C draws a picture | {"actions": [{"actor": "C", "verb": "draws", "patient": "picture"}]} | 直接：R5=keep; R6=keep | At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.<br><br>At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture. |
| 122 / [122, 123] | #C C draws the picture | {"actions": [{"actor": "C", "verb": "draws", "patient": "picture"}]} | 直接：R6=keep; R55=keep | At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.<br><br>At sampled intervals [113,114), [122,123), [125,126), [130,131) (source assertion), C draws the picture. |
| 123 / [123, 124] | #C C draws on the book | 没有已接受结构化字段；原文回退 | 直接：R31=keep | At sampled intervals [24,25), [25,26), [27,28), [115,116), [123,124), [128,129), [131,132), [149,150) (source assertion), C draws on the book. |
| 124 / [124, 125] | #C C draws a picture | {"actions": [{"actor": "C", "verb": "draws", "patient": "picture"}]} | 直接：R5=keep; R6=keep | At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.<br><br>At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture. |
| 125 / [125, 126] | #C C draws the picture | {"actions": [{"actor": "C", "verb": "draws", "patient": "picture"}]} | 直接：R6=keep; R55=keep | At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.<br><br>At sampled intervals [113,114), [122,123), [125,126), [130,131) (source assertion), C draws the picture. |
| 126 / [126, 127] | #C C turns the art paper | {"actions": [{"actor": "C", "verb": "turns", "patient": "art paper"}]} | 直接：R60=skip; R61=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 127 / [127, 128] | #C C draws a dog | {"actions": [{"actor": "C", "verb": "draws", "patient": "dog"}]} | 直接：R62=skip; R63=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 128 / [128, 129] | #C C draws on the book | 没有已接受结构化字段；原文回退 | 直接：R31=keep | At sampled intervals [24,25), [25,26), [27,28), [115,116), [123,124), [128,129), [131,132), [149,150) (source assertion), C draws on the book. |
| 129 / [129, 130] | #C C draws a portrait | {"actions": [{"actor": "C", "verb": "draws", "patient": "portrait"}]} | 直接：R64=skip; R65=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 130 / [130, 131] | #C C draws the picture | {"actions": [{"actor": "C", "verb": "draws", "patient": "picture"}]} | 直接：R6=keep; R55=keep | At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.<br><br>At sampled intervals [113,114), [122,123), [125,126), [130,131) (source assertion), C draws the picture. |
| 131 / [131, 132] | #C C draws on the book | 没有已接受结构化字段；原文回退 | 直接：R31=keep | At sampled intervals [24,25), [25,26), [27,28), [115,116), [123,124), [128,129), [131,132), [149,150) (source assertion), C draws on the book. |
| 132 / [132, 133] | #C C draws the art | {"actions": [{"actor": "C", "verb": "draws", "patient": "art"}]} | 直接：R66=skip; R67=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 133 / [133, 134] | #C C draws the image | {"actions": [{"actor": "C", "verb": "draws", "patient": "image"}]} | 直接：R68=skip; R69=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 134 / [134, 135] | #C C draws a image | {"actions": [{"actor": "C", "verb": "draws", "patient": "image"}]} | 直接：R69=skip; R70=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 135 / [135, 136] | #C C moves the drawing book | {"actions": [{"actor": "C", "verb": "moves", "patient": "drawing book"}]} | 直接：R17=keep; R18=keep | At sampled intervals [12,13), [135,136) (source assertion), C moves the drawing book.<br><br>At sampled intervals [12,13), [135,136) (source assertion), C moves drawing book. |
| 136 / [136, 137] | #C C turns the lid | {"actions": [{"actor": "C", "verb": "turns", "patient": "lid"}]} | 直接：R71=skip; R72=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 137 / [137, 138] | #C C holds pen | {"actions": [{"actor": "C", "verb": "holds", "patient": "pen"}]} | 直接：R73=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 138 / [138, 139] | #C C touches the hands | 没有已接受结构化字段；原文回退 | 直接：R74=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 139 / [139, 140] | #C C holds the hand  | 没有已接受结构化字段；原文回退 | 直接：R75=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 140 / [140, 141] | #C C touches the pen | {"actions": [{"actor": "C", "verb": "touches", "patient": "pen"}]} | 直接：R76=skip; R77=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 141 / [141, 142] | #C C closes the pen | {"actions": [{"actor": "C", "verb": "closes", "patient": "pen"}]} | 直接：R78=skip; R79=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 142 / [142, 143] | #C C holds a pen with both hands | {"actions": [{"actor": "C", "verb": "holds", "patient": "pen"}]} | 直接：R73=skip; R80=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 143 / [143, 144] | #C C holds the pen | {"actions": [{"actor": "C", "verb": "holds", "patient": "pen"}]} | 直接：R73=skip; R81=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 144 / [144, 145] | #C C holds a pencil | {"actions": [{"actor": "C", "verb": "holds", "patient": "pencil"}]} | 直接：R35=skip; R82=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 145 / [145, 146] | #C C draws a picture  | {"actions": [{"actor": "C", "verb": "draws", "patient": "picture"}]} | 直接：R5=keep; R6=keep | At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.<br><br>At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture. |
| 146 / [146, 147] | #C C draws on the art | 没有已接受结构化字段；原文回退 | 直接：R83=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 147 / [147, 148] | #C C draws on the art book | 没有已接受结构化字段；原文回退 | 直接：R84=skip | ∅（本行没有直接证据或时序关联进入最终输入） |
| 148 / [148, 149] | #C C draws on the art paper  | 没有已接受结构化字段；原文回退 | 直接：R85=skip<br>时序：R88=group:R88; R91=group:R91; R92=group:R92; R95=group:R95 | At sampled intervals [148,149), [149,150) (source assertion), C draws on the art paper. The text memory associates this observation with C (text reference E0).<br><br>At sampled intervals [148,149), [149,150) (source assertion), The source describes "C (text reference E0)" as the subject of "draws on", with "book (text reference E1)" as its object.<br><br>At sampled intervals [10,11), [22,23), [24,25), [148,149) (text-inferred hypothesis), The text memory proposes "different_instance" between "C (text reference E2)" and "C (text reference E0)"; the recorded rationale is "Captions describe different actions on books in different scenes".<br><br>At sampled intervals [22,23), [24,25), [25,26), [148,149), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "book (text reference E1)" and "C (text reference E0)"; the recorded rationale is "C draws on different objects (book vs. art paper).". |
| 149 / [149, 150] | #C C draws on the book | 没有已接受结构化字段；原文回退 | 直接：R31=keep<br>时序：R88=group:R88; R89=group:R89; R91=group:R91; R93=group:R93; R94=group:R94; R95=group:R95 | At sampled intervals [24,25), [25,26), [27,28), [115,116), [123,124), [128,129), [131,132), [149,150) (source assertion), C draws on the book.<br><br>At sampled intervals [148,149), [149,150) (source assertion), C draws on the art paper. The text memory associates this observation with C (text reference E0).<br><br>At sampled intervals [149,150) (source assertion), C draws on the book. The text memory associates this observation with book (text reference E1).<br><br>At sampled intervals [148,149), [149,150) (source assertion), The source describes "C (text reference E0)" as the subject of "draws on", with "book (text reference E1)" as its object.<br><br>At sampled intervals [22,23), [24,25), [25,26), [149,150) (text-inferred hypothesis), The text memory proposes "same_instance" between "book (text reference E1)" and "book (text reference E1)"; the recorded rationale is "Both captions mention drawing on a book labeled 'book' in scene S0 and S4".<br><br>At sampled intervals [10,11), [22,23), [24,25), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "C (text reference E2)" and "book (text reference E1)"; the recorded rationale is "Captions describe different actions on books in different scenes".<br><br>At sampled intervals [22,23), [24,25), [25,26), [148,149), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "book (text reference E1)" and "C (text reference E0)"; the recorded rationale is "C draws on different objects (book vs. art paper).". |
| 150 / [150, 151] | #C C draws a picture | {"actions": [{"actor": "C", "verb": "draws", "patient": "picture"}]} | 直接：R5=keep; R6=keep | At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.<br><br>At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture. |
| 151 / [151, 152] | #C C draws a picture | {"actions": [{"actor": "C", "verb": "draws", "patient": "picture"}]} | 直接：R5=keep; R6=keep | At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.<br><br>At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture. |

## 逐条展开

下方保留F完整档案，区分“已接受字段”和“保留原文”；最终段落逐字复制，不人工改写。


### 采样点 0，区间 [0, 1]

**原始文本**

```text
#C C moves book
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "moves",
      "patient": "book"
    }
  ],
  "original_text": "#C C moves book"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R0：`keep`；C moves book.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R0：

```text
At sampled intervals [0,1), [6,7), [34,35) (source assertion), C moves book.
```


### 采样点 1，区间 [1, 2]

**原始文本**

```text
#C C rubs the paint
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "rubs",
      "patient": "paint"
    }
  ],
  "original_text": "#C C rubs the paint"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R1：`skip`；C rubs the paint.
- R2：`skip`；C rubs paint.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 2，区间 [2, 3]

**原始文本**

```text
#C C draws on the paper
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the paper"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R3：`keep`；C draws on the paper.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R3：

```text
At sampled intervals [2,3), [26,27), [35,36), [36,37), [41,42) (source assertion), C draws on the paper.
```


### 采样点 3，区间 [3, 4]

**原始文本**

```text
#C C draws on the painting paper
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the painting paper"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R4：`skip`；C draws on the painting paper.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 4，区间 [4, 5]

**原始文本**

```text
#C C draws a picture
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "picture"
    }
  ],
  "original_text": "#C C draws a picture"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R5：`keep`；C draws a picture.
- R6：`keep`；C draws picture.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R5：

```text
At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.
```

来自本行直接原文／事实；对应候选 R6：

```text
At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.
```


### 采样点 5，区间 [5, 6]

**原始文本**

```text
#C C adjusts the drawing
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "adjusts",
      "patient": "drawing"
    }
  ],
  "original_text": "#C C adjusts the drawing"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R7：`skip`；C adjusts the drawing.
- R8：`skip`；C adjusts drawing.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 6，区间 [6, 7]

**原始文本**

```text
#C C moves the book
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "moves",
      "patient": "book"
    }
  ],
  "original_text": "#C C moves the book"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R0：`keep`；C moves book.
- R9：`keep`；C moves the book.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R0：

```text
At sampled intervals [0,1), [6,7), [34,35) (source assertion), C moves book.
```

来自本行直接原文／事实；对应候选 R9：

```text
At sampled intervals [6,7), [34,35) (source assertion), C moves the book.
```


### 采样点 7，区间 [7, 8]

**原始文本**

```text
#C C moves hand on the table
```

**F实际保存**

```json
{
  "original_text": "#C C moves hand on the table"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R10：`skip`；C moves hand on the table.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 8，区间 [8, 9]

**原始文本**

```text
#C C moves a rubber on the table
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "moves",
      "patient": "rubber"
    }
  ],
  "original_text": "#C C moves a rubber on the table"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R11：`skip`；C moves a rubber on the table.
- R12：`skip`；C moves rubber.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 9，区间 [9, 10]

**原始文本**

```text
#C C draws on the drawing book with a pencil
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the drawing book with a pencil"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R13：`skip`；C draws on the drawing book with a pencil.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 10，区间 [10, 11]

**原始文本**

```text
#C C moves pen
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "moves",
      "patient": "pen"
    }
  ],
  "original_text": "#C C moves pen"
}
```

**K处理状态：** 直接候选跳过，仅保留上游时序关联。


直接候选：

- R14：`skip`；C moves pen.

上游时序关联候选：

- R92：`group:R92`；The text memory proposes "different_instance" between "C (text reference E2)" and "C (text reference E0)"; the recorded rationale is "Captions describe different actions on books in different scenes".
- R94：`group:R94`；The text memory proposes "different_instance" between "C (text reference E2)" and "book (text reference E1)"; the recorded rationale is "Captions describe different actions on books in different scenes".

**处理到最后的实际文本**

来自引用本行的上游时序条目；对应候选 R92：

```text
At sampled intervals [10,11), [22,23), [24,25), [148,149) (text-inferred hypothesis), The text memory proposes "different_instance" between "C (text reference E2)" and "C (text reference E0)"; the recorded rationale is "Captions describe different actions on books in different scenes".
```

来自引用本行的上游时序条目；对应候选 R94：

```text
At sampled intervals [10,11), [22,23), [24,25), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "C (text reference E2)" and "book (text reference E1)"; the recorded rationale is "Captions describe different actions on books in different scenes".
```


### 采样点 11，区间 [11, 12]

**原始文本**

```text
#C C moves the pencil to the right
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "moves",
      "patient": "pencil"
    }
  ],
  "original_text": "#C C moves the pencil to the right"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R15：`skip`；C moves the pencil to the right.
- R16：`skip`；C moves pencil.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 12，区间 [12, 13]

**原始文本**

```text
#C C moves the drawing book 
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "moves",
      "patient": "drawing book"
    }
  ],
  "original_text": "#C C moves the drawing book "
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R17：`keep`；C moves the drawing book.
- R18：`keep`；C moves drawing book.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R17：

```text
At sampled intervals [12,13), [135,136) (source assertion), C moves the drawing book.
```

来自本行直接原文／事实；对应候选 R18：

```text
At sampled intervals [12,13), [135,136) (source assertion), C moves drawing book.
```


### 采样点 13，区间 [13, 14]

**原始文本**

```text
#C C rubs a drawing
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "rubs",
      "patient": "drawing"
    }
  ],
  "original_text": "#C C rubs a drawing"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R19：`skip`；C rubs a drawing.
- R20：`skip`；C rubs drawing.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 14，区间 [14, 15]

**原始文本**

```text
#C C rubs a drawing with a rubber
```

**F实际保存**

```json
{
  "original_text": "#C C rubs a drawing with a rubber"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R21：`skip`；C rubs a drawing with a rubber.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 15，区间 [15, 16]

**原始文本**

```text
#C C rubs a drawing
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "rubs",
      "patient": "drawing"
    }
  ],
  "original_text": "#C C rubs a drawing"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R19：`skip`；C rubs a drawing.
- R20：`skip`；C rubs drawing.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 16，区间 [16, 17]

**原始文本**

```text
#C C rubs drawing
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "rubs",
      "patient": "drawing"
    }
  ],
  "original_text": "#C C rubs drawing"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R20：`skip`；C rubs drawing.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 17，区间 [17, 18]

**原始文本**

```text
#C C  moves hands 
```

**F实际保存**

```json
{
  "original_text": "#C C  moves hands "
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R22：`skip`；C  moves hands.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 18，区间 [18, 19]

**原始文本**

```text
#C C scratches the book
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "scratches",
      "patient": "book"
    }
  ],
  "original_text": "#C C scratches the book"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R23：`skip`；C scratches the book.
- R24：`skip`；C scratches book.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 19，区间 [19, 20]

**原始文本**

```text
#C C wipes the face
```

**F实际保存**

```json
{
  "original_text": "#C C wipes the face"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R25：`skip`；C wipes the face.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 20，区间 [20, 21]

**原始文本**

```text
#C C scratches her face
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "scratches",
      "patient": "face"
    }
  ],
  "original_text": "#C C scratches her face"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R26：`skip`；C scratches her face.
- R27：`skip`；C scratches face.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 21，区间 [21, 22]

**原始文本**

```text
#C C drops pencil
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "drops",
      "patient": "pencil"
    }
  ],
  "original_text": "#C C drops pencil"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R28：`skip`；C drops pencil.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 22，区间 [22, 23]

**原始文本**

```text
#C C draws on the book with a pen 
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the book with a pen "
}
```

**K处理状态：** 直接候选跳过，仅保留上游时序关联。


直接候选：

- R29：`skip`；C draws on the book with a pen.

上游时序关联候选：

- R86：`group:R86`；C draws on the book with a pen. The text memory associates this observation with C (text reference E2).
- R87：`group:R86`；C draws on the book with a pen. The text memory associates this observation with book (text reference E1).
- R90：`group:R90`；The source describes "C (text reference E2)" as the subject of "draws on", with "book (text reference E1)" as its object.
- R92：`group:R92`；The text memory proposes "different_instance" between "C (text reference E2)" and "C (text reference E0)"; the recorded rationale is "Captions describe different actions on books in different scenes".
- R93：`group:R93`；The text memory proposes "same_instance" between "book (text reference E1)" and "book (text reference E1)"; the recorded rationale is "Both captions mention drawing on a book labeled 'book' in scene S0 and S4".
- R94：`group:R94`；The text memory proposes "different_instance" between "C (text reference E2)" and "book (text reference E1)"; the recorded rationale is "Captions describe different actions on books in different scenes".
- R95：`group:R95`；The text memory proposes "different_instance" between "book (text reference E1)" and "C (text reference E0)"; the recorded rationale is "C draws on different objects (book vs. art paper).".

**处理到最后的实际文本**

来自引用本行的上游时序条目；对应候选 R86, R87：

```text
At sampled intervals [22,23), [25,26) (source assertion), C draws on the book with a pen. The text memory associates this observation with C (text reference E2). At sampled intervals [22,23), [25,26) (source assertion), C draws on the book with a pen. The text memory associates this observation with book (text reference E1).
```

来自引用本行的上游时序条目；对应候选 R90：

```text
At sampled intervals [22,23), [24,25), [25,26) (source assertion), The source describes "C (text reference E2)" as the subject of "draws on", with "book (text reference E1)" as its object.
```

来自引用本行的上游时序条目；对应候选 R92：

```text
At sampled intervals [10,11), [22,23), [24,25), [148,149) (text-inferred hypothesis), The text memory proposes "different_instance" between "C (text reference E2)" and "C (text reference E0)"; the recorded rationale is "Captions describe different actions on books in different scenes".
```

来自引用本行的上游时序条目；对应候选 R93：

```text
At sampled intervals [22,23), [24,25), [25,26), [149,150) (text-inferred hypothesis), The text memory proposes "same_instance" between "book (text reference E1)" and "book (text reference E1)"; the recorded rationale is "Both captions mention drawing on a book labeled 'book' in scene S0 and S4".
```

来自引用本行的上游时序条目；对应候选 R94：

```text
At sampled intervals [10,11), [22,23), [24,25), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "C (text reference E2)" and "book (text reference E1)"; the recorded rationale is "Captions describe different actions on books in different scenes".
```

来自引用本行的上游时序条目；对应候选 R95：

```text
At sampled intervals [22,23), [24,25), [25,26), [148,149), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "book (text reference E1)" and "C (text reference E0)"; the recorded rationale is "C draws on different objects (book vs. art paper).".
```


### 采样点 23，区间 [23, 24]

**原始文本**

```text
#C C draws on a paper
```

**F实际保存**

```json
{
  "original_text": "#C C draws on a paper"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R30：`skip`；C draws on a paper.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 24，区间 [24, 25]

**原始文本**

```text
#C C draws on the book
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the book"
}
```

**K处理状态：** 直接证据及上游时序关联均保留。


直接候选：

- R31：`keep`；C draws on the book.

上游时序关联候选：

- R90：`group:R90`；The source describes "C (text reference E2)" as the subject of "draws on", with "book (text reference E1)" as its object.
- R92：`group:R92`；The text memory proposes "different_instance" between "C (text reference E2)" and "C (text reference E0)"; the recorded rationale is "Captions describe different actions on books in different scenes".
- R93：`group:R93`；The text memory proposes "same_instance" between "book (text reference E1)" and "book (text reference E1)"; the recorded rationale is "Both captions mention drawing on a book labeled 'book' in scene S0 and S4".
- R94：`group:R94`；The text memory proposes "different_instance" between "C (text reference E2)" and "book (text reference E1)"; the recorded rationale is "Captions describe different actions on books in different scenes".
- R95：`group:R95`；The text memory proposes "different_instance" between "book (text reference E1)" and "C (text reference E0)"; the recorded rationale is "C draws on different objects (book vs. art paper).".
- R96：`skip`；An unresolved source observation states: "C draws on the book".

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R31：

```text
At sampled intervals [24,25), [25,26), [27,28), [115,116), [123,124), [128,129), [131,132), [149,150) (source assertion), C draws on the book.
```

来自引用本行的上游时序条目；对应候选 R90：

```text
At sampled intervals [22,23), [24,25), [25,26) (source assertion), The source describes "C (text reference E2)" as the subject of "draws on", with "book (text reference E1)" as its object.
```

来自引用本行的上游时序条目；对应候选 R92：

```text
At sampled intervals [10,11), [22,23), [24,25), [148,149) (text-inferred hypothesis), The text memory proposes "different_instance" between "C (text reference E2)" and "C (text reference E0)"; the recorded rationale is "Captions describe different actions on books in different scenes".
```

来自引用本行的上游时序条目；对应候选 R93：

```text
At sampled intervals [22,23), [24,25), [25,26), [149,150) (text-inferred hypothesis), The text memory proposes "same_instance" between "book (text reference E1)" and "book (text reference E1)"; the recorded rationale is "Both captions mention drawing on a book labeled 'book' in scene S0 and S4".
```

来自引用本行的上游时序条目；对应候选 R94：

```text
At sampled intervals [10,11), [22,23), [24,25), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "C (text reference E2)" and "book (text reference E1)"; the recorded rationale is "Captions describe different actions on books in different scenes".
```

来自引用本行的上游时序条目；对应候选 R95：

```text
At sampled intervals [22,23), [24,25), [25,26), [148,149), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "book (text reference E1)" and "C (text reference E0)"; the recorded rationale is "C draws on different objects (book vs. art paper).".
```


### 采样点 25，区间 [25, 26]

**原始文本**

```text
#C C draws on the book
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the book"
}
```

**K处理状态：** 直接证据及上游时序关联均保留。


直接候选：

- R31：`keep`；C draws on the book.

上游时序关联候选：

- R86：`group:R86`；C draws on the book with a pen. The text memory associates this observation with C (text reference E2).
- R87：`group:R86`；C draws on the book with a pen. The text memory associates this observation with book (text reference E1).
- R90：`group:R90`；The source describes "C (text reference E2)" as the subject of "draws on", with "book (text reference E1)" as its object.
- R93：`group:R93`；The text memory proposes "same_instance" between "book (text reference E1)" and "book (text reference E1)"; the recorded rationale is "Both captions mention drawing on a book labeled 'book' in scene S0 and S4".
- R95：`group:R95`；The text memory proposes "different_instance" between "book (text reference E1)" and "C (text reference E0)"; the recorded rationale is "C draws on different objects (book vs. art paper).".

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R31：

```text
At sampled intervals [24,25), [25,26), [27,28), [115,116), [123,124), [128,129), [131,132), [149,150) (source assertion), C draws on the book.
```

来自引用本行的上游时序条目；对应候选 R86, R87：

```text
At sampled intervals [22,23), [25,26) (source assertion), C draws on the book with a pen. The text memory associates this observation with C (text reference E2). At sampled intervals [22,23), [25,26) (source assertion), C draws on the book with a pen. The text memory associates this observation with book (text reference E1).
```

来自引用本行的上游时序条目；对应候选 R90：

```text
At sampled intervals [22,23), [24,25), [25,26) (source assertion), The source describes "C (text reference E2)" as the subject of "draws on", with "book (text reference E1)" as its object.
```

来自引用本行的上游时序条目；对应候选 R93：

```text
At sampled intervals [22,23), [24,25), [25,26), [149,150) (text-inferred hypothesis), The text memory proposes "same_instance" between "book (text reference E1)" and "book (text reference E1)"; the recorded rationale is "Both captions mention drawing on a book labeled 'book' in scene S0 and S4".
```

来自引用本行的上游时序条目；对应候选 R95：

```text
At sampled intervals [22,23), [24,25), [25,26), [148,149), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "book (text reference E1)" and "C (text reference E0)"; the recorded rationale is "C draws on different objects (book vs. art paper).".
```


### 采样点 26，区间 [26, 27]

**原始文本**

```text
#C C draws on the paper
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the paper"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R3：`keep`；C draws on the paper.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R3：

```text
At sampled intervals [2,3), [26,27), [35,36), [36,37), [41,42) (source assertion), C draws on the paper.
```


### 采样点 27，区间 [27, 28]

**原始文本**

```text
#C C draws on the book
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the book"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R31：`keep`；C draws on the book.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R31：

```text
At sampled intervals [24,25), [25,26), [27,28), [115,116), [123,124), [128,129), [131,132), [149,150) (source assertion), C draws on the book.
```


### 采样点 28，区间 [28, 29]

**原始文本**

```text
#C C draws on the drawing book
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the drawing book"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R32：`skip`；C draws on the drawing book.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 29，区间 [29, 30]

**原始文本**

```text
#C C draws on a paper
```

**F实际保存**

```json
{
  "original_text": "#C C draws on a paper"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R30：`skip`；C draws on a paper.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 30，区间 [30, 31]

**原始文本**

```text
#C C draws a picture
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "picture"
    }
  ],
  "original_text": "#C C draws a picture"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R5：`keep`；C draws a picture.
- R6：`keep`；C draws picture.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R5：

```text
At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.
```

来自本行直接原文／事实；对应候选 R6：

```text
At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.
```


### 采样点 31，区间 [31, 32]

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

- R33：`skip`；C lifts the hand.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 32，区间 [32, 33]

**原始文本**

```text
#C C holds the pencil
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "holds",
      "patient": "pencil"
    }
  ],
  "original_text": "#C C holds the pencil"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R34：`skip`；C holds the pencil.
- R35：`skip`；C holds pencil.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 33，区间 [33, 34]

**原始文本**

```text
#C C wipes the face
```

**F实际保存**

```json
{
  "original_text": "#C C wipes the face"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R25：`skip`；C wipes the face.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 34，区间 [34, 35]

**原始文本**

```text
#C C moves the book
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "moves",
      "patient": "book"
    }
  ],
  "original_text": "#C C moves the book"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R0：`keep`；C moves book.
- R9：`keep`；C moves the book.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R0：

```text
At sampled intervals [0,1), [6,7), [34,35) (source assertion), C moves book.
```

来自本行直接原文／事实；对应候选 R9：

```text
At sampled intervals [6,7), [34,35) (source assertion), C moves the book.
```


### 采样点 35，区间 [35, 36]

**原始文本**

```text
#C C draws on the paper
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the paper"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R3：`keep`；C draws on the paper.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R3：

```text
At sampled intervals [2,3), [26,27), [35,36), [36,37), [41,42) (source assertion), C draws on the paper.
```


### 采样点 36，区间 [36, 37]

**原始文本**

```text
#C C draws on the paper 
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the paper "
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R3：`keep`；C draws on the paper.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R3：

```text
At sampled intervals [2,3), [26,27), [35,36), [36,37), [41,42) (source assertion), C draws on the paper.
```


### 采样点 37，区间 [37, 38]

**原始文本**

```text
#C C draws a picture of a dog 
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "picture"
    }
  ],
  "original_text": "#C C draws a picture of a dog "
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R6：`keep`；C draws picture.
- R36：`skip`；C draws a picture of a dog.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R6：

```text
At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.
```


### 采样点 38，区间 [38, 39]

**原始文本**

```text
#C C draws the sketch with the marker pen
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "sketch"
    }
  ],
  "original_text": "#C C draws the sketch with the marker pen"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R37：`skip`；C draws the sketch with the marker pen.
- R38：`skip`；C draws sketch.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 39，区间 [39, 40]

**原始文本**

```text
#C C draws a picture
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "picture"
    }
  ],
  "original_text": "#C C draws a picture"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R5：`keep`；C draws a picture.
- R6：`keep`；C draws picture.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R5：

```text
At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.
```

来自本行直接原文／事实；对应候选 R6：

```text
At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.
```


### 采样点 40，区间 [40, 41]

**原始文本**

```text
#C C colors a drawing
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "colors",
      "patient": "drawing"
    }
  ],
  "original_text": "#C C colors a drawing"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R39：`skip`；C colors a drawing.
- R40：`skip`；C colors drawing.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 41，区间 [41, 42]

**原始文本**

```text
#C C draws on the paper
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the paper"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R3：`keep`；C draws on the paper.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R3：

```text
At sampled intervals [2,3), [26,27), [35,36), [36,37), [41,42) (source assertion), C draws on the paper.
```


### 采样点 42，区间 [42, 43]

**原始文本**

```text
#C C holds the book
```

**F实际保存**

```json
{
  "original_text": "#C C holds the book"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R41：`skip`；C holds the book.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 88，区间 [88, 89]

**原始文本**

```text
#C C turns the art book
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "turns",
      "patient": "art book"
    }
  ],
  "original_text": "#C C turns the art book"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R42：`skip`；C turns the art book.
- R43：`skip`；C turns art book.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 89，区间 [89, 90]

**原始文本**

```text
#C C picks a pen
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "picks",
      "patient": "pen"
    }
  ],
  "original_text": "#C C picks a pen"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R44：`skip`；C picks a pen.
- R45：`skip`；C picks pen.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 90，区间 [90, 91]

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

- R46：`skip`；C moves hand.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 107，区间 [107, 108]

**原始文本**

```text
#C C holds the drawing book
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "holds",
      "patient": "drawing book"
    }
  ],
  "original_text": "#C C holds the drawing book"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R47：`skip`；C holds the drawing book.
- R48：`skip`；C holds drawing book.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 108，区间 [108, 109]

**原始文本**

```text
#C C draws a picture of a man 
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "picture"
    }
  ],
  "original_text": "#C C draws a picture of a man "
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R6：`keep`；C draws picture.
- R49：`skip`；C draws a picture of a man.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R6：

```text
At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.
```


### 采样点 109，区间 [109, 110]

**原始文本**

```text
#C C draws on the art with a pen 
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the art with a pen "
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R50：`skip`；C draws on the art with a pen.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 110，区间 [110, 111]

**原始文本**

```text
#C C draws on a drawing
```

**F实际保存**

```json
{
  "original_text": "#C C draws on a drawing"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R51：`skip`；C draws on a drawing.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 111，区间 [111, 112]

**原始文本**

```text
#C C holds the picture
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "holds",
      "patient": "picture"
    }
  ],
  "original_text": "#C C holds the picture"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R52：`skip`；C holds the picture.
- R53：`skip`；C holds picture.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 112，区间 [112, 113]

**原始文本**

```text
#C C turns around
```

**F实际保存**

```json
{
  "original_text": "#C C turns around"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R54：`skip`；C turns around.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 113，区间 [113, 114]

**原始文本**

```text
#C C draws the picture
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "picture"
    }
  ],
  "original_text": "#C C draws the picture"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R6：`keep`；C draws picture.
- R55：`keep`；C draws the picture.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R6：

```text
At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.
```

来自本行直接原文／事实；对应候选 R55：

```text
At sampled intervals [113,114), [122,123), [125,126), [130,131) (source assertion), C draws the picture.
```


### 采样点 114，区间 [114, 115]

**原始文本**

```text
#C C draws on a paper
```

**F实际保存**

```json
{
  "original_text": "#C C draws on a paper"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R30：`skip`；C draws on a paper.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 115，区间 [115, 116]

**原始文本**

```text
#C C draws on the book
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the book"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R31：`keep`；C draws on the book.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R31：

```text
At sampled intervals [24,25), [25,26), [27,28), [115,116), [123,124), [128,129), [131,132), [149,150) (source assertion), C draws on the book.
```


### 采样点 116，区间 [116, 117]

**原始文本**

```text
#C C draws on a paper 
```

**F实际保存**

```json
{
  "original_text": "#C C draws on a paper "
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R30：`skip`；C draws on a paper.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 117，区间 [117, 118]

**原始文本**

```text
#C C draws a picture
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "picture"
    }
  ],
  "original_text": "#C C draws a picture"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R5：`keep`；C draws a picture.
- R6：`keep`；C draws picture.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R5：

```text
At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.
```

来自本行直接原文／事实；对应候选 R6：

```text
At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.
```


### 采样点 118，区间 [118, 119]

**原始文本**

```text
#C C holds the art
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "holds",
      "patient": "art"
    }
  ],
  "original_text": "#C C holds the art"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R56：`skip`；C holds the art.
- R57：`skip`；C holds art.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 119，区间 [119, 120]

**原始文本**

```text
#C C draws on a paper 
```

**F实际保存**

```json
{
  "original_text": "#C C draws on a paper "
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R30：`skip`；C draws on a paper.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 120，区间 [120, 121]

**原始文本**

```text
#C C paints a picture with a pen
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "paints",
      "patient": "picture"
    }
  ],
  "original_text": "#C C paints a picture with a pen"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R58：`skip`；C paints a picture with a pen.
- R59：`skip`；C paints picture.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 121，区间 [121, 122]

**原始文本**

```text
#C C draws a picture
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "picture"
    }
  ],
  "original_text": "#C C draws a picture"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R5：`keep`；C draws a picture.
- R6：`keep`；C draws picture.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R5：

```text
At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.
```

来自本行直接原文／事实；对应候选 R6：

```text
At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.
```


### 采样点 122，区间 [122, 123]

**原始文本**

```text
#C C draws the picture
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "picture"
    }
  ],
  "original_text": "#C C draws the picture"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R6：`keep`；C draws picture.
- R55：`keep`；C draws the picture.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R6：

```text
At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.
```

来自本行直接原文／事实；对应候选 R55：

```text
At sampled intervals [113,114), [122,123), [125,126), [130,131) (source assertion), C draws the picture.
```


### 采样点 123，区间 [123, 124]

**原始文本**

```text
#C C draws on the book
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the book"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R31：`keep`；C draws on the book.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R31：

```text
At sampled intervals [24,25), [25,26), [27,28), [115,116), [123,124), [128,129), [131,132), [149,150) (source assertion), C draws on the book.
```


### 采样点 124，区间 [124, 125]

**原始文本**

```text
#C C draws a picture
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "picture"
    }
  ],
  "original_text": "#C C draws a picture"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R5：`keep`；C draws a picture.
- R6：`keep`；C draws picture.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R5：

```text
At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.
```

来自本行直接原文／事实；对应候选 R6：

```text
At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.
```


### 采样点 125，区间 [125, 126]

**原始文本**

```text
#C C draws the picture
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "picture"
    }
  ],
  "original_text": "#C C draws the picture"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R6：`keep`；C draws picture.
- R55：`keep`；C draws the picture.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R6：

```text
At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.
```

来自本行直接原文／事实；对应候选 R55：

```text
At sampled intervals [113,114), [122,123), [125,126), [130,131) (source assertion), C draws the picture.
```


### 采样点 126，区间 [126, 127]

**原始文本**

```text
#C C turns the art paper
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "turns",
      "patient": "art paper"
    }
  ],
  "original_text": "#C C turns the art paper"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R60：`skip`；C turns the art paper.
- R61：`skip`；C turns art paper.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 127，区间 [127, 128]

**原始文本**

```text
#C C draws a dog
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "dog"
    }
  ],
  "original_text": "#C C draws a dog"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R62：`skip`；C draws a dog.
- R63：`skip`；C draws dog.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 128，区间 [128, 129]

**原始文本**

```text
#C C draws on the book
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the book"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R31：`keep`；C draws on the book.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R31：

```text
At sampled intervals [24,25), [25,26), [27,28), [115,116), [123,124), [128,129), [131,132), [149,150) (source assertion), C draws on the book.
```


### 采样点 129，区间 [129, 130]

**原始文本**

```text
#C C draws a portrait
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "portrait"
    }
  ],
  "original_text": "#C C draws a portrait"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R64：`skip`；C draws a portrait.
- R65：`skip`；C draws portrait.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 130，区间 [130, 131]

**原始文本**

```text
#C C draws the picture
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "picture"
    }
  ],
  "original_text": "#C C draws the picture"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R6：`keep`；C draws picture.
- R55：`keep`；C draws the picture.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R6：

```text
At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.
```

来自本行直接原文／事实；对应候选 R55：

```text
At sampled intervals [113,114), [122,123), [125,126), [130,131) (source assertion), C draws the picture.
```


### 采样点 131，区间 [131, 132]

**原始文本**

```text
#C C draws on the book
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the book"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R31：`keep`；C draws on the book.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R31：

```text
At sampled intervals [24,25), [25,26), [27,28), [115,116), [123,124), [128,129), [131,132), [149,150) (source assertion), C draws on the book.
```


### 采样点 132，区间 [132, 133]

**原始文本**

```text
#C C draws the art
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "art"
    }
  ],
  "original_text": "#C C draws the art"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R66：`skip`；C draws the art.
- R67：`skip`；C draws art.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 133，区间 [133, 134]

**原始文本**

```text
#C C draws the image
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "image"
    }
  ],
  "original_text": "#C C draws the image"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R68：`skip`；C draws the image.
- R69：`skip`；C draws image.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 134，区间 [134, 135]

**原始文本**

```text
#C C draws a image
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "image"
    }
  ],
  "original_text": "#C C draws a image"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R69：`skip`；C draws image.
- R70：`skip`；C draws a image.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 135，区间 [135, 136]

**原始文本**

```text
#C C moves the drawing book
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "moves",
      "patient": "drawing book"
    }
  ],
  "original_text": "#C C moves the drawing book"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R17：`keep`；C moves the drawing book.
- R18：`keep`；C moves drawing book.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R17：

```text
At sampled intervals [12,13), [135,136) (source assertion), C moves the drawing book.
```

来自本行直接原文／事实；对应候选 R18：

```text
At sampled intervals [12,13), [135,136) (source assertion), C moves drawing book.
```


### 采样点 136，区间 [136, 137]

**原始文本**

```text
#C C turns the lid
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "turns",
      "patient": "lid"
    }
  ],
  "original_text": "#C C turns the lid"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R71：`skip`；C turns the lid.
- R72：`skip`；C turns lid.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 137，区间 [137, 138]

**原始文本**

```text
#C C holds pen
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "holds",
      "patient": "pen"
    }
  ],
  "original_text": "#C C holds pen"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R73：`skip`；C holds pen.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 138，区间 [138, 139]

**原始文本**

```text
#C C touches the hands
```

**F实际保存**

```json
{
  "original_text": "#C C touches the hands"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R74：`skip`；C touches the hands.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 139，区间 [139, 140]

**原始文本**

```text
#C C holds the hand 
```

**F实际保存**

```json
{
  "original_text": "#C C holds the hand "
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R75：`skip`；C holds the hand.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 140，区间 [140, 141]

**原始文本**

```text
#C C touches the pen
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "touches",
      "patient": "pen"
    }
  ],
  "original_text": "#C C touches the pen"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R76：`skip`；C touches the pen.
- R77：`skip`；C touches pen.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 141，区间 [141, 142]

**原始文本**

```text
#C C closes the pen
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "closes",
      "patient": "pen"
    }
  ],
  "original_text": "#C C closes the pen"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R78：`skip`；C closes the pen.
- R79：`skip`；C closes pen.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 142，区间 [142, 143]

**原始文本**

```text
#C C holds a pen with both hands
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "holds",
      "patient": "pen"
    }
  ],
  "original_text": "#C C holds a pen with both hands"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R73：`skip`；C holds pen.
- R80：`skip`；C holds a pen with both hands.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 143，区间 [143, 144]

**原始文本**

```text
#C C holds the pen
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "holds",
      "patient": "pen"
    }
  ],
  "original_text": "#C C holds the pen"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R73：`skip`；C holds pen.
- R81：`skip`；C holds the pen.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 144，区间 [144, 145]

**原始文本**

```text
#C C holds a pencil
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "holds",
      "patient": "pencil"
    }
  ],
  "original_text": "#C C holds a pencil"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R35：`skip`；C holds pencil.
- R82：`skip`；C holds a pencil.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 145，区间 [145, 146]

**原始文本**

```text
#C C draws a picture 
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "picture"
    }
  ],
  "original_text": "#C C draws a picture "
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R5：`keep`；C draws a picture.
- R6：`keep`；C draws picture.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R5：

```text
At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.
```

来自本行直接原文／事实；对应候选 R6：

```text
At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.
```


### 采样点 146，区间 [146, 147]

**原始文本**

```text
#C C draws on the art
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the art"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R83：`skip`；C draws on the art.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 147，区间 [147, 148]

**原始文本**

```text
#C C draws on the art book
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the art book"
}
```

**K处理状态：** 未进入最终输入。


直接候选：

- R84：`skip`；C draws on the art book.

**处理到最后的实际文本**

本行没有证据或关联时序条目进入最终输入。

### 采样点 148，区间 [148, 149]

**原始文本**

```text
#C C draws on the art paper 
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the art paper "
}
```

**K处理状态：** 直接候选跳过，仅保留上游时序关联。


直接候选：

- R85：`skip`；C draws on the art paper.

上游时序关联候选：

- R88：`group:R88`；C draws on the art paper. The text memory associates this observation with C (text reference E0).
- R91：`group:R91`；The source describes "C (text reference E0)" as the subject of "draws on", with "book (text reference E1)" as its object.
- R92：`group:R92`；The text memory proposes "different_instance" between "C (text reference E2)" and "C (text reference E0)"; the recorded rationale is "Captions describe different actions on books in different scenes".
- R95：`group:R95`；The text memory proposes "different_instance" between "book (text reference E1)" and "C (text reference E0)"; the recorded rationale is "C draws on different objects (book vs. art paper).".

**处理到最后的实际文本**

来自引用本行的上游时序条目；对应候选 R88：

```text
At sampled intervals [148,149), [149,150) (source assertion), C draws on the art paper. The text memory associates this observation with C (text reference E0).
```

来自引用本行的上游时序条目；对应候选 R91：

```text
At sampled intervals [148,149), [149,150) (source assertion), The source describes "C (text reference E0)" as the subject of "draws on", with "book (text reference E1)" as its object.
```

来自引用本行的上游时序条目；对应候选 R92：

```text
At sampled intervals [10,11), [22,23), [24,25), [148,149) (text-inferred hypothesis), The text memory proposes "different_instance" between "C (text reference E2)" and "C (text reference E0)"; the recorded rationale is "Captions describe different actions on books in different scenes".
```

来自引用本行的上游时序条目；对应候选 R95：

```text
At sampled intervals [22,23), [24,25), [25,26), [148,149), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "book (text reference E1)" and "C (text reference E0)"; the recorded rationale is "C draws on different objects (book vs. art paper).".
```


### 采样点 149，区间 [149, 150]

**原始文本**

```text
#C C draws on the book
```

**F实际保存**

```json
{
  "original_text": "#C C draws on the book"
}
```

**K处理状态：** 直接证据及上游时序关联均保留。


直接候选：

- R31：`keep`；C draws on the book.

上游时序关联候选：

- R88：`group:R88`；C draws on the art paper. The text memory associates this observation with C (text reference E0).
- R89：`group:R89`；C draws on the book. The text memory associates this observation with book (text reference E1).
- R91：`group:R91`；The source describes "C (text reference E0)" as the subject of "draws on", with "book (text reference E1)" as its object.
- R93：`group:R93`；The text memory proposes "same_instance" between "book (text reference E1)" and "book (text reference E1)"; the recorded rationale is "Both captions mention drawing on a book labeled 'book' in scene S0 and S4".
- R94：`group:R94`；The text memory proposes "different_instance" between "C (text reference E2)" and "book (text reference E1)"; the recorded rationale is "Captions describe different actions on books in different scenes".
- R95：`group:R95`；The text memory proposes "different_instance" between "book (text reference E1)" and "C (text reference E0)"; the recorded rationale is "C draws on different objects (book vs. art paper).".

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R31：

```text
At sampled intervals [24,25), [25,26), [27,28), [115,116), [123,124), [128,129), [131,132), [149,150) (source assertion), C draws on the book.
```

来自引用本行的上游时序条目；对应候选 R88：

```text
At sampled intervals [148,149), [149,150) (source assertion), C draws on the art paper. The text memory associates this observation with C (text reference E0).
```

来自引用本行的上游时序条目；对应候选 R89：

```text
At sampled intervals [149,150) (source assertion), C draws on the book. The text memory associates this observation with book (text reference E1).
```

来自引用本行的上游时序条目；对应候选 R91：

```text
At sampled intervals [148,149), [149,150) (source assertion), The source describes "C (text reference E0)" as the subject of "draws on", with "book (text reference E1)" as its object.
```

来自引用本行的上游时序条目；对应候选 R93：

```text
At sampled intervals [22,23), [24,25), [25,26), [149,150) (text-inferred hypothesis), The text memory proposes "same_instance" between "book (text reference E1)" and "book (text reference E1)"; the recorded rationale is "Both captions mention drawing on a book labeled 'book' in scene S0 and S4".
```

来自引用本行的上游时序条目；对应候选 R94：

```text
At sampled intervals [10,11), [22,23), [24,25), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "C (text reference E2)" and "book (text reference E1)"; the recorded rationale is "Captions describe different actions on books in different scenes".
```

来自引用本行的上游时序条目；对应候选 R95：

```text
At sampled intervals [22,23), [24,25), [25,26), [148,149), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "book (text reference E1)" and "C (text reference E0)"; the recorded rationale is "C draws on different objects (book vs. art paper).".
```


### 采样点 150，区间 [150, 151]

**原始文本**

```text
#C C draws a picture
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "picture"
    }
  ],
  "original_text": "#C C draws a picture"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R5：`keep`；C draws a picture.
- R6：`keep`；C draws picture.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R5：

```text
At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.
```

来自本行直接原文／事实；对应候选 R6：

```text
At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.
```


### 采样点 151，区间 [151, 152]

**原始文本**

```text
#C C draws a picture
```

**F实际保存**

```json
{
  "actions": [
    {
      "actor": "C",
      "verb": "draws",
      "patient": "picture"
    }
  ],
  "original_text": "#C C draws a picture"
}
```

**K处理状态：** 直接证据保留。


直接候选：

- R5：`keep`；C draws a picture.
- R6：`keep`；C draws picture.

**处理到最后的实际文本**

来自本行直接原文／事实；对应候选 R5：

```text
At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.
```

来自本行直接原文／事实；对应候选 R6：

```text
At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.
```

