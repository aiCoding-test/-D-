"""Generate the acceptance ledger from actual pytest + container-fault results."""
import json
import argparse
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
evidence = ROOT / "evidence"
parser=argparse.ArgumentParser()
parser.add_argument("--junit",default="junit.xml")
parser.add_argument("--runtime",default="runtime.json")
parser.add_argument("--output",default="acceptance.md")
args=parser.parse_args()
tree = ET.parse(evidence / args.junit)
cases = {c.attrib["name"]: c.find("failure") is None and c.find("error") is None and c.find("skipped") is None
         for c in tree.findall(".//testcase")}
runtime = json.loads((evidence / args.runtime).read_text(encoding="utf-8"))
runtime_ok = {aid: check["passed"] for check in runtime.get("checks", []) for aid in check["ids"]}

mapping = {
    "A01": ("正常 DAG、依赖、可下载产物", ["test_A01_"], []),
    "A02": ("并发同键接收幂等", ["test_A02_"], []),
    "A03": ("同键不同参数冲突", ["test_A02_"], []),
    "A04": ("临时失败重试且上游不重做", ["test_A04_"], []),
    "A05": ("三次尝试后耗尽预算", ["test_A05_A06_"], []),
    "A06": ("永久错误、稳定模拟 OOM 不重试", ["test_A05_A06_"], []),
    "A07": ("真实 Worker 容器 SIGKILL 后恢复", ["test_A07_"], ["A07"]),
    "A08": ("旧完成、心跳与进度拒绝", ["test_A08_"], []),
    "A09": ("领取后、启动前控制进程退出", ["test_A09_A11_A12_"], []),
    "A10": ("真实中断期间未发布临时文件不可消费", ["test_A07_"], ["A10"]),
    "A11": ("文件发布后、DB 提交前崩溃", ["test_A09_A11_A12_"], []),
    "A12": ("DB 提交后崩溃与幂等完成回报", ["test_A09_A11_A12_", "test_completion_ack_"], []),
    "A13": ("失败任务人工恢复保留有效上游", ["test_A13_A27_"], []),
    "A14": ("相同输入新任务 CACHED、无计算尝试", ["test_A14_A15_A16_A17_"], []),
    "A15": ("只修改预览参数仅重算受影响后代", ["test_A14_A15_A16_A17_"], []),
    "A16": ("种子、模型版本变更及执行模式指纹隔离", ["test_A14_A15_A16_A17_", "test_A28_"], []),
    "A17": ("缓存文件缺失后拒绝命中并重算", ["test_A14_A15_A16_A17_"], []),
    "A18": ("单模拟 GPU 排队不消耗尝试", ["test_A18_"], []),
    "A19": ("只杀 Supervisor，遗留子进程保持隔离", ["test_A19_"], []),
    "A20": ("挂起步骤心跳持续，进度停滞后运行超时", ["test_A20_"], []),
    "A21": ("取消/完成双顺序与真实并发竞争", ["test_A21_", "test_concurrent_final_"], []),
    "A22": ("真实停 PostgreSQL，API 503 与租约接续", [], ["A22"]),
    "A23": ("退避期间重启不重置时间或次数", ["test_A23_"], []),
    "A24": ("API 容器重启、页面重连后历史仍在", ["test_A24_A26_"], ["A24"]),
    "A25": ("双 Worker 并发只领取一个当前尝试", ["test_A25_"], []),
    "A26": ("并发事件提交、快照与游标不遗漏", ["test_A24_A26_"], []),
    "A27": ("人工恢复重算损坏祖先及后代", ["test_A13_A27_"], []),
    "A28": ("非法请求、控制幂等与 DAG 校验", ["test_A28_"], []),
}

lines = ["# P0 验收结果", "", f"运行环境：Linux 容器，Python 3.12.13，PostgreSQL 16.14；{len(cases)} 项 pytest 用例。",
         "", f"证据： [JUnit]({args.junit})、[真实容器故障快照]({args.runtime})、[故障过程 Worker 日志]({args.runtime.removesuffix('.json')}-worker.log)、[全新克隆启动](clean-start.json)、[页面恢复截图](dashboard.jpg)、[最终看板](dashboard-preview.jpg)。",
         "", "测试采用加速配置：单步 0.07～0.2 秒、心跳 0.25 秒、租约 2 秒；容器故障验证为心跳 0.5 秒、租约 4 秒。默认演示配置仍为心跳 5 秒、租约 30 秒。",
         "", "| 编号 | 场景 | 结果 | 证据入口 |", "| --- | --- | --- | --- |"]
passed = 0
for aid, (description, prefixes, ext) in mapping.items():
    matched = [n for prefix in prefixes for n in cases if n.startswith(prefix)]
    ok = all(any(n.startswith(prefix) for n in cases) for prefix in prefixes)
    ok = ok and all(cases[n] for n in matched) and all(runtime_ok.get(i, False) for i in ext)
    passed += int(ok)
    refs = "、".join(f"`{n}`" for n in matched) + ("；runtime.json " + ",".join(ext) if ext else "")
    lines.append(f"| {aid} | {description} | {'通过' if ok else '失败或未验证'} | {refs} |")
lines += ["", f"**矩阵通过 {passed}/28；pytest 通过 {sum(cases.values())}/{len(cases)}；容器故障脚本 {'通过' if runtime.get('passed') else '失败或未完成'}。**", "",
          "A16 中执行模式隔离验证的是语义指纹不同，P0 API 拒绝开启真实计算；没有宣称运行过真实模型/GPU。A10 验证实际存在的未发布临时文件中断，没有模拟真实编解码器的内部文件损坏。",
          "", "小时级长跑、真 GPU/NVML、模型内部检查点、宿主掉电和磁盘损坏没有验证，均超出 P0。页面验收已完成：提交预览失败任务、查看失败尝试、点击恢复、完成 7/7 步、刷新后保留历史。",
          "", "复现命令见 [README](../README.md)。不要将这些小规模故障验证解释为集群吞吐或生产高可用证明。", ""]
(evidence / args.output).write_text("\n".join(lines), encoding="utf-8")
print(f"Matrix {passed}/28; pytest {sum(cases.values())}/{len(cases)}; runtime={runtime.get('passed')}")
if passed != 28:
    raise SystemExit(1)
