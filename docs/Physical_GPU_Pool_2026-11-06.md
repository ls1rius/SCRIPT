# 后续实验的物理显存预留

用户要求：后续实验按运行峰值所需容量实际占住显存，模型跨题、跨阶段驻留。本次只准备代码与验证，不修改、重启或迁移当前冻结训练和500题评测。

实现位于 `script_core/physical_cuda_pool.py` 和 `script_core/csrc/physical_cuda_pool.cpp`。新模型进程在首次 PyTorch CUDA 初始化前，用一次 `cudaMalloc` 建立固定容量池；模型权重、激活、梯度、优化器张量以及由 PyTorch 分配的库工作区均从池内分配。释放张量仅归还池内空闲块；`empty_cache()`、题目结束和阶段切换均不释放池本体。容量持续占用到模型进程退出；正常流程保持原先的长驻进程和共享租约。

与只创建占位张量不同，计算直接复用这块已分配的空间，没有“先释放占位再抢显存”的空窗。分配器覆盖整个进程，包括 autograd 工作线程；记录分配 stream 和 `record_stream` 使用的 stream，通过 CUDA event 约束复用，不逐次全卡同步。池内超限或碎片导致无法满足申请时明确 OOM，不回退申请池外张量显存。

| 角色 | 原共享准入 | 物理池容量 | CUDA上下文及池外库分配余量 |
| --- | ---: | ---: | ---: |
| N来源/Q4驻留进程 | 32768 MiB | 30720 MiB | 2048 MiB |
| 连续训练→验证主进程 | 43008 MiB | 40960 MiB | 2048 MiB |
| Q4评测/helper | 24576 MiB | 22528 MiB | 2048 MiB |

自动模式严格核对租约存活、进程祖先关系及所选 GPU UUID。模型进程只能使用自己的租约。不同阶段仍执行原 allocator 上限，例如同一个30 GiB池在Q4阶段限制为22 GiB；固定池不缩小。训练主进程从教师/加载阶段开始即占40 GiB，不到正式梯度阶段才申请余量。

这些数字是现有 allocator 的峰值预算，不是已测得的所有输入的真实峰值。CUDA上下文和绕过 PyTorch 的库分配可能仍使用池外显存；2 GiB余量仍是共享调度预约，并非物理池的一部分。未来完整模型首次上卡仍须核对容量、池外开销和正式语义/梯度门，不能据此宣称任何负载都不会 OOM。当前仅支持每模型进程单GPU、普通eager/梯度检查点训练，不支持CUDA Graph捕获或跨进程CUDA张量共享。

## 新实验启动

默认普通配置保持关闭，防止影响已有运行。新实验通过 `script/run_with_physical_cuda_pool.py` 启用，监督及其子进程继承 `VIDEOAGENT_PHYSICAL_CUDA_POOL=1`。该入口预编译与当前 PyTorch ABI 对应的扩展，检查冻结 runtime 和驻留模型入口已包含支持，然后执行原实验命令。编译检查不申请GPU显存。

```bash
PYTHON=/mnt/ssd2/hli/work/miniconda3/envs/vts/bin/python
$PYTHON script/run_with_physical_cuda_pool.py --runtime . --check
# 对从当前代码完整冻结的新Q4实验：
$PYTHON script/run_with_physical_cuda_pool.py --runtime /path/to/NEW/runtime -- \
  $PYTHON /path/to/NEW/runtime/script/runner.py supervise --out /path/to/NEW
```

对于继承历史教师标签的固定6轮训练，工作区的 `train_q4_fixed_epochs.py prepare` 在启用此模式时，会在**新目录、哈希冻结前**加入显存池支持，并更新新复制的驻留模型入口；它拒绝对已有 launch/frozen_hashes 的实验执行该支持补丁。准备阶段也应通过上述入口设置模式：

```bash
$PYTHON script/run_with_physical_cuda_pool.py --runtime . -- \
  $PYTHON script/train_q4_fixed_epochs.py prepare --old /path/to/TEACHER_SOURCE --out /path/to/NEW
$PYTHON script/run_with_physical_cuda_pool.py --runtime /path/to/NEW/runtime -- \
  $PYTHON /path/to/NEW/runtime/script/train_q4_fixed_epochs.py supervise --out /path/to/NEW
```

沿用原准备器对新目录的要求。不能直接对当前旧冻结任务设置该环境变量来宣称已启用；旧入口会被检查拒绝。后续新实验按本方案准备和冻结，不热改现有任务。显存池的容量、张量占用和峰值会记录在模型调用 `usage.cuda_physical_pool`，已有调用成本与训练协议保持。

## 验证范围

- CPU检查覆盖设置边界、32/42/24 GiB租约对应容量、过期/无关/错误GPU租约拒绝、新继承 runtime 的冻结前补丁、独立目录编译及旧入口拒绝；原驻留调度、共享租约、训练生命周期回归通过。
- 仅申请一次小规模共享准入，使用256 MiB物理池测试并在测试进程退出后确认释放。CUDA driver独立空闲显存读数验证真实占用；多线程前反向、仓库实际LoRA层、bf16底座/fp32适配器、梯度检查点、六次优化器更新、跨stream使用、碎片合并、上限切换、超限拒绝、`empty_cache`保留和峰值统计均检查。
- LoRA矩阵运算产生64 MiB常驻cuBLAS工作区，计入同一池。首次扩展测试误将其当作应释放的普通张量，断言失败；已按实际常驻工作区基线修正测试。另外修正了测试从consume stream读取标量时缺少对应stream上下文的问题。两次测试断言失败记录均保留，最终GPU检查通过。
- 未对当前大模型进行新调用，未申请30/40 GiB池，不把小规模通过称为完整Q4/六轮大模型验证通过。证据在 `workdirs/Q4-physical-pool-preparation-20261106-r1/`。

参考接口：[PyTorch CUDA显存管理](https://docs.pytorch.org/docs/stable/notes/cuda.html#memory-management)、[PyTorch可插拔分配器源码](https://github.com/pytorch/pytorch/blob/v2.13.0/torch/csrc/cuda/CUDAPluggableAllocator.cpp)。
