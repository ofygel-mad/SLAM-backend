# -*- coding: utf-8 -*-
"""FastAPI: управление блоками/работниками + запуск автозаполнения SLAM.
API-only сервис (фронтенд деплоится отдельным проектом)."""
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import init_db, get_session, Block, Worker
from jobs import JobManager

# headless из окружения (на сервере — да). HEADLESS=0 для отладки с окном.
HEADLESS = os.environ.get("HEADLESS", "1") != "0"
# Разрешённые источники для фронтенда (через запятую). По умолчанию — любой.
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

app = FastAPI(title="SLAM Auto-Fill API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGINS],
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
    name: str
    company: str = ""
    object_key: str = "sulphide_1"
    task: str = "montazh"


class BlockPatch(BaseModel):
    name: Optional[str] = None
    company: Optional[str] = None
    object_key: Optional[str] = None
    task: Optional[str] = None


class WorkerIn(BaseModel):
    full_name: str


class SubmitIn(BaseModel):
    workplace: str
    submit: bool = True          # «Отправить» в UI = реальная отправка
    # необязательные переопределения (иначе берутся из блока)
    task: Optional[str] = None
    object_key: Optional[str] = None
    company: Optional[str] = None


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
    b = Block(name=data.name, company=data.company,
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
    w = Worker(block_id=block_id, full_name=data.full_name, order_index=idx)
    s.add(w); s.commit(); s.refresh(b)
    return b.to_dict()


@app.patch("/api/workers/{worker_id}")
def update_worker(worker_id: int, data: WorkerIn, s: Session = Depends(db)):
    w = s.get(Worker, worker_id)
    if not w:
        raise HTTPException(404, "Работник не найден")
    w.full_name = data.full_name
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
    workers = [w.to_dict() for w in b.workers]
    if not workers:
        raise HTTPException(400, "В блоке нет работников")
    if not data.workplace.strip():
        raise HTTPException(400, "Не указано наименование рабочего места")

    # переопределения сохраняем в блок (кроме workplace — он не хранится)
    if data.task: b.task = data.task
    if data.object_key: b.object_key = data.object_key
    if data.company is not None: b.company = data.company
    s.commit()

    base = {
        "workplace": data.workplace.strip(),
        "task": b.task,
        "object_key": b.object_key,
        "company": b.company,
    }
    job = jobs.start(b.name, workers, base, submit=data.submit)
    return job.to_dict()


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    return job.to_dict()
