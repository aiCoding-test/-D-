# P1 实际验收记录

自动回归：36/36 通过；JUnit 见 [完整结果](p1-full-junit.xml)。
环境：Windows 宿主 / Docker Linux 容器，Python 3.12、PostgreSQL 16；数据库与进程均为真实执行。加速故障测试使用 2 秒租约，长跑保持 30 秒租约 / 5 秒心跳。

| 编号 | 验收项 | 实际结果 | 证据 |
| --- | --- | --- | --- |
| P01 | 分块正常执行、完整 manifest / 下载与整步缓存区分 | 通过 | [p1-full-junit.xml](p1-full-junit.xml) |
| P02 | 提交两块后真实杀 Worker/子进程，仅补剩余块 | 通过 | [p1-full-junit.xml](p1-full-junit.xml) |
| P03 | 半块中断不复用 | 通过 | [p1-full-junit.xml](p1-full-junit.xml) |
| P04 | 发布后登记前崩溃，孤立文件不算完成 | 通过 | [p1-full-junit.xml](p1-full-junit.xml) |
| P05 | 登记后 ack 丢失、重复登记幂等 | 通过 | [p1-full-junit.xml](p1-full-junit.xml) |
| P06 | 损坏块与人工恢复，保留有效块 | 通过 | [p1-full-junit.xml](p1-full-junit.xml) |
| P07 | 输入 / 参数 / 处理器 / 分块语义改变不误复用 | 通过 | [p1-full-junit.xml](p1-full-junit.xml) |
| P08 | 旧 Attempt 块、心跳进度、完整 Step 汇总拒绝 | 通过 | [p1-full-junit.xml](p1-full-junit.xml) |
| P09 | 取消与登记竞争；失联资源隔离继承 P0 回归 | 通过 | [p1-p0-regression.md](p1-p0-regression.md) |
| P10 | 超过 100 事件筛选/翻页、资源查询及指标口径 | 通过 | [p1-ui-history.json](p1-ui-history.json) |
| P11 | 降采样和保留期；无效指标保留缺失 | 通过 | [p1-full-junit.xml](p1-full-junit.xml) |
| P12 | 中断未完成 Step 中坏 1 块，只重算坏块和缺失块 | 通过 | [p1-full-junit.xml](p1-full-junit.xml) |
| P13 | 实际 ≥3600 秒、真实中断恢复、旧块保留和资源回收 | 未完成 / 待验证 | [hourly.json](hourly.json) |
| P14 | 0001 不变、真实旧数据/下载兼容升级 | 通过 | [p1-upgrade.json](p1-upgrade.json) |
| P15 | 公开克隆、新卷迁移启动、v1/v2与重启持久化 | 未完成 / 待验证 | [p1-clean-start.json](p1-clean-start.json) |

P0 A01～A28 的 P1 回归映射见 [回归矩阵](p1-p0-regression.md)；真实停库、kill 容器及 API 重启见 [容器验证](p1-runtime.json)。

## 实际小时级记录

任务 `e362ced8-448f-4722-9555-44af8216ea42`；状态 `RUNNING`；开始 UTC `2026-11-01T11:31:17.255311+00:00`；结束 UTC `—`；实际观察时长 `1954.40` 秒。

[逐次观察](hourly-observations.jsonl) · [独立时间/下载/心跳审计](p1-hourly-audit.json) · [页面重连证据](p1-hourly-browser.json)

## 复现

```bash
docker compose -f compose.yaml -f compose.test.yaml run --rm test
python scripts/verify_runtime.py --prefix p1-runtime
python scripts/report_acceptance.py --junit p1-full-junit.xml --runtime p1-runtime.json --output p1-p0-regression.md
# 小时级测试必须独立后台运行；先检查既有记录和观察器，不能覆盖证据
python scripts/verify_hourly.py
python scripts/report_p1.py
```

分块故障阶段由测试专用环境开关确定，默认不开；没有用修改 Task/Step 状态代替进程故障。单元式指纹验证不伪称真实 GPU 测试。
历史分页测试会增加事务提交的测试事件；另用真实失败任务加 5 次合法恢复得到 128 条事件，经浏览器全部加载。

限制、观测器中断修复和复现步骤见 [实现说明](../docs/10-P1实现与验收.md)。这组结果不证明真实 GPU、模型推理质量或任意模型内部状态恢复。
