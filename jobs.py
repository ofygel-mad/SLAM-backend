# -*- coding: utf-8 -*-
"""Менеджер фоновых задач: для блока отправляет по одной форме на каждого работника,
ведёт прогресс и пер-результаты (диагностика)."""
import threading
import uuid
import datetime as dt
import copy
from typing import Dict, List, Optional

from engine.slam_filler import SlamFiller, WorkerData


class Job:
    def __init__(self, job_id: str, block_name: str, workers: List[dict],
                 base: dict, submit: bool):
        self.id = job_id
        self.block_name = block_name
        self.submit = submit
        self.base = base                  # workplace, task, object_key, company
        self.status = "queued"            # queued|running|done|error
        self.total = len(workers)
        self.done = 0
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.results: List[dict] = [
            {"full_name": w["full_name"], "status": "pending",
             "submitted": False, "steps": [], "errors": []}
            for w in workers
        ]
        self._workers = workers
        self._lock = threading.Lock()

    def to_dict(self):
        with self._lock:
            return {
                "id": self.id, "block_name": self.block_name, "status": self.status,
                "submit": self.submit, "total": self.total, "done": self.done,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "results": copy.deepcopy(self.results),
            }

    def set_meta(self, **values):
        with self._lock:
            for key, value in values.items():
                setattr(self, key, value)

    def update_result(self, index: int, **values):
        with self._lock:
            self.results[index].update(values)

    def append_result(self, item: dict):
        with self._lock:
            self.results.append(item)

    def increment_done(self):
        with self._lock:
            self.done += 1


class JobManager:
    def __init__(self, headless: bool = True):
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self.headless = headless

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, block_name: str, workers: List[dict], base: dict,
              submit: bool) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job = Job(job_id, block_name, workers, base, submit)
        with self._lock:
            self._jobs[job_id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job: Job):
        job.set_meta(status="running", started_at=dt.datetime.utcnow().isoformat())
        filler = SlamFiller(headless=self.headless)
        try:
            for i, w in enumerate(job._workers):
                job.update_result(i, status="running")
                data = WorkerData(
                    full_name=w["full_name"],
                    workplace=job.base["workplace"],
                    task=job.base["task"],
                    object_key=job.base["object_key"],
                    company=job.base["company"],
                )
                try:
                    res = filler.fill_one(data, submit=job.submit)
                    job.update_result(
                        i,
                        steps=res.steps,
                        errors=res.errors,
                        submitted=res.submitted,
                        status="ok" if res.ok else "failed",
                    )
                except Exception as e:
                    job.update_result(i, status="failed", errors=[f"{type(e).__name__}: {e}"])
                job.increment_done()
            job.set_meta(status="done")
        except Exception as e:
            job.set_meta(status="error")
            job.append_result({"full_name": "—", "status": "failed",
                               "submitted": False, "steps": [], "errors": [str(e)]})
        finally:
            job.set_meta(finished_at=dt.datetime.utcnow().isoformat())
