# Task D · 可观测 AI 任务管线

一个“能看进度、出错能重试、进程中断能继续”的后台任务处理工具。提交预置动画任务后，页面展示 7 步过程、执行尝试、失败原因、资源等待和产物；相同输入可以复用已验证结果。

**模型、动画加工和 GPU 遥测为 MOCK；数据库、独立进程、调度、重试、租约恢复、文件校验与资源排队真实运行。**

源码：[aiCoding-test/-D-](https://github.com/aiCoding-test/-D-)。

## 启动

需要 Docker Engine / Docker Desktop（Linux 容器）、Docker Compose **2.24.4+**。首次构建需要访问官方镜像与 PyPI。Windows 推荐 Docker Desktop + WSL2；Worker 使用 Linux flock，不支持原生 Windows 执行。

```bash
git clone https://github.com/aiCoding-test/-D-.git task-d
cd task-d
docker compose up --build -d
```

打开 [任务看板](http://localhost:8000) 或 [OpenAPI](http://localhost:8000/docs)。默认仅绑定 127.0.0.1；公开交付为源码仓库，未部署公网执行服务。

```bash
docker compose ps
docker compose logs -f worker
docker compose down
```

普通 down 保留数据库和产物卷，之后 up -d 可继续。**不要使用 down -v 恢复任务，它会删除持久卷。** 数据库健康见 /health/ready，Worker 在线见 /api/resources。

可复制 .env.example 为 .env 调整端口、密码和心跳/租约。已初始化数据库的密码不能仅改环境变量完成轮换。专用网段默认 10.233.84.0/24，冲突时配置 TASKD_SUBNET，不要清理其他项目网络。

## 使用

1. 首页选择素材、参数和演示场景，点击“创建任务”。
2. 查看状态、进度和等待原因；点击步骤查看尝试、错误、心跳及资源。
3. 瞬时错误自动重试；失败耗尽后点击“恢复未完成步骤”，有效成功步骤保留。
4. 完成后下载 JSON、SVG 或 manifest，内容明确标注 MOCK。
5. 相同输入再次提交并开启复用，会显示 CACHED 和原产物来源，不创建计算尝试。

```text
校验 → 抽帧 → 抠图[GPU] → 增强[GPU] ┬→ 图集[CPU] ─┐
                                       └→ 预览[CPU] ─┴→ 打包
```

图集与预览独立并行，打包依赖两者。打包结果为记录输入指纹的 MOCK manifest/文件集合，未执行真实媒体加工。

| 演示场景 | 行为 |
| --- | --- |
| 正常完成 | 按依赖执行全部步骤 |
| 抠图首次失败 | 第一次失败，第二次成功，上游不重跑 |
| 预览失败、人工恢复 | 第一代次失败三次，恢复后第二代次继续 |
| 长步骤 / 中断 | 抠图至少持续 15 秒，适合 kill 与取消 |
| GPU OOM | 模拟稳定显存不足，原配置不自动重试 |
| 持续失败 | 抠图耗尽 3 次尝试，下游阻塞 |
| 挂起 / 超时 | 心跳正常但业务无进度，随后运行超时 |

页面故障场景自动关闭缓存读取，避免缓存跳过故障；API 的 cache_read 按调用者配置执行。

## 实现与恢复语义

- Python 3.12、FastAPI、SQLAlchemy/Alembic、PostgreSQL 16；Jinja2 与原生 JS 页面，2 秒轮询。
- API / Worker 独立容器，步骤在子进程运行；数据库兼作就绪队列。
- Task / Step / Attempt 分开保存，状态和关键事件在同一事务提交，每任务事件游标有序。
- 短事务领取；租约、当前尝试和递增 fence 保护写入。旧尝试不能完成、续租或更新进度。
- 默认心跳 5 秒、租约 30 秒。退避和预算持久化；独立看门狗不访问 DB，不能可信续租时停止本地计算。
- 子进程继承共享卷上的 Linux 文件锁；失联先隔离，确认停止后才释放资源。
- 文件在私有目录生成、校验、不可变发布，再提交正式引用；孤立文件不作为成功结果，P0 不自动 GC。
- 缓存键包含输入、步骤/模型版本、有效参数、种子、schema 与模拟模式；命中前校验文件哈希。

提供至少一次计算尝试语义，中断的当前步骤可能重算；进度百分比不是内部检查点。真 GPU、小时级稳定性、多机和永久存储损坏不在已验证范围。

## 验证

测试只允许清空名为 taskd_test 的独立测试库，不清空演示任务。

```bash
docker compose exec -T postgres createdb -U taskd taskd_test
docker compose -f compose.yaml -f compose.test.yaml run --rm test
```

测试库已存在时省略 createdb。JUnit 输出至 evidence/junit.xml。

真实容器故障验证使用独立 taskd-e2e 项目，端口 18080/55433、网段 10.233.85.0/24，不停止演示服务。主机需要 Python 3.12 和锁定依赖：

```bash
python -m venv .venv
# Linux/macOS 使用 .venv/bin/python；Windows 使用 .venv/Scripts/python.exe
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python scripts/verify_runtime.py
.venv/bin/python scripts/report_acceptance.py
```

脚本真实 kill Worker、stop PostgreSQL、restart API。验证后可运行 python scripts/verify_runtime.py --cleanup，仅移除该测试项目的容器/网络，保留卷与证据。首次运行自动建立全新项目；重复运行保留历史任务。

实际结果与 A01～A28 对照见 **[验收记录](evidence/acceptance.md)**、[容器故障快照](evidence/runtime.json)、[全新克隆启动证据](evidence/clean-start.json)、[页面截图](evidence/dashboard.jpg)。

## 代码与文档

| 内容 | 入口 |
| --- | --- |
| 题目要求的说明、token、耗时、失败分析 | [交付说明](docs/08-交付说明与过程记录.md) |
| API | [app/api.py](app/api.py) |
| 事务状态机、领取、重试与缓存 | [app/service.py](app/service.py) |
| 执行监督与租约看门狗 | [app/worker.py](app/worker.py) |
| 数据模型和冻结迁移 | [app/models.py](app/models.py)、[0001.sql](migrations/versions/0001.sql) |
| 数据库/进程测试 | [tests/test_pipeline.py](tests/test_pipeline.py) |
| 设计与实现差异 | [实现说明](docs/09-实现差异与限制.md) |
| 原始设计参考 | [业务需求](docs/01-业务场景与需求.md)、[架构](docs/02-架构与技术选型.md)、[恢复协议](docs/03-执行状态与恢复设计.md)、[数据接口](docs/04-数据模型与接口.md)、[模块风险](docs/05-模块拆分与风险.md)、[GPU/长任务](docs/06-GPU与小时级任务.md)、[验收计划](docs/07-验收与实施计划.md) |

设计稿保留作决策参考。已实现行为以代码、实际验收和实现差异为准；P0-GOAL.md 是开发任务原文。
