import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PIPELINE_VERSION = "animation-v2"
SPECS = [
    {"key": "inspect", "name": "校验素材", "deps": [], "resource": "cpu", "params": []},
    {"key": "normalize_extract", "name": "规范化与抽帧", "deps": ["inspect"], "resource": "cpu", "params": ["fps"]},
    {"key": "remove_background", "name": "逐帧抠图", "deps": ["normalize_extract"], "resource": "gpu", "params": ["matte_model", "seed"]},
    {"key": "enhance_frames", "name": "帧增强", "deps": ["remove_background"], "resource": "gpu", "params": ["enhance_model", "size", "seed"]},
    {"key": "build_atlas", "name": "生成图集", "deps": ["enhance_frames"], "resource": "cpu", "params": ["atlas_columns"]},
    {"key": "render_preview", "name": "生成预览", "deps": ["enhance_frames"], "resource": "cpu", "params": ["preview_format"]},
    {"key": "package", "name": "打包产物", "deps": ["build_atlas", "render_preview"], "resource": "cpu", "params": []},
]
for _spec in SPECS:
    _spec["processor_version"] = "mock-2" if _spec["key"] == "render_preview" else "mock-1"
LEGACY_SPECS = deepcopy(SPECS)
for _spec in SPECS:
    if _spec["key"] in {"remove_background", "enhance_frames"}:
        _spec["processor_version"] = "mock-chunks-1"
        _spec["partition"] = {"version": "frames-24-v1", "frames": 96, "chunk_size": 24}
BY_KEY = {s["key"]: s for s in SPECS}
OK = {"SUCCEEDED", "CACHED"}
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fps: int = Field(24, ge=1, le=60)
    size: int = Field(256, ge=64, le=1024)
    seed: int = Field(42, ge=0, le=2**31 - 1)
    matte_model: Literal["matte-mock-v1", "matte-mock-v2"] = "matte-mock-v1"
    enhance_model: Literal["enhance-mock-v1", "enhance-mock-v2"] = "enhance-mock-v1"
    atlas_columns: int = Field(4, ge=1, le=16)
    preview_format: Literal["svg", "json"] = "svg"


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field("动画素材处理", min_length=1, max_length=80)
    asset_id: Literal["orbit", "bounce"] = "orbit"
    pipeline_version: Literal["animation-v1", "animation-v2"] = PIPELINE_VERSION
    parameters: Parameters = Field(default_factory=Parameters)
    scenario: Literal["normal", "transient_once", "always_fail", "resume_preview", "gpu_oom", "invalid_input", "slow_step", "hang_step"] = "normal"
    cache_read: bool = True
    step_seconds: float = Field(2, ge=0.05, le=120)
    step_timeout_seconds: float = Field(120, ge=0.1, le=86400)
    task_timeout_seconds: float = Field(1800, ge=0.2, le=172800)
    execution_profile: Literal["demo", "hourly"] = "demo"
    gpu_mode: Literal["mock", "nvidia"] = "mock"
    gpu: 'GPURequest' = Field(default_factory=lambda: GPURequest())

    @model_validator(mode='after')
    def real_profile(self):
        if self.gpu_mode=='nvidia' and (self.pipeline_version!='animation-v2' or self.execution_profile!='demo' or self.scenario!='normal'):
            raise ValueError('NVIDIA probe requires v2/demo/normal; MOCK failure scenarios cannot substitute hardware errors')
        if self.gpu_mode=='mock' and self.gpu!=GPURequest():
            raise ValueError('GPU constraints/probe settings require explicit gpu_mode=nvidia')
        return self


class GPURequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    min_memory_mb: int = Field(512,ge=128,le=1048576)
    min_compute_major: int = Field(7,ge=7,le=20)
    uuid: str | None = Field(None,pattern=r'^GPU-[0-9a-fA-F-]{36}$')
    probe_memory_mb: int = Field(8,ge=1,le=64)
    oom_test: bool = False


TaskInput.model_rebuild()


def validate_dag(specs):
    keys = [s["key"] for s in specs]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate step key")
    done = set()
    while len(done) < len(keys):
        ready = [s["key"] for s in specs if s["key"] not in done and set(s["deps"]) <= done]
        if not ready:
            raise ValueError("Cycle or missing dependency")
        done.update(ready)


def asset_digest(asset_id):
    return hashlib.sha256((Path(__file__).parent / "assets" / f"{asset_id}.svg").read_bytes()).hexdigest()


def semantic(task, spec, parent_digests):
    result = {"cache_schema": 1, "processor": spec["key"], "processor_version": spec.get("processor_version", "mock-1"),
            "output_schema": 1, "runtime": "python-mock-1", "simulated": True,
            "parameters": {k: task.request["parameters"][k] for k in spec["params"]},
            "inputs": parent_digests or {"source": task.input_digest}}
    if spec.get("partition"):
        result["partition"] = spec["partition"]
    if task.request.get('gpu_mode')=='nvidia':
        result['execution_mode']='nvidia-probe-1'
        if spec['resource']=='gpu':
            from app.cuda_probe import PTX_SHA
            result.update(simulated=False,runtime='cuda-driver-12-ptx7-sm70-integer-v1',output_schema=2,
                probe={k:task.request['gpu'][k] for k in ['probe_memory_mb','oom_test']},ptx_sha256=PTX_SHA)
    return result


validate_dag(SPECS)
