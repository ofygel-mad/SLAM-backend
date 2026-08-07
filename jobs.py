# -*- coding: utf-8 -*-
"""Менеджер фоновых задач: для блока отправляет по одной форме на каждого
работника, ведёт прогресс и пер-результаты (диагностика).

Один браузер на всю задачу: раньше он поднимался и гасился на каждого
работника, что добавляло по несколько секунд к каждой форме.
"""
import copy
import datetime as dt
import threading
import uuid
from collections import OrderedDict
from typing import Dict, List, Optional

from engine.slam_filler import SlamFiller, WorkerData
from engine import answers as A

# Сколько завершённых задач держать в памяти (иначе процесс растёт бесконечно).
MAX_JOBS = 200


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


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
        self.created_at = _now()
        self.results: List[dict] = [
            {"worker_id": w.get("id"), "full_name": w["full_name"],
             "status": "pending", "submitted": False,
             "steps": [], "errors": [], "warnings": [], "fields": [], "elapsed": 0.0}
            for w in workers
        ]
        self._workers = workers
        self._lock = threading.Lock()

    def to_dict(self, with_fields: bool = False):
        """with_fields=False — «лёгкий» ответ для опроса прогресса.

        Протокол полей одного работника весит ~30 КБ (одни только тексты мер
        контроля), и гонять его каждые полторы секунды по мобильной сети
        незачем: вместо него отдаём счётчики, а сам протокол — по запросу
        (GET /api/jobs/{id}/fields/{index}), когда пользователь раскроет плашку.
        """
        with self._lock:
            results = copy.deepcopy(self.results)
        for r in results:
            fields = r.get("fields") or []
            r["fields_total"] = len(fields)
            r["fields_verified"] = sum(1 for f in fields if f.get("verified"))
            if not with_fields:
                r.pop("fields", None)
        return {
            "id": self.id, "block_name": self.block_name, "status": self.status,
            "submit": self.submit, "total": self.total, "done": self.done,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "base": dict(self.base), "results": results,
        }

    def fields_of(self, index: int) -> Optional[list]:
        with self._lock:
            if 0 <= index < len(self.results):
                return copy.deepcopy(self.results[index].get("fields") or [])
        return None

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
    def __init__(self, headless: bool = True, max_concurrent: int = 1):
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._lock = threading.Lock()
        self.headless = headless
        # Каждая задача держит свой chromium (сотни МБ). Без ограничения две
        # бригады, нажавшие «Отправить» одновременно, кладут контейнер.
        # Лишние задачи честно ждут в статусе «в очереди».
        self._slots = threading.Semaphore(max(1, max_concurrent))

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, block_name: str, workers: List[dict], base: dict,
              submit: bool) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job = Job(job_id, block_name, workers, base, submit)
        with self._lock:
            self._jobs[job_id] = job
            self._evict_locked()
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _evict_locked(self):
        """Выбросить самые старые завершённые задачи сверх лимита."""
        while len(self._jobs) > MAX_JOBS:
            for jid, j in list(self._jobs.items()):
                if j.status in ("done", "error"):
                    del self._jobs[jid]
                    break
            else:
                break        # все оставшиеся ещё выполняются — не трогаем

    def _run(self, job: Job):
        with self._slots:
            self._run_locked(job)

    def _run_locked(self, job: Job):
        job.set_meta(status="running", started_at=_now())
        try:
            # sync-Playwright привязан к потоку — поднимаем браузер здесь,
            # внутри рабочего потока, и держим один на всю бригаду.
            with SlamFiller(headless=self.headless) as filler:
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
                            warnings=res.warnings,
                            fields=res.fields,
                            submitted=res.submitted,
                            elapsed=res.elapsed,
                            status=res.status,   # ok | unconfirmed | failed
                        )
                    except Exception as e:
                        job.update_result(i, status="failed",
                                          errors=[f"{type(e).__name__}: {e}"])
                    job.increment_done()
            job.set_meta(status="done")
        except Exception as e:
            job.set_meta(status="error")
            job.append_result({"worker_id": None, "full_name": "—", "status": "failed",
                               "submitted": False, "steps": [], "warnings": [],
                               "fields": [], "elapsed": 0.0,
                               "errors": [f"Сбой браузера: {type(e).__name__}: {e}"]})
            # Работники, до которых не дошли, не должны выглядеть «ожидающими».
            for i, r in enumerate(job.results):
                if r["status"] in ("pending", "running"):
                    job.update_result(i, status="failed",
                                      errors=r["errors"] + ["Задача прервана сбоем браузера."])
        finally:
            job.set_meta(finished_at=_now())

    # ---- предпросмотр сценария (то же, что покажет фронтенд) ----
    @staticmethod
    def preview(full_name: str, workplace: str, task: str, object_key: str,
                company: str) -> list:
        return A.resolve_plan(full_name, workplace, task, object_key, company)
