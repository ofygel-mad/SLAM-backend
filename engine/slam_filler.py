# -*- coding: utf-8 -*-
"""
Движок автозаполнения формы SLAM (Microsoft Forms).
Ветка: Подрядная организация -> Сульфидная фабрика 1/2.

Заполняет ОДНУ форму на одного работника, проверяет каждое поле (диагностика)
и при submit=True нажимает «Отправить».

Использование:
    from engine.slam_filler import SlamFiller, WorkerData
    data = WorkerData(full_name="Иванов И.И.", workplace="...", task="montazh",
                      object_key="sulphide_1", company="ТОО Ромашка")
    result = SlamFiller().fill_one(data, submit=False)
    print(result.ok, result.steps)
"""
from dataclasses import dataclass, field
from typing import List, Optional
import time

from playwright.sync_api import sync_playwright, Page

from . import answers as A

FORM_URL = (
    "https://forms.office.com/pages/responsepage.aspx?"
    "id=z_7mWGUcvUKsB3AP7auruNSKV8FZLQpGiZMpsAQgRdlUNlVYWFpURlpRODBEMjlLVktRR0RLS1ZLNS4u"
    "&origin=QRCode&route=shorturl"
)

SEL_QITEM = '[data-automation-id="questionItem"]'
SEL_TITLE = '[data-automation-id="questionTitle"]'
SEL_TEXT = '[data-automation-id="textInput"]'
SEL_CHOICE = '[data-automation-id="choiceItem"]'
SEL_NEXT = '[data-automation-id="nextButton"]'
SEL_SUBMIT = '[data-automation-id="submitButton"]'
SEL_SECTION = '[data-automation-id="sectionTitle"]'


@dataclass
class WorkerData:
    full_name: str
    workplace: str
    task: str                 # "montazh" | "demontazh"
    object_key: str           # "sulphide_1" | "sulphide_2"
    company: str              # название подрядной организации (пойдёт в "Другое")


@dataclass
class FillResult:
    ok: bool = False
    submitted: bool = False
    steps: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def log(self, msg: str):
        self.steps.append(msg)

    def fail(self, msg: str):
        self.errors.append(msg)
        self.ok = False


# ---------- низкоуровневые помощники ----------

def _norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def _qitems(page: Page):
    return page.query_selector_all(SEL_QITEM)


def _section(page: Page) -> str:
    el = page.query_selector(SEL_SECTION)
    return el.inner_text().strip() if el else ""


def _choice_questions(page: Page):
    return [q for q in _qitems(page) if q.query_selector_all(SEL_CHOICE)]


def _click_choice(qitem, substr: str) -> Optional[str]:
    sub = _norm(substr)
    for c in qitem.query_selector_all(SEL_CHOICE):
        if sub in _norm(c.inner_text()):
            c.click()
            return c.inner_text().strip().replace("\n", " ")
    return None


def _question_title(qitem) -> str:
    t = qitem.query_selector(SEL_TITLE)
    return t.inner_text().strip().replace("\n", " ") if t else ""


def _wait_render(page, timeout=8.0):
    """Дать форме дорисовать появившиеся (branching) вопросы."""
    time.sleep(1.2)


# ---------- основной движок ----------

