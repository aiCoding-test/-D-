"""Trusted MOCK child, no DB credentials or business-state mutations needed."""
import json
import os
import sys
import time

from app.artifacts import folder, seal, write_json


def run(attempt_id):
    work = folder(f"work/{attempt_id}")
    spec = json.loads((work / "spec.json").read_text(encoding="utf-8"))
    key, scenario = spec["key"], spec["scenario"]
    error = None
    if key == "inspect" and scenario == "invalid_input":
        error = ("INVALID_INPUT", False)
    if key == "remove_background":
        if scenario == "gpu_oom":
            error = ("GPU_OOM", False)
        elif scenario == "always_fail" or (scenario == "transient_once" and spec["epoch_number"] == 1):
            error = ("TRANSIENT_DEPENDENCY", True)
    if key == "render_preview" and scenario == "resume_preview" and spec["epoch"] == 1:
        error = ("TRANSIENT_DEPENDENCY", True)
    duration = spec["seconds"]
    if key == "remove_background" and scenario == "slow_step":
        duration = max(duration * 10, 15)
    if key == "remove_background" and scenario == "hang_step":
        while True:
            time.sleep(1)
    (work / "partial.tmp").write_text("not a published artifact", encoding="utf-8")
    for i in range(1, 11):
        time.sleep(duration / 10)
        write_json(work / "progress.next", {"done": i * 10, "total": 100})
        os.replace(work / "progress.next", work / "progress.json")
    if error:
        write_json(work / "result.json", {"ok": False, "code": error[0], "retryable": error[1]})
        return
    (work / "partial.tmp").unlink()
    payload = {"simulated": True, "notice": "MOCK animation output; no AI model was executed",
               "step": key, "semantic": spec["semantic"], "frames": 96}
    write_json(work / "output.json", payload)
    if key in {"build_atlas", "render_preview", "package"} and not (
            key == "render_preview" and spec["semantic"]["processor_version"] == "mock-2"
            and spec["semantic"]["parameters"].get("preview_format") == "json"):
        # Deterministic visual mock, useful as a downloadable demonstration artifact.
        color = "#38bdf8" if key == "build_atlas" else "#5eead4"
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="240"><rect width="640" height="240" rx="20" fill="#102034"/><text x="30" y="48" fill="white" font-size="22">MOCK · {key}</text>'
        for i in range(8):
            svg += f'<circle cx="{45 + i * 76}" cy="{130 + (i % 3) * 16}" r="22" fill="{color}"/>'
        svg += '<text x="30" y="220" fill="#9caec4" font-size="14">Simulated artifact · no AI model</text></svg>'
        p = work / "preview.svg"
        with p.open("w", encoding="utf-8") as f:
            f.write(svg)
            f.flush()
            os.fsync(f.fileno())
    seal(work, spec["cache_key"])
    write_json(work / "result.json", {"ok": True})


if __name__ == "__main__":
    run(sys.argv[1])
