# P0 验收结果

运行环境：Linux 容器，Python 3.12.13，PostgreSQL 16.14；25 项 pytest 用例。

证据： [JUnit](junit.xml)、[真实容器故障快照](runtime.json)、[故障过程 Worker 日志](runtime-worker.log)、[全新克隆启动](clean-start.json)、[页面恢复截图](dashboard.jpg)、[最终看板](dashboard-preview.jpg)。

测试采用加速配置：单步 0.07～0.2 秒、心跳 0.25 秒、租约 2 秒；容器故障验证为心跳 0.5 秒、租约 4 秒。默认演示配置仍为心跳 5 秒、租约 30 秒。

| 编号 | 场景 | 结果 | 证据入口 |
| --- | --- | --- | --- |
| A01 | 正常 DAG、依赖、可下载产物 | 通过 | `test_A01_normal_dag_downloads` |
| A02 | 并发同键接收幂等 | 通过 | `test_A02_A03_concurrent_idempotency` |
| A03 | 同键不同参数冲突 | 通过 | `test_A02_A03_concurrent_idempotency` |
| A04 | 临时失败重试且上游不重做 | 通过 | `test_A04_transient_retry_preserves_upstream` |
| A05 | 三次尝试后耗尽预算 | 通过 | `test_A05_A06_retry_budget_and_permanent_errors[always_fail-3]`、`test_A05_A06_retry_budget_and_permanent_errors[gpu_oom-1]`、`test_A05_A06_retry_budget_and_permanent_errors[invalid_input-1]` |
| A06 | 永久错误、稳定模拟 OOM 不重试 | 通过 | `test_A05_A06_retry_budget_and_permanent_errors[always_fail-3]`、`test_A05_A06_retry_budget_and_permanent_errors[gpu_oom-1]`、`test_A05_A06_retry_budget_and_permanent_errors[invalid_input-1]` |
| A07 | 真实 Worker 容器 SIGKILL 后恢复 | 通过 | `test_A07_A10_real_supervisor_and_child_kill`；runtime.json A07 |
| A08 | 旧完成、心跳与进度拒绝 | 通过 | `test_A08_stale_completion_renewal_and_progress` |
| A09 | 领取后、启动前控制进程退出 | 通过 | `test_A09_A11_A12_commit_crash_windows[after_claim]`、`test_A09_A11_A12_commit_crash_windows[after_publish]`、`test_A09_A11_A12_commit_crash_windows[after_commit]` |
| A10 | 真实中断期间未发布临时文件不可消费 | 通过 | `test_A07_A10_real_supervisor_and_child_kill`；runtime.json A10 |
| A11 | 文件发布后、DB 提交前崩溃 | 通过 | `test_A09_A11_A12_commit_crash_windows[after_claim]`、`test_A09_A11_A12_commit_crash_windows[after_publish]`、`test_A09_A11_A12_commit_crash_windows[after_commit]` |
| A12 | DB 提交后崩溃与幂等完成回报 | 通过 | `test_A09_A11_A12_commit_crash_windows[after_claim]`、`test_A09_A11_A12_commit_crash_windows[after_publish]`、`test_A09_A11_A12_commit_crash_windows[after_commit]`、`test_completion_ack_replay_is_idempotent` |
| A13 | 失败任务人工恢复保留有效上游 | 通过 | `test_A13_A27_resume_invalidates_corrupt_ancestors` |
| A14 | 相同输入新任务 CACHED、无计算尝试 | 通过 | `test_A14_A15_A16_A17_cache_semantics_integrity` |
| A15 | 只修改预览参数仅重算受影响后代 | 通过 | `test_A14_A15_A16_A17_cache_semantics_integrity` |
| A16 | 种子、模型版本变更及执行模式指纹隔离 | 通过 | `test_A14_A15_A16_A17_cache_semantics_integrity`、`test_A28_api_validation_and_command_dedup` |
| A17 | 缓存文件缺失后拒绝命中并重算 | 通过 | `test_A14_A15_A16_A17_cache_semantics_integrity` |
| A18 | 单模拟 GPU 排队不消耗尝试 | 通过 | `test_A18_single_gpu_queue_without_attempt` |
| A19 | 只杀 Supervisor，遗留子进程保持隔离 | 通过 | `test_A19_orphan_child_keeps_resource_quarantined` |
| A20 | 挂起步骤心跳持续，进度停滞后运行超时 | 通过 | `test_A20_heartbeat_distinct_from_hung_progress` |
| A21 | 取消/完成双顺序与真实并发竞争 | 通过 | `test_A21_cancellation_completion_order[cancel]`、`test_A21_cancellation_completion_order[complete]`、`test_concurrent_final_completion_and_cancel` |
| A22 | 真实停 PostgreSQL，API 503 与租约接续 | 通过 | ；runtime.json A22 |
| A23 | 退避期间重启不重置时间或次数 | 通过 | `test_A23_retry_backoff_survives_worker_restart` |
| A24 | API 容器重启、页面重连后历史仍在 | 通过 | `test_A24_A26_snapshots_reconnect_and_event_cursor`；runtime.json A24 |
| A25 | 双 Worker 并发只领取一个当前尝试 | 通过 | `test_A25_two_workers_one_current_attempt` |
| A26 | 并发事件提交、快照与游标不遗漏 | 通过 | `test_A24_A26_snapshots_reconnect_and_event_cursor` |
| A27 | 人工恢复重算损坏祖先及后代 | 通过 | `test_A13_A27_resume_invalidates_corrupt_ancestors` |
| A28 | 非法请求、控制幂等与 DAG 校验 | 通过 | `test_A28_api_validation_and_command_dedup` |

**矩阵通过 28/28；pytest 通过 25/25；容器故障脚本 通过。**

A16 中执行模式隔离验证的是语义指纹不同，P0 API 拒绝开启真实计算；没有宣称运行过真实模型/GPU。A10 验证实际存在的未发布临时文件中断，没有模拟真实编解码器的内部文件损坏。

小时级长跑、真 GPU/NVML、模型内部检查点、宿主掉电和磁盘损坏没有验证，均超出 P0。页面验收已完成：提交预览失败任务、查看失败尝试、点击恢复、完成 7/7 步、刷新后保留历史。

复现命令见 [README](../README.md)。不要将这些小规模故障验证解释为集群吞吐或生产高可用证明。
