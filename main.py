# -*- coding: utf-8 -*-
"""FastAPI: управление блоками/работниками + запуск автозаполнения SLAM.
API-only сервис (фронтенд деплоится отдельным проектом)."""
import os
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db import init_db, get_session, Block, Worker
from jobs import JobManager

# headless из окружения (на сервере — да). HEADLESS=0 для отладки с окном.
HEADLESS = os.environ.get("HEADLESS", "1") != "0"
# Разрешённые источники для фронтенда (через запятую). По умолчанию — любой.
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()] or ["*"]

TaskKey = Literal["montazh", "demontazh"]
ObjectKey = Literal["sulphide_1", "sulphide_2"]

app = FastAPI(title="SLAM Auto-Fill API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
jobs = JobManager(headless=HEADLESS)


@app.on_event("startup")
def _startup():
    init_db()


def db() -> Session:
    s = get_session()
    try:
        yield s
    finally:
        s.close()


# ---------- схемы ----------
class BlockIn(BaseModel):
    name: str = Field(..., max_length=200)
    company: str = Field(default="", max_length=300)
    object_key: ObjectKey = "sulphide_1"
    task: TaskKey = "montazh"


class BlockPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    company: Optional[str] = Field(default=None, max_length=300)
    object_key: Optional[ObjectKey] = None
    task: Optional[TaskKey] = None


class WorkerIn(BaseModel):
    full_name: str = Field(default="", max_length=300)


class SubmitIn(BaseModel):
    workplace: str = Field(..., max_length=1000)
    submit: bool = True          # «Отправить» в UI = реальная отправка
    # необязательные переопределения (иначе берутся из блока)
    task: Optional[TaskKey] = None
    object_key: Optional[ObjectKey] = None
    company: Optional[str] = Field(default=None, max_length=300)


# ---------- служебное ----------
@app.get("/")
def root():
    return {"service": "SLAM Auto-Fill API", "ok": True}


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------- блоки ----------
@app.get("/api/blocks")
def list_blocks(s: Session = Depends(db)):
    return [b.to_dict() for b in s.query(Block).order_by(Block.id).all()]


@app.post("/api/blocks")
def create_block(data: BlockIn, s: Session = Depends(db)):
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "Не указано название бригады")
    b = Block(name=name, company=data.company.strip(),
              object_key=data.object_key, task=data.task)
    s.add(b); s.commit(); s.refresh(b)
    return b.to_dict()


@app.patch("/api/blocks/{block_id}")
def update_block(block_id: int, data: BlockPatch, s: Session = Depends(db)):
    b = s.get(Block, block_id)
    if not b:
        raise HTTPException(404, "Блок не найден")
    for f in ("name", "company", "object_key", "task"):
        v = getattr(data, f)
        if v is not None:
            if isinstance(v, str):
                v = v.strip()
            if f == "name" and not v:
                raise HTTPException(400, "Не указано название бригады")
            setattr(b, f, v)
    s.commit(); s.refresh(b)
    return b.to_dict()


@app.delete("/api/blocks/{block_id}")
def delete_block(block_id: int, s: Session = Depends(db)):
    b = s.get(Block, block_id)
    if not b:
        raise HTTPException(404, "Блок не найден")
    s.delete(b); s.commit()
    return {"ok": True}


# ---------- работники ----------
@app.post("/api/blocks/{block_id}/workers")
def add_worker(block_id: int, data: WorkerIn, s: Session = Depends(db)):
    b = s.get(Block, block_id)
    if not b:
        raise HTTPException(404, "Блок не найден")
    idx = len(b.workers)
    w = Worker(block_id=block_id, full_name=data.full_name.strip(), order_index=idx)
    s.add(w); s.commit(); s.refresh(b)
    return b.to_dict()


@app.patch("/api/workers/{worker_id}")
def update_worker(worker_id: int, data: WorkerIn, s: Session = Depends(db)):
    w = s.get(Worker, worker_id)
    if not w:
        raise HTTPException(404, "Работник не найден")
    w.full_name = data.full_name.strip()
    s.commit()
    return {"ok": True}


@app.delete("/api/workers/{worker_id}")
def delete_worker(worker_id: int, s: Session = Depends(db)):
    w = s.get(Worker, worker_id)
    if not w:
        raise HTTPException(404, "Работник не найден")
    s.delete(w); s.commit()
    return {"ok": True}


# ---------- запуск автозаполнения ----------
@app.post("/api/blocks/{block_id}/submit")
def submit_block(block_id: int, data: SubmitIn, s: Session = Depends(db)):
    b = s.get(Block, block_id)
    if not b:
        raise HTTPException(404, "Блок не найден")
    workers = []
    for w in b.workers:
        full_name = (w.full_name or "").strip()
        if full_name:
            item = w.to_dict()
            item["full_name"] = full_name
            workers.append(item)
    if not workers:
        raise HTTPException(400, "В блоке нет работников")
    workplace = data.workplace.strip()
    if not workplace:
        raise HTTPException(400, "Не указано наименование рабочего места")

    # переопределения сохраняем в блок (кроме workplace — он не хранится)
    if data.task: b.task = data.task
    if data.object_key: b.object_key = data.object_key
    if data.company is not None: b.company = data.company.strip()
    if not (b.company or "").strip():
        raise HTTPException(400, "Не указана подрядная организация")
    s.commit()

    base = {
        "workplace": workplace,
        "task": b.task,
        "object_key": b.object_key,
        "company": b.company.strip(),
    }
    job = jobs.start(b.name, workers, base, submit=data.submit)
    return job.to_dict()


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    return job.to_dict()
