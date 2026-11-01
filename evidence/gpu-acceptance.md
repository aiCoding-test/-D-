# R14 实际 GPU 验收

回归 **43/43**，使用真实 PostgreSQL；其中 G01～G05 为无 GPU 契约测试，不作为硬件证据。
[JUnit](gpu-regression-junit.xml) · [硬件快照/任务/Attempt](gpu-hardware.json) · [真实日志](gpu-worker.log) · [子进程退出验证](gpu-extra.json)

GPU：`NVIDIA GeForce RTX 4060 Laptop GPU`；UUID `GPU-069ab99a-b67f-4aef-e072-90c6f401d7d8`；驱动 `560.94`；CUDA Driver API `12060`。
本次硬件运行 UTC `2026-11-01T12:58:31.501466+00:00` → `2026-11-01T13:00:39.047013+00:00`；预检查其他计算进程输出 `(空)`。

| 编号 | 场景 | 实际结果 |
| --- | --- | --- |
| H01_UUID_KERNEL_NVML | 实际 UUID / cuda:0 绑定、内核输出、下载和 NVML 数据 | 通过 |
| H02_ATOMIC_QUEUE | 两任务争同卡：等待不消耗 Attempt，随后各自完成 | 通过 |
| H03_CONSTRAINTS_NO_ATTEMPT | 最低容量不匹配：排队解释且不创建 GPU Attempt | 通过 |
| H04_REAL_BOUNDED_OOM | 16MiB 封顶池真实 CUDA 2 OOM；不以原配置自动重试 | 通过 |
| H05_CANCEL_CLEANUP | 真实 GPU 子进程取消、退出确认与健康样本后回收 | 通过 |
| H06_KILL_RECOVERY_FENCE | 真实杀 Worker；保留两块，旧块/进度/汇总写入拒绝 | 通过 |
| H07_CACHE_CONFIG_ISOLATION | MOCK/CUDA 缓存隔离，有效分配配置改变影响身份 | 通过 |
| H08 | 单独杀 CUDA 子进程，记录 -9 退出、只补缺块，完整 schema 复核 | 通过 |

NVML 的显存/温度为真实查询。当前 WSL 驱动利用率返回 Unknown Error，保留 null/unavailable，趋势不填 0，指标不导出伪值；NVML 进程列表可能受 WSL 限制，不据此承诺外部独占。
OOM 为真实 CUDA 驱动封顶池分配失败，不等于真实模型或全卡耗尽试验。未下载/运行 AI 模型；CPU 媒体产物仍为 MOCK。

设备回收依赖 lifetime flock 退出证明与确认退出后的新鲜健康样本；缺失时保持隔离。G05 专门检查旧健康样本不能提前解除隔离。

[旧数据升级](gpu-upgrade.json) · [最终匿名源码干净部署](gpu-clean-final.json) · [独立交付审计](gpu-completion-audit.json) · [页面实际提交](gpu-ui.json)

运行命令与边界见 [实现说明](../docs/11-真实GPU实现与验收.md)。P0/P1 初始证据完整保留；当前迁移/公开交付状态另见 README。
