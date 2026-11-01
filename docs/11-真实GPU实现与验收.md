# R14：真实 NVIDIA GPU 适配

动画媒体处理仍是 MOCK。显式选择 `gpu_mode=nvidia` 后，抠图/增强的四个帧块分别执行真实 CUDA 整数内核并验证回传结果；这不是抠图模型、训练或 AI 效果验证。默认 `docker compose up --build -d` 完全不安装或导入 NVML，也不要求 GPU。

## 启动和请求

需要 NVIDIA 驱动、Docker Linux 容器 GPU 透传。先在主机检查 `nvidia-smi`，再在容器检查；驱动或 NVML 不可用时真实 Worker 明确启动失败，不退回模拟。发现不到匹配设备时任务排队，不创建计算 Attempt。

```bash
# 先构建无 GPU 基础镜像，再构建可选 Worker
docker compose build api
docker compose -f compose.yaml -f compose.gpu.yaml build worker
docker compose -f compose.yaml -f compose.gpu.yaml up --no-build -d
```

首页 GPU 执行选择“真实 NVIDIA CUDA 探针”；使用 normal + 快速演示。也可请求：

```json
{
  "name": "bounded CUDA probe",
  "gpu_mode": "nvidia",
  "pipeline_version": "animation-v2",
  "scenario": "normal",
  "cache_read": false,
  "step_seconds": 4,
  "gpu": {"min_memory_mb":512,"min_compute_major":7,"probe_memory_mb":8,"oom_test":false}
}
```

通过 `POST /api/tasks` 并带 `Idempotency-Key` 提交。可用 `gpu.uuid` 限定物理卡。`GPU_UUIDS` 环境变量限制 Worker 发现的卡；容器可见卡集合仍由 Docker GPU 透传控制。CUDA 初始化前设置 `CUDA_VISIBLE_DEVICES=<UUID>`，初始化后要求进程恰有一张卡，并用 `cuDeviceGetUuid_v2` 再核对物理 UUID，记录进程内序号 0。可见性不是显存硬配额；本系统独占不能阻止外部应用抢占。

可选依赖仅 `requirements.gpu.lock` 中带 SHA-256 的 NVIDIA 官方 `nvidia-ml-py`。CUDA 使用驱动 API 和仓库内固定 PTX，不安装 PyTorch、CUDA Toolkit 或大型模型；内核/PTX 与 ABI 兼容契约版本进入缓存身份。驱动属于宿主先决条件，实际版本记录在设备、Attempt 和产物中。

## 调度、恢复和持久化

- 设备键为 `node_id:GPU-UUID`，一行独占整卡。保存型号、UUID、容量、计算能力、驱动、健康及样本时间；同一物理节点必须使用稳定一致的 node_id。
- 每个 GPU Step 冻结类型、最低容量、最低计算能力及可选 UUID；短领取事务按原锁顺序原子绑定 Slot、Attempt 和 fence。GPU_BUSY、容量/能力不符、外部计算进程、压力、过期采样、清理隔离都有等待原因；等待不消耗次数。剩余显存只用于辅助准入，绝不是原子显存预留。
- 新迁移 0003 只扩大设备 key 并增加 ResourceSlot.device、Attempt.execution；0001/0002 保持不变。原任务按冻结 v1/v2 MOCK 处理器继续，默认提交剔除新增默认字段，保留原请求幂等指纹。
- CUDA 探针沿用 Goal A 的检查点提交协议：块私有写、校验、不可变发布、受当前 epoch/Attempt/租约/fence 保护登记；四块齐备才汇总。真实块 schema 增加 `cuda_probe`（UUID、ordinal、驱动、分配大小、内核哈希、计算校验和、launch 次数），明确 `simulated=false`。CPU 媒体步骤仍标 MOCK。
- 已提交块可跨重试和人工代次复用。GPU 模式、PTX/处理器、兼容契约、有效内存配置及 OOM 测试标志参与语义；MOCK 与 CUDA 缓存隔离。最低准入容量/指定设备属于调度约束，不改变确定性整数计算结果。变更有效计算配置通过新任务审计，不能静默降 batch。
- 正常退出、失联或取消后，真实槽先隔离；继承的 lifetime flock 证明旧执行器已退出，再等待退出后的新鲜健康 NVML 样本才释放。数据不确定时继续隔离。控制循环不调用阻塞 NVML；独立采样线程与既有无 DB 租约看门狗并存。

