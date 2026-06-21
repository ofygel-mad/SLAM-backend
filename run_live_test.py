# -*- coding: utf-8 -*-
"""ОДНА реальная отправка для проверки боевого режима (submit=True).
Данные намеренно помечены как тест."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
from engine.slam_filler import SlamFiller, WorkerData

data = WorkerData(
    full_name="ТЕСТ СИСТЕМЫ (не учитывать)",
    workplace="ТЕСТ — проверка автоматизации SLAM, прошу проигнорировать",
    task="montazh",
    object_key="sulphide_1",
    company="ТЕСТ",
)

res = SlamFiller(headless=True).fill_one(
    data, submit=True, screenshot_prefix="../screenshots/live"
)
print("\n=== РЕЗУЛЬТАТ БОЕВОЙ ОТПРАВКИ ===")
print("OK:", res.ok, "| submitted:", res.submitted)
for s in res.steps:
    print("  •", s)
for e in res.errors:
    print("  ✗", e)
