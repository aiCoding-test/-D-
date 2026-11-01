import uuid
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app import artifacts, queries, service
from app.db import Session
from app.models import Artifact, Attempt, Event, Step, Task
from app.pipeline import SPECS, TaskInput
from app.logging_setup import configure

configure()
app = FastAPI(title="Task D · Observable Pipeline", version="0.1.0")
base = Path(__file__).parent
templates = Jinja2Templates(directory=str(base / "templates"))
app.mount("/static", StaticFiles(directory=base / "static"), name="static")


@app.middleware("http")
async def request_id(request: Request, call_next):
    request.state.request_id = str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(service.Problem)
async def problem_handler(request, exc):
    return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": exc.message,
                        "request_id": request.state.request_id})


@app.exception_handler(SQLAlchemyError)
async def database_handler(request, exc):
    return JSONResponse(status_code=503, content={"code": "DATABASE_UNAVAILABLE", "message": "数据库暂不可用，请稍后重试",
                        "retryable": True, "request_id": request.state.request_id})


@app.get("/", include_in_schema=False)
def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})


@app.get("/tasks/{tid}", include_in_schema=False)
def task_page(request: Request, tid: str):
    return templates.TemplateResponse(request=request, name="task.html", context={"task_id": tid})


@app.get("/health/live")
def live():
    return {"ok": True}


@app.get("/health/ready")
def ready():
    with Session() as s:
        s.execute(text("SELECT 1"))
    if not artifacts.settings.artifact_root.is_dir():
        raise service.Problem("STORAGE_UNAVAILABLE", "Artifact volume is unavailable", 503)
    return {"ok": True, "database": "postgresql"}


@app.get("/api/pipeline")
def pipeline():
    return {"version": "animation-v1", "steps": SPECS, "simulated": True}


Idempotency = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


@app.post("/api/tasks", status_code=202)
def create(data: TaskInput, idempotency_key: Idempotency):
    return service.submit(data, idempotency_key)


@app.get("/api/tasks")
def list_tasks(status: str | None = None, cursor: str | None = None, limit: int = Query(30, ge=1, le=100)):
    if status and status not in {"QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLING", "CANCELLED"}:
        raise service.Problem("INVALID_STATUS", "Unknown task status", 422)
    return queries.tasks(status, cursor, limit)


@app.get("/api/tasks/{tid}")
def task_detail(tid: str):
    return queries.detail(tid)


@app.get("/api/tasks/{tid}/events")
def events(tid: str, after_seq: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    with Session.begin() as s:
        s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        if not s.get(Task, tid):
            raise service.Problem("NOT_FOUND", "Task not found", 404)
        items = [queries.row(e) for e in s.scalars(select(Event).where(Event.task_id == tid, Event.seq > after_seq).order_by(Event.seq).limit(limit))]
        return {"items": items, "next_cursor": items[-1]["seq"] if items else after_seq}


@app.get("/api/tasks/{tid}/steps/{key}/attempts")
def attempts(tid: str, key: str):
    with Session() as s:
        step = s.scalar(select(Step).where(Step.task_id == tid, Step.key == key))
        if not step:
            raise service.Problem("NOT_FOUND", "Step not found", 404)
        return {"items": [queries.row(a) for a in s.scalars(select(Attempt).where(Attempt.step_id == step.id).order_by(Attempt.number))]}


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int | None = Field(None, ge=1)


@app.post("/api/tasks/{tid}/resume", status_code=202)
def resume(tid: str, body: Control, idempotency_key: Idempotency):
    return service.command(tid, "resume", idempotency_key, body.expected_revision)


@app.post("/api/tasks/{tid}/steps/{key}/retry", status_code=202)
def retry_step(tid: str, key: str, body: Control, idempotency_key: Idempotency):
    return service.command(tid, "resume", idempotency_key, body.expected_revision, key)


@app.post("/api/tasks/{tid}/cancel", status_code=202)
def cancel(tid: str, body: Control, idempotency_key: Idempotency):
    return service.command(tid, "cancel", idempotency_key, body.expected_revision)


@app.get("/api/tasks/{tid}/artifacts")
def task_artifacts(tid: str):
    return {"items": [x["artifact"] for x in queries.detail(tid)["steps"] if x["artifact"]]}


@app.get("/api/artifacts/{aid}/files/{file_key}")
def download(aid: str, file_key: str):
    with Session() as s:
        a = s.get(Artifact, aid)
        if not a:
            raise service.Problem("NOT_FOUND", "Artifact not found", 404)
        if not a.valid or not artifacts.verify(a.directory, a.manifest_digest):
            raise service.Problem("ARTIFACT_UNAVAILABLE", "产物缺失或校验失败", 410)
        allowed = {f["key"] for f in a.manifest["files"]} | {"manifest.json"}
        if file_key not in allowed:
            raise service.Problem("NOT_FOUND", "File not in manifest", 404)
        return FileResponse(artifacts.folder(a.directory) / file_key, filename=file_key)


@app.get("/api/resources")
def resources():
    return queries.resources()


@app.get("/api/overview")
def overview():
    with Session() as s:
        counts = dict(s.execute(select(Task.status, func.count()).group_by(Task.status)).all())
        return {"tasks": counts, "attempts": s.scalar(select(func.count()).select_from(Attempt)),
                "cached_steps": s.scalar(select(func.count()).select_from(Step).where(Step.status == "CACHED"))}