class SlamFiller:
    def __init__(self, headless: bool = True, slow_mo: int = 0):
        self.headless = headless
        self.slow_mo = slow_mo

    def fill_one(self, data: WorkerData, submit: bool = False,
                 screenshot_prefix: Optional[str] = None) -> FillResult:
        res = FillResult(ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless, slow_mo=self.slow_mo)
            page = browser.new_page(viewport={"width": 1280, "height": 1100})
            try:
                self._open(page, res)
                self._page1(page, data, res)
                self._advance(page, res)  # -> readiness/hazard pages
                # Страницы 2..N: да/нет + (последняя) тексты опасностей
                while True:
                    sec = _section(page)
                    has_submit = bool(page.query_selector(SEL_SUBMIT))
                    has_next = bool(page.query_selector(SEL_NEXT))
                    text_qs = page.query_selector_all(SEL_TEXT)

                    if text_qs and (has_submit or not has_next):
                        # финальная страница опасностей
                        self._hazard_page(page, res)
                    else:
                        self._yesno_page(page, sec, res)

                    if screenshot_prefix:
                        page.screenshot(path=f"{screenshot_prefix}_sec_{_norm(sec)[:12]}.png",
                                        full_page=True)

                    if page.query_selector(SEL_SUBMIT) and not page.query_selector(SEL_NEXT):
                        break
                    if not page.query_selector(SEL_NEXT):
                        res.fail(f"Нет кнопки Next и нет Submit на разделе '{sec}'")
                        break
                    self._advance(page, res)

                # Диагностика финальной страницы перед отправкой
                self._verify_before_submit(page, res)

                if res.ok and submit:
                    page.click(SEL_SUBMIT)
                    time.sleep(3)
                    # подтверждение отправки
                    body = _norm(page.inner_text("body"))
                    if any(m in body for m in ["ваш ответ", "response was submitted",
                                               "thank", "спасибо", "жауабыңыз"]):
                        res.submitted = True
                        res.log("✅ Форма отправлена (подтверждение получено).")
                    else:
                        res.submitted = True
                        res.log("Submit нажат (текст подтверждения не распознан — проверьте вручную).")
                    if screenshot_prefix:
                        page.screenshot(path=f"{screenshot_prefix}_submitted.png", full_page=True)
                elif res.ok:
                    res.log("Все поля заполнены и проверены. Submit НЕ нажат (submit=False).")
            except Exception as e:
                res.fail(f"Исключение: {type(e).__name__}: {e}")
                if screenshot_prefix:
                    try:
                        page.screenshot(path=f"{screenshot_prefix}_error.png", full_page=True)
                    except Exception:
                        pass
            finally:
                browser.close()
        return res

    # ----- этапы -----

    def _open(self, page, res):
        page.goto(FORM_URL, wait_until="domcontentloaded", timeout=60000)
        page.get_by_role("button", name="Start now").last.click(timeout=30000)
        page.wait_for_selector(SEL_TEXT, timeout=40000)
        _wait_render(page)
        res.log("Форма открыта, нажата кнопка «Start now».")

    def _page1(self, page, data: WorkerData, res):
        # 3 текстовых поля: ФИО, рабочее место, описание задания
        texts = page.query_selector_all(SEL_TEXT)
        if len(texts) < 3:
            res.fail(f"Ожидалось 3 текстовых поля на стр.1, найдено {len(texts)}")
            return
        task_text = A.WORK_TASK.get(data.task)
        if not task_text:
            res.fail(f"Неизвестный тип задания: {data.task}")
            return
        texts[0].fill(data.full_name)
        texts[1].fill(data.workplace)
        texts[2].fill(task_text)
        res.log(f"Стр.1: ФИО='{data.full_name}', место='{data.workplace}', задание='{task_text}'")

        # Q4: департамент -> Подрядная организация
        cqs = _choice_questions(page)
        if not cqs:
            res.fail("Стр.1: не найден вопрос выбора департамента (Q4)")
            return
        picked = _click_choice(cqs[0], A.DEPARTMENT_CONTRACTOR)
        if not picked:
            res.fail("Стр.1: не удалось выбрать «Подрядная организация» (Q4)")
            return
        res.log(f"Q4 -> {picked}")
        _wait_render(page)

        # Q5: объект -> Сульфидная фабрика 1/2
        obj_text = A.OBJECT_OPTIONS.get(data.object_key)
        if not obj_text:
            res.fail(f"Неизвестный объект: {data.object_key}")
            return
        cqs = _choice_questions(page)
        if len(cqs) < 2:
            res.fail("Стр.1: не появился вопрос объекта (Q5)")
            return
        picked = _click_choice(cqs[1], obj_text)
        if not picked:
            res.fail(f"Стр.1: не удалось выбрать объект «{obj_text}» (Q5)")
            return
        res.log(f"Q5 -> {picked}")
        _wait_render(page)

        # Q6: наименование подрядной организации -> "Другое" + текст
        q6 = None
        for q in _qitems(page):
            if "наименование подрядной" in _norm(_question_title(q)):
                q6 = q
                break
        if not q6:
            res.fail("Стр.1: не появился вопрос «Наименование подрядной организации» (Q6)")
            return
        # выбрать "Other" (последняя опция с aria 'Other answer') и вписать компанию
        other_radio = q6.query_selector('[role=radio][aria-label="Other answer"]')
        if other_radio:
            other_radio.click()
        ti = q6.query_selector(SEL_TEXT)
        if not ti:
            res.fail("Стр.1: не найдено поле «Другое» в Q6")
            return
        ti.fill(data.company)
        res.log(f"Q6 -> Другое: '{data.company}'")
        _wait_render(page)

    def _yesno_page(self, page, section, res):
        is_hazard = any(m in _norm(section) for m in A.HAZARD_SECTION_MARKERS)
        answer = A.NO if is_hazard else A.YES
        cqs = _choice_questions(page)
        if not cqs:
            res.fail(f"Раздел '{section}': не найдено вопросов да/нет")
            return
        for q in cqs:
            picked = _click_choice(q, answer)
            if not picked:
                res.fail(f"Раздел '{section}': не удалось выбрать «{answer}» в "
                         f"«{_question_title(q)[:50]}»")
        res.log(f"Раздел '{section[:40]}': {len(cqs)} вопрос(ов) -> «{answer}»")

    def _hazard_page(self, page, res):
        order = [
            A.HAZARDS["hazard_1"], A.HAZARDS["control_1"],
            A.HAZARDS["hazard_2"], A.HAZARDS["control_2"],
            A.HAZARDS["hazard_3"], A.HAZARDS["control_3"],
            A.HAZARDS["hazard_4"], A.HAZARDS["control_4"],
            A.HAZARDS["remarks"],
        ]
        texts = page.query_selector_all(SEL_TEXT)
        if len(texts) < len(order):
            res.fail(f"Стр. опасностей: ожидалось {len(order)} полей, найдено {len(texts)}")
            return
        for el, val in zip(texts, order):
            el.fill(val)
        res.log(f"Страница опасностей: заполнено {len(order)} текстовых поля.")

    def _advance(self, page, res):
        page.click(SEL_NEXT)
        time.sleep(2.2)
        alert = page.query_selector('[role=alert]')
        if alert:
            txt = alert.inner_text().strip()
            if "need to be completed" in txt.lower() or "необходимо" in txt.lower():
                res.fail(f"Валидация не пройдена: {txt[:120]}")

    def _verify_before_submit(self, page, res):
        """Финальная диагностика: проверяем, что на текущей (submit) странице
        все обязательные текстовые поля заполнены."""
        problems = []
        for q in _qitems(page):
            ti = q.query_selector(SEL_TEXT)
            required = bool(q.query_selector('[data-automation-id="requiredStar"]'))
            if ti and required and not ti.input_value().strip():
                problems.append(_question_title(q)[:60])
        if problems:
            res.fail("Пустые обязательные поля: " + "; ".join(problems))
        else:
            res.log("Диагностика: обязательные поля на финальной странице заполнены.")
