# 遛狗：筛选后保留6条

**逐帧看修改前后：** [全部原始文本→F解构→K取舍→最终实际段落](19_per_frame_before_after.md)，也可看[CSV表格](21_per_frame_before_after.csv)。

**先看采样点：** [全部已选采样索引、逐条原始文本、F实际解构、K逐条取舍](15_sampling_trace.md)。

来源题目ID：`15bae307-992b-4359-9fe0-3f622f957d42`。真实原文，不补写视频事实。

## 1. 原问题与全部选项

In your own words, summarize the key actions c repeatedly performs throughout the video without simply listing them out.

A. The individual, c, continuously and repeatedly ceases their walking motion.

B. Continuously, c repeatedly waves her right hand multiple times.

C. Continuously, c takes plastic paper using her right hand, performing the action repeatedly.

D. C repeatedly takes plastic paper with her left hand.

E. C repeatedly touches her hair and walks her dog.


## 2. 结构化档案与候选

共有90个逐采样观测、56条候选。完整实体／状态／关系与事实见[结构化档案](02_entities_and_facts.json)，全部候选及来源见[候选池](03_candidate_pool.json)，规划模型实际读取[此输入](04_actual_planner_input.json)。

局部实体档案如下；同名实体引用保持不同，身份关系属于文本推断：

```json
[
  {
    "id": "E0",
    "label": "C",
    "attributes": [],
    "scene_ids": [
      "S0"
    ],
    "state_ids": [
      "T0"
    ]
  },
  {
    "id": "E1",
    "label": "dog",
    "attributes": [],
    "scene_ids": [
      "S0"
    ],
    "state_ids": [
      "T1"
    ]
  }
]
```


## 3. 规划后的保留、跳过与分组

保留6条、跳过50条，模型分组0组。以下列出全部保留正文；时间和来源完整保存在[保留证据JSON](07_retained_evidence.json)及[来源审计](08_plan_audit.json)。

| ID | 决策 | 原始证据正文 |
|---|---|---|
| R0 | keep | C walks with the dog. |
| R3 | keep | C walks dog. |
| R7 | keep | C pulls dog leash. |
| R15 | keep | C holds dog leash. |
| R53 | keep | C walks dog on the road. The text memory associates this observation with C (text reference E0). |
| R54 | keep | C walks dog on the road. The text memory associates this observation with dog (text reference E1). |

完整程序分区：

```json
{
  "keep": [
    "R0",
    "R3",
    "R7",
    "R15",
    "R53",
    "R54"
  ],
  "skip": [
    "R1",
    "R2",
    "R4",
    "R5",
    "R6",
    "R8",
    "R9",
    "R10",
    "R11",
    "R12",
    "R13",
    "R14",
    "R16",
    "R17",
    "R18",
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
    "R31",
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
    "R55"
  ],
  "uncertain": [],
  "merge_groups": []
}
```


## 4. 真正送给最终回答模型的信息

实际输入为792token，完整JSON见[最终调用输入](10_actual_final_model_input.json)，消息列表见[system/user消息](11_final_messages.json)，下方是经本地同一tokenizer还原、已核对token数的完整聊天模板文本。没有省略任何部分。

```text
<|im_start|>system
You are presented with a textual description of a video clip, it consists of frame captions sparsely sampled from the video. Your task is to answer a question related to this video, choosing the correct option out of five possible answers. Please provide the answer with a single-letter (A, B, C, D, E)It is crucial that you imagine the visual scene as vividly as possible to enhance the accuracy of your response. After selecting your answer, rate your confidence level in this choice on a scale from 1 to 100, where 1 indicates low confidence and 100 signifies high confidence. Please provide a concise one-sentence explanation for your chosen answer. If you are uncertain about the correct option, select the one that seems closest to being correct. <|im_end|>
<|im_start|>user
Here are a few examples. 
None 

###

  Description: These are sampled source observations, not a complete video record. Text identity and change hypotheses are not verified physical identities or causes. Listed intervals are separate observations, not proof of continuous duration or event counts. Question and options are hypotheses, not evidence. Missing evidence is not evidence of absence.
At sampled intervals [0,1), [1,2), [14,15), [16,17), [31,32), [38,39), [40,41), [43,44), [44,45), [145,146), [167,168), [172,173) (source assertion), C walks with the dog.
At sampled intervals [3,4), [4,5), [17,18), [18,19), [139,140), [148,149), [157,158), [173,174), [174,175) (source assertion), C walks dog.
At sampled intervals [4,5), [5,6), [15,16), [16,17) (source assertion), C walks dog on the road. The text memory associates this observation with C (text reference E0).
At sampled intervals [4,5), [15,16), [16,17) (source assertion), C walks dog on the road. The text memory associates this observation with dog (text reference E1).
At sampled intervals [6,7), [36,37), [140,141), [142,143), [151,152), [154,155), [175,176), [176,177) (source assertion), C pulls dog leash.
At sampled intervals [12,13), [39,40), [42,43), [136,137), [169,170), [177,178) (source assertion), C holds dog leash.

###

 Questions: In your own words, summarize the key actions c repeatedly performs throughout the video without simply listing them out. 
 Options: 
 A: The individual, c, continuously and repeatedly ceases their walking motion. 
 B: Continuously, c repeatedly waves her right hand multiple times. 
 C: Continuously, c takes plastic paper using her right hand, performing the action repeatedly. 
 D: C repeatedly takes plastic paper with her left hand. 
 E: C repeatedly touches her hair and walks her dog. 

###

 The prediction, explanation, confidence is (please response in the format of 'prediction: 
 explanation: 
 confidence: 
 '):<|im_end|>
<|im_start|>assistant

```


最终输出另外受到`prediction: A`至`prediction: E`的解码约束；这不属于输入证据，也不会把标准答案传给模型。


实际生成：`prediction: E`。这里不使用标准答案筛选或修正demo。

