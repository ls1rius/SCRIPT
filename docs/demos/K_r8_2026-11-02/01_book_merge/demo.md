# 书作为绘画表面：有真实分组

**逐帧看修改前后：** [全部原始文本→F解构→K取舍→最终实际段落](19_per_frame_before_after.md)，也可看[CSV表格](21_per_frame_before_after.csv)。

**先看采样点：** [全部已选采样索引、逐条原始文本、F实际解构、K逐条取舍](15_sampling_trace.md)。

来源题目ID：`916476da-a699-4132-b5cb-ae992e647b1b`。真实原文，不补写视频事实。

## 1. 原问题与全部选项

Summarize the primary interaction between c and the book throughout the video, while explaining the importance of this interaction relative to c's main action.

A. C uses the book as a surface to draw on.

B. In their free time, c frequently utilizes the informative book to enjoy reading.

C. Casually, c utilizes the informative book as a valuable resource to compose writing.

D. C uses the book to prop up his/her hand.

E. Casually, c picks up the book, and then uses it to cover his/her face discreetly.


## 2. 结构化档案与候选

共有91个逐采样观测、97条候选。完整实体／状态／关系与事实见[结构化档案](02_entities_and_facts.json)，全部候选及来源见[候选池](03_candidate_pool.json)，规划模型实际读取[此输入](04_actual_planner_input.json)。

局部实体档案如下；同名实体引用保持不同，身份关系属于文本推断：

```json
[
  {
    "id": "E1",
    "label": "book",
    "attributes": [],
    "scene_ids": [
      "S4",
      "S0"
    ],
    "state_ids": [
      "T1",
      "T3"
    ]
  },
  {
    "id": "E2",
    "label": "C",
    "attributes": [],
    "scene_ids": [
      "S0"
    ],
    "state_ids": [
      "T2"
    ]
  },
  {
    "id": "E0",
    "label": "C",
    "attributes": [],
    "scene_ids": [
      "S4"
    ],
    "state_ids": [
      "T0"
    ]
  }
]
```


## 3. 规划后的保留、跳过与分组

保留19条、跳过78条，模型分组1组。以下列出全部保留正文；时间和来源完整保存在[保留证据JSON](07_retained_evidence.json)及[来源审计](08_plan_audit.json)。

| ID | 决策 | 原始证据正文 |
|---|---|---|
| R0 | keep | C moves book. |
| R3 | keep | C draws on the paper. |
| R5 | keep | C draws a picture. |
| R6 | keep | C draws picture. |
| R9 | keep | C moves the book. |
| R17 | keep | C moves the drawing book. |
| R18 | keep | C moves drawing book. |
| R31 | keep | C draws on the book. |
| R55 | keep | C draws the picture. |
| R86 | group:R86 | C draws on the book with a pen. The text memory associates this observation with C (text reference E2). |
| R87 | group:R86 | C draws on the book with a pen. The text memory associates this observation with book (text reference E1). |
| R88 | group:R88 | C draws on the art paper. The text memory associates this observation with C (text reference E0). |
| R89 | group:R89 | C draws on the book. The text memory associates this observation with book (text reference E1). |
| R90 | group:R90 | The source describes "C (text reference E2)" as the subject of "draws on", with "book (text reference E1)" as its object. |
| R91 | group:R91 | The source describes "C (text reference E0)" as the subject of "draws on", with "book (text reference E1)" as its object. |
| R92 | group:R92 | The text memory proposes "different_instance" between "C (text reference E2)" and "C (text reference E0)"; the recorded rationale is "Captions describe different actions on books in different scenes". |
| R93 | group:R93 | The text memory proposes "same_instance" between "book (text reference E1)" and "book (text reference E1)"; the recorded rationale is "Both captions mention drawing on a book labeled 'book' in scene S0 and S4". |
| R94 | group:R94 | The text memory proposes "different_instance" between "C (text reference E2)" and "book (text reference E1)"; the recorded rationale is "Captions describe different actions on books in different scenes". |
| R95 | group:R95 | The text memory proposes "different_instance" between "book (text reference E1)" and "C (text reference E0)"; the recorded rationale is "C draws on different objects (book vs. art paper).". |

完整程序分区：

```json
{
  "keep": [
    "R0",
    "R3",
    "R5",
    "R6",
    "R9",
    "R17",
    "R18",
    "R31",
    "R55",
    "R88",
    "R89",
    "R90",
    "R91",
    "R92",
    "R93",
    "R94",
    "R95"
  ],
  "skip": [
    "R1",
    "R2",
    "R4",
    "R7",
    "R8",
    "R10",
    "R11",
    "R12",
    "R13",
    "R14",
    "R15",
    "R16",
    "R19",
    "R20",
    "R21",
    "R22",
    "R23",
    "R24",
    "R25",
    "R26",
    "R27",
    "R28",
    "R29",
    "R30",
    "R32",
    "R33",
    "R34",
    "R35",
    "R36",
    "R37",
    "R38",
    "R39",
    "R40",
    "R41",
    "R42",
    "R43",
    "R44",
    "R45",
    "R46",
    "R47",
    "R48",
    "R49",
    "R50",
    "R51",
    "R52",
    "R53",
    "R54",
    "R56",
    "R57",
    "R58",
    "R59",
    "R60",
    "R61",
    "R62",
    "R63",
    "R64",
    "R65",
    "R66",
    "R67",
    "R68",
    "R69",
    "R70",
    "R71",
    "R72",
    "R73",
    "R74",
    "R75",
    "R76",
    "R77",
    "R78",
    "R79",
    "R80",
    "R81",
    "R82",
    "R83",
    "R84",
    "R85",
    "R96"
  ],
  "uncertain": [],
  "merge_groups": [
    [
      "R86",
      "R87"
    ]
  ]
}
```

