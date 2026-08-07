# -*- coding: utf-8 -*-
"""Тестовый прогон движка БЕЗ отправки (submit=False).
Заполняет форму тестовыми данными, проверяет поля, делает скриншоты."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from engine.slam_filler import SlamFiller, WorkerData

data = WorkerData(
    full_name="Иванов Иван Иванович",
    workplace="Цех №3, отметка +12.000, ремонт площадки обслуживания",
    task="montazh",                 # montazh | demontazh
    object_key="sulphide_1",        # sulphide_1 | sulphide_2
    company="ТОО \"СтройМонтаж Сервис\"",
)

res = SlamFiller(headless=True).fill_one(
    data, submit=False, screenshot_prefix="../screenshots/test"
)

print("\n================ РЕЗУЛЬТАТ ================")
print(f"Статус: {res.status} | submitted: {res.submitted} | {res.elapsed} с")

bad = [f for f in res.fields if not f["verified"]]
print(f"\n--- ПОЛЯ: {len(res.fields) - len(bad)}/{len(res.fields)} сверено ---")
for f in res.fields:
    print(f"  [{'V' if f['verified'] else 'x'}] {f['q'][:64]}")
    print(f"      -> {f['value'][:88]}{'…' if len(f['value']) > 88 else ''}")
    if f["note"]:
        print(f"      прим.: {f['note']}")

print("\n--- ШАГИ ---")
for s in res.steps:
    print("  •", s)
if res.warnings:
    print("\n--- ПРЕДУПРЕЖДЕНИЯ ---")
    for w in res.warnings:
        print("  !", w)
if res.errors:
    print("\n--- ОШИБКИ ---")
    for e in res.errors:
        print("  ✗", e)
print("==========================================")
