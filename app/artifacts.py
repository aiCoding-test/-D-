"""Small, immutable artifact sets. No database I/O and no user-controlled paths."""
import hashlib
import json
import os
from pathlib import Path

from app.config import settings
from app.pipeline import digest


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def folder(key):
    root = settings.artifact_root
    path = (root / key).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Invalid storage key")
    return path


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())


def seal(work, semantic_key):
    files = []
    for p in sorted(work.iterdir()):
        if (p.is_file() and p.name not in {"manifest.json", "progress.json", "result.json", "spec.json", "executor.log"}
                and not p.name.startswith(("chunk-ready-", "chunk-ack-"))):
            files.append({"key": p.name, "sha256": file_hash(p), "size": p.stat().st_size})
    if not files:
        raise ValueError("Empty artifact")
    manifest = {"schema": 1, "simulated": True, "semantic_key": semantic_key, "files": files,
                "content_digest": digest(files)}
    write_json(work / "manifest.json", manifest)
    return manifest


def verify(directory, expected_digest):
    try:
        path = folder(directory)
        if file_hash(path / "manifest.json") != expected_digest:
            return False
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("schema") != 1 or manifest.get("simulated") is not True or not manifest["files"]:
            return False
        for f in manifest["files"]:
            if Path(f["key"]).name != f["key"]:
                return False
            p = path / f["key"]
            if p.is_symlink() or not p.is_file() or p.stat().st_size != f["size"] or file_hash(p) != f["sha256"]:
                return False
        return digest(manifest["files"]) == manifest["content_digest"]
    except (OSError, ValueError, KeyError, TypeError):
        return False


def publish(attempt_id):
    return publish_directory(f"work/{attempt_id}", f"published/{attempt_id}")


def publish_directory(source, destination):
    work = folder(source)
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    expected = file_hash(work / "manifest.json")
    if not verify(source, expected):
        raise ValueError("ARTIFACT_INVALID")
    dest = folder(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        # Rename stays within one mounted volume; never overwrite another attempt.
        os.rename(work, dest)
        if os.name == "posix":
            fd = os.open(dest.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    if not verify(destination, expected):
        raise ValueError("ARTIFACT_INVALID")
    return {"directory": destination, "manifest_digest": expected,
            "content_digest": manifest["content_digest"], "manifest": manifest}