分组只将原陈述放进同一段落，不生成新的归纳事实、不删除实体引用。以下是实际被分组的原文：

- R86: C draws on the book with a pen. The text memory associates this observation with C (text reference E2).
- R87: C draws on the book with a pen. The text memory associates this observation with book (text reference E1).

## 4. 真正送给最终回答模型的信息

实际输入为1534token，完整JSON见[最终调用输入](10_actual_final_model_input.json)，消息列表见[system/user消息](11_final_messages.json)，下方是经本地同一tokenizer还原、已核对token数的完整聊天模板文本。没有省略任何部分。

```text
<|im_start|>system
You are presented with a textual description of a video clip, it consists of frame captions sparsely sampled from the video. Your task is to answer a question related to this video, choosing the correct option out of five possible answers. Please provide the answer with a single-letter (A, B, C, D, E)It is crucial that you imagine the visual scene as vividly as possible to enhance the accuracy of your response. After selecting your answer, rate your confidence level in this choice on a scale from 1 to 100, where 1 indicates low confidence and 100 signifies high confidence. Please provide a concise one-sentence explanation for your chosen answer. If you are uncertain about the correct option, select the one that seems closest to being correct. <|im_end|>
<|im_start|>user
Here are a few examples. 
None 

###

  Description: These are sampled source observations, not a complete video record. Text identity and change hypotheses are not verified physical identities or causes. Listed intervals are separate observations, not proof of continuous duration or event counts. Question and options are hypotheses, not evidence. Missing evidence is not evidence of absence.
At sampled intervals [0,1), [6,7), [34,35) (source assertion), C moves book.
At sampled intervals [2,3), [26,27), [35,36), [36,37), [41,42) (source assertion), C draws on the paper.
At sampled intervals [4,5), [30,31), [39,40), [117,118), [121,122), [124,125), [145,146), [150,151), [151,152) (source assertion), C draws a picture.
At sampled intervals [4,5), [30,31), [37,38), [39,40), [108,109), [113,114), [117,118), [121,122), [122,123), [124,125), [125,126), [130,131), [145,146), [150,151), [151,152) (source assertion), C draws picture.
At sampled intervals [6,7), [34,35) (source assertion), C moves the book.
At sampled intervals [10,11), [22,23), [24,25), [148,149) (text-inferred hypothesis), The text memory proposes "different_instance" between "C (text reference E2)" and "C (text reference E0)"; the recorded rationale is "Captions describe different actions on books in different scenes".
At sampled intervals [10,11), [22,23), [24,25), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "C (text reference E2)" and "book (text reference E1)"; the recorded rationale is "Captions describe different actions on books in different scenes".
At sampled intervals [12,13), [135,136) (source assertion), C moves the drawing book.
At sampled intervals [12,13), [135,136) (source assertion), C moves drawing book.
At sampled intervals [22,23), [25,26) (source assertion), C draws on the book with a pen. The text memory associates this observation with C (text reference E2). At sampled intervals [22,23), [25,26) (source assertion), C draws on the book with a pen. The text memory associates this observation with book (text reference E1).
At sampled intervals [22,23), [24,25), [25,26) (source assertion), The source describes "C (text reference E2)" as the subject of "draws on", with "book (text reference E1)" as its object.
At sampled intervals [22,23), [24,25), [25,26), [149,150) (text-inferred hypothesis), The text memory proposes "same_instance" between "book (text reference E1)" and "book (text reference E1)"; the recorded rationale is "Both captions mention drawing on a book labeled 'book' in scene S0 and S4".
At sampled intervals [22,23), [24,25), [25,26), [148,149), [149,150) (text-inferred hypothesis), The text memory proposes "different_instance" between "book (text reference E1)" and "C (text reference E0)"; the recorded rationale is "C draws on different objects (book vs. art paper).".
At sampled intervals [24,25), [25,26), [27,28), [115,116), [123,124), [128,129), [131,132), [149,150) (source assertion), C draws on the book.
At sampled intervals [113,114), [122,123), [125,126), [130,131) (source assertion), C draws the picture.
At sampled intervals [148,149), [149,150) (source assertion), C draws on the art paper. The text memory associates this observation with C (text reference E0).
At sampled intervals [148,149), [149,150) (source assertion), The source describes "C (text reference E0)" as the subject of "draws on", with "book (text reference E1)" as its object.
At sampled intervals [149,150) (source assertion), C draws on the book. The text memory associates this observation with book (text reference E1).

###

 Questions: Summarize the primary interaction between c and the book throughout the video, while explaining the importance of this interaction relative to c's main action. 
 Options: 
 A: C uses the book as a surface to draw on. 
 B: In their free time, c frequently utilizes the informative book to enjoy reading. 
 C: Casually, c utilizes the informative book as a valuable resource to compose writing. 
 D: C uses the book to prop up his/her hand. 
 E: Casually, c picks up the book, and then uses it to cover his/her face discreetly. 

###

 The prediction, explanation, confidence is (please response in the format of 'prediction: 
 explanation: 
 confidence: 
 '):<|im_end|>
<|im_start|>assistant

```


最终输出另外受到`prediction: A`至`prediction: E`的解码约束；这不属于输入证据，也不会把标准答案传给模型。


实际生成：`prediction: A`。这里不使用标准答案筛选或修正demo。

