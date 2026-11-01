"""Trusted MOCK child, no DB credentials or business-state mutations needed."""
import json
import os
import sys
import time
import shutil

from app.artifacts import folder, seal, write_json


def execute_chunks(work, spec, probe=None):
    from app.artifacts import verify, file_hash
    chunks = spec["chunks"]
    for chunk in chunks:
        index = chunk["index"]
        reuse = chunk.get("reuse")
        if reuse:
            if not verify(reuse["directory"], reuse["manifest_digest"]):
                raise ValueError("CHECKPOINT_INVALID")
            source = folder(reuse["directory"])
        else:
            source = folder(f'checkpoint-work/{spec["id"]}/{index}')
            source.mkdir(parents=True, exist_ok=True)
            (source / "partial.tmp").write_text("uncommitted chunk", encoding="utf-8")
            if spec.get("test_partial_chunk") == index and spec["epoch_number"] == 1:
                time.sleep(60)
            duration = spec["seconds"] / len(chunks)
            if spec["key"] == "remove_background" and spec["scenario"] == "slow_step":
                duration = max(spec["seconds"] * 10, 15) / len(chunks)
            started = time.monotonic()
            cuda_result=probe.compute(duration) if probe else None
            while not probe:
                elapsed = time.monotonic() - started
                fraction = min(elapsed / duration, 1)
                write_json(work / "progress.next", {"done": 100 * (index + fraction) / len(chunks),
                           "total": 100, "current_chunk": index})
                os.replace(work / "progress.next", work / "progress.json")
                if fraction == 1:
                    break
                time.sleep(min(.5, duration / 5))
            (source / "partial.tmp").unlink()
            payload={"simulated":probe is None,"chunk_identity":chunk['identity']}
            if probe:payload['cuda_probe']=cuda_result
            write_json(source / "output.json",payload)
            seal(source, chunk["chunk_key"],simulated=probe is None)
            write_json(work / f"chunk-ready-{index}.json", {"index": index})
            while not (work / f"chunk-ack-{index}.json").exists():
                time.sleep(.03)
            # The supervisor publishes before acknowledging.
            source = folder(f'checkpoints/{spec["id"]}/{index}')
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        target = work / f"block-{index:03d}.json"
        shutil.copyfile(source / "output.json", target)
        if file_hash(target) != next(f["sha256"] for f in manifest["files"] if f["key"] == "output.json"):
            raise ValueError("CHECKPOINT_INVALID")
    write_json(work / "output.json", {"simulated": probe is None, "step": spec["key"],
               "semantic": spec["semantic"], "blocks": [c["chunk_key"] for c in chunks], "frames": 96})
    seal(work, spec["cache_key"],simulated=probe is None)
    write_json(work / "result.json", {"ok": True})


def run(attempt_id):
    work = folder(f"work/{attempt_id}")
    spec = json.loads((work / "spec.json").read_text(encoding="utf-8"))
    key, scenario = spec["key"], spec["scenario"]
    if spec.get('execution'):
        from app.cuda_probe import Probe,CudaError
        probe=None
        try:
            execution=spec['execution'];config=execution['effective_config']
            if config['oom_test'] and not execution['oom_authorized']:
                raise RuntimeError('OOM_TEST_DISABLED: explicit worker authorization required')
            probe=Probe(execution['gpu_uuid'],config['probe_memory_mb'],spec['semantic']['parameters']['seed'])
            if config['oom_test']:probe.bounded_oom()
            execute_chunks(work,spec,probe)
        except CudaError as exc:
            print(str(exc),flush=True)
            write_json(work/'result.json',{'ok':False,'code':'CUDA_OOM' if exc.code==2 else 'CUDA_DRIVER_ERROR',
                'retryable':False,'detail':str(exc),'cuda_code':exc.code})
        except Exception as exc:
            print(repr(exc),flush=True)
            write_json(work/'result.json',{'ok':False,'code':'CUDA_PROBE_ERROR','retryable':False,'detail':str(exc)})
        finally:
            if probe:probe.close()
        return
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
    if spec.get("chunks") and not error:
        try:
            execute_chunks(work, spec)
        except (OSError, ValueError, KeyError):
            write_json(work / "result.json", {"ok": False, "code": "CHECKPOINT_INVALID", "retryable": True})
        return
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