## 资源边界与真实 OOM

常规探针每 Attempt 1～64MiB 设备数组，单个整数内核很短，执行间隔 10ms，Step 时长与 Task 截止沿用现有控制。子进程可被取消/看门狗终止，驱动 context 随进程释放；没有 GPU 重置、杀外部进程、调频或压力跑分。

专用 OOM 配置 `gpu.oom_test=true` 还要求 Worker 显式设置 `ALLOW_CUDA_OOM_TEST=1`；普通部署默认为 0。测试在该 Attempt 的独立 CUDA 内存池设置 `maxSize=16MiB`，向该池请求 64MiB，驱动实际返回 `CUDA_ERROR_OUT_OF_MEMORY (2)`。不会递增分配直到整卡耗尽；池上限不支持/不生效就失败并报告，不升级为危险分配。该证据是**真实驱动分配器的封顶池 OOM**，不声称测过真实模型全卡 OOM。

稳定 CUDA OOM 记录 API 操作、实际返回值、错误文本、有效配置和执行日志，原配置不自动重试。其他 CUDA 初始化/内核错误也明确失败；进程异常退出记录 returncode，按既有预算重试并保留有效块。

## 采样与限制

NVML 获取实际显存、利用率、温度和可获得的计算/图形进程信息。返回 null/unavailable 与字段错误原因，不补 0；设备有效表示身份/显存查询可用，不意味着每个可选指标都可用。当前 Windows WSL2 驱动的利用率查询返回 `Unknown Error`，因此利用率趋势留空、Prometheus 不导出虚假的利用率值；显存和温度为实际值。

NVML 在 WSL/WDDM 的进程视图可能不完整，空列表不能证明没有外部程序；硬件验收另执行主机 nvidia-smi 进程检查。整卡独占仅对本系统任务成立。资源历史仍按原始 1h、分钟 7d 保留，汇总保留真实 source，不能把 NVML 改标 MOCK。`/metrics` 分开导出 mock/nvml，禁止 task_id、attempt_id 标签。

## 硬件验收与复现

独立验证配置 `compose.gpu-test.yaml` 使用 taskd-gpu、18085/55438 和 10.233.90.0/24，保留单独数据库/产物卷，不干扰默认演示。先确认无外部计算工作负载，执行：

```bash
docker compose -p taskd-gpu -f compose.yaml -f compose.gpu.yaml -f compose.gpu-test.yaml up --no-build -d api worker
python scripts/verify_gpu.py
docker compose -f compose.yaml -f compose.test.yaml run --rm test
# 收起硬件验证服务，保留全部卷
docker compose -p taskd-gpu -f compose.yaml -f compose.gpu.yaml -f compose.gpu-test.yaml down
```

硬件脚本保存任务/尝试 ID、快照、真实日志、两任务排队、受限 OOM、取消清理、实际 SIGKILL Worker 后块复用、旧令牌拒绝及缓存/参数隔离。原始文件见 [gpu-hardware.json](../evidence/gpu-hardware.json)、[Worker 日志](../evidence/gpu-worker.log)；实际结果以 [验收表](../evidence/gpu-acceptance.md) 为准。无硬件的契约测试不计作硬件通过。

参考 NVIDIA 官方 [Driver API](https://docs.nvidia.com/cuda/archive/12.6.2/cuda-driver-api/group__CUDA__MALLOC__ASYNC.html)、[WSL 限制](https://docs.nvidia.com/cuda/wsl-user-guide/index.html)、[NVML](https://docs.nvidia.com/deploy/nvml-api/latest/index.html)。
