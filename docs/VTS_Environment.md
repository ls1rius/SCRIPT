# 统一 vts 环境

2026-11-01，新加坡时间。所有现存 SCRIPT 查询、记忆构建、E/F/G/H/I/J 实验入口及 CPU 检查统一使用：

```bash
conda activate /mnt/ssd2/hli/work/miniconda3/envs/vts
python script_qa.py --help
python script_qa.py query --base <base> --questions <questions.jsonl> --out workdirs/query-F
```

未初始化 Conda 的 shell 可先执行：

```bash
source /mnt/ssd2/hli/work/miniconda3/etc/profile.d/conda.sh
conda activate vts
```

也可直接使用 `/mnt/ssd2/hli/work/miniconda3/envs/vts/bin/python`。本地 smoke 脚本默认使用该解释器，仍允许通过 `PYTHON_BIN` 显式指定。

`workdirs` 中的旧独立环境、包目录及兼容软链接均已删除。原 `query-runtime-env` 路径不再存在，新操作和现有 H→I→J 控制器直接使用 vts，不再创建项目专用环境。调用历史冻结入口时，将解释器改为上述 vts 路径，冻结脚本和实验目录保持原样。

本次迁移将原覆盖环境的 46 个依赖按既有版本迁入 vts，另补齐调试依赖 debugpy。保留迁移前两者实际共同使用的 PyTorch/CUDA 版本，不修改历史实验报告的软件指纹。

| 组件 | 当前版本 |
| --- | --- |
| Python | 3.12.15 |
| PyTorch | 2.13.0+cu130 |
| TorchVision | 0.28.0+cu130 |
| CUDA runtime | 13.0 |
| Transformers | 4.57.6 |
| Accelerate | 1.12.0 |
| LM Format Enforcer | 0.11.3 |
| PyAV | 19.0.2 |
| debugpy | 1.8.22 |

全部 82 个已安装分发包的确切版本记录在 [requirements-vts.lock.txt](../requirements-vts.lock.txt)，直接依赖见 [requirements.txt](../requirements.txt)。锁文件描述当前环境；重建环境时应使用对应的 CUDA 13.0 PyTorch wheel。历史报告中其他软件版本反映各自运行时，保留原记录。

迁移验证：`pip check` 通过；19 个运行时模块在禁用用户级包目录后均从 vts 导入；小型随机初始化 Qwen2.5-VL 的真实 CPU 前向通过；CLI 的 E/F/I 参数正常。完整回归运行 869 项，868 项通过、1 项跳过。CUDA 可用，可识别四张 Blackwell GPU，当前 PyTorch 包含 `sm_120` 支持。未在迁移中加载 7B 权重或新增正式 GPU 答案；H 的真实功能门仍按原队列执行。

迁移时 H-r5、I-r4-queue、J-r7-queue 控制器保持运行。随后按用户要求彻底移除 workdirs 环境路径时，仅将这三个尚未启动模型的等待控制器重新启动为直接使用 vts；没有重试模型或重新评测题目，冻结方法、输出目录、GPU 门槛、失败处理和 H→I→J 顺序保持。实时 PID 与状态读取各自 `queue_status.json`。已退役的实验与删除的训练代码不因环境迁移而恢复。其他项目的 Conda 环境不在清理范围。

迁移和完整性记录：[VTS_Environment_Unification_2026-11-01.json](history/VTS_Environment_Unification_2026-11-01.json)。

旧环境路径移除及等待控制器切换记录：[VTS_Legacy_Path_Removal_2026-11-01.json](history/VTS_Legacy_Path_Removal_2026-11-01.json)。
