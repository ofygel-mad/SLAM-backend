# -*- coding: utf-8 -*-
"""
Движок автозаполнения формы SLAM (Microsoft Forms).
Ветка: Подрядная организация -> Сульфидная фабрика 1/2.

Принципы (важно — от них зависит доверие к отчёту):

  1. Каждое записанное значение перечитывается из DOM. Текст сверяется
     посимвольно, выбор — по aria-checked. В отчёт попадает то, что реально
     оказалось в поле, а не то, что мы собирались записать.
  2. Переход «Далее» считается успешным только если форма реально сменила
     страницу. Сама Microsoft Forms не пускает дальше при незаполненном
     обязательном вопросе — это независимая проверка нашей работы.
  3. Отправка считается состоявшейся ТОЛЬКО при распознанной странице
     подтверждения. Если подтверждения нет — статус «не подтверждено»
     (submitted=False), а не «отправлено».
  4. В конце фактически записанное сверяется со сценарием answers.build_plan —
     тем же, что показывается пользователю в плашке предпросмотра.

Использование:
    from engine.slam_filler import SlamFiller, WorkerData
    data = WorkerData(full_name="Иванов И.И.", workplace="...", task="montazh",
                      object_key="sulphide_1", company="ТОО Ромашка")
    # один работник (браузер поднимается и гасится сам):
    result = SlamFiller().fill_one(data, submit=False)
    # бригада (один браузер на всех — заметно быстрее):
    with SlamFiller() as f:
        for d in datas:
            f.fill_one(d, submit=True)
"""
import re
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from playwright.sync_api import (sync_playwright,
                                 TimeoutError as PlaywrightTimeoutError)

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
SEL_REQUIRED = '[data-automation-id="requiredStar"]'

# Признаки страницы «ответ отправлен» (форма трёхъязычная).
CONFIRM_MARKERS = [
    "ваш ответ был отправлен", "ваш ответ отправлен", "ответ отправлен",
    "your response was submitted", "response was submitted",
    "жауабыңыз жіберілді", "жауабыңыз тіркелді",
    "отправить ещё один ответ", "отправить еще один ответ",
    "submit another response", "спасибо", "thank you",
]

MAX_PAGES = 12           # страховка от зацикливания на непереключающейся форме
DEFAULT_BUDGET = 300.0   # секунд на одного работника


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
    unconfirmed: bool = False          # submit нажат, подтверждения не увидели
    steps: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    fields: List[dict] = field(default_factory=list)   # что реально записано
    elapsed: float = 0.0

    @property
    def status(self) -> str:
        if not self.ok or self.errors:
            return "failed"
        if self.unconfirmed:
            return "unconfirmed"
        return "ok"

    def log(self, msg: str):
        self.steps.append(msg)

    def warn(self, msg: str):
        self.warnings.append(msg)

    def fail(self, msg: str):
        self.errors.append(msg)
        self.ok = False

    def record(self, section: str, question: str, kind: str, value: str,
               verified: bool, note: str = "", key: str = ""):
        """Занести в протокол фактически записанное значение."""
        self.fields.append({
            "section": section, "q": question, "kind": kind, "value": value,
            "verified": verified, "note": note, "key": key,
        })


# ---------- низкоуровневые помощники ----------

def _norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def _segments(text: str) -> List[str]:
    """Варианты формы трёхъязычные: «Да/ Иә/Yes». Режем на части, чтобы
    сравнивать точно, а не подстрокой (иначе «Да» ловится в «Нет данных»)."""
    return [seg for seg in (_norm(x) for x in re.split(r"[/|]", text or "")) if seg]


def _poll(fn: Callable, timeout: float = 10.0, interval: float = 0.12):
    """Ждать, пока fn() вернёт истину. Возвращает значение или None по таймауту."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            value = fn()
        except Exception:
            value = None
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def _qitems(page):
    return page.query_selector_all(SEL_QITEM)


def _section(page) -> str:
    el = page.query_selector(SEL_SECTION)
    return el.inner_text().strip() if el else ""


def _question_title(qitem) -> str:
    t = qitem.query_selector(SEL_TITLE)
    return t.inner_text().strip().replace("\n", " ") if t else ""


def _choice_questions(page):
    return [q for q in _qitems(page) if q.query_selector_all(SEL_CHOICE)]


_SIG_JS = """() => {
  const sec = document.querySelector('[data-automation-id="sectionTitle"]');
  const qs = Array.from(document.querySelectorAll('[data-automation-id="questionTitle"]'));
  return (sec ? sec.innerText.trim() : '') + '\\u0001' + qs.length + '\\u0001'
       + qs.map(q => q.innerText.trim().slice(0, 60)).join('\\u0002');
}"""


def _signature(page) -> str:
    """Отпечаток страницы: раздел + заголовки вопросов. Меняется при переходе.

    Считается одним вызовом в браузере: _settle опрашивает его в цикле, а
    поэлементный обход стоил бы N+1 IPC-раундтрипов на каждую итерацию."""
    try:
        return page.evaluate(_SIG_JS)
    except Exception:
        return ""


def _settle(page, quiet: float = 0.3, timeout: float = 8.0):
    """Дождаться, пока форма перестанет дорисовывать вопросы (branching).

    Заменяет прежние глухие time.sleep(): в типовом случае возвращает управление
    за ~0.4 с вместо 1.2 с, а на медленной сети честно ждёт дольше."""
    deadline = time.monotonic() + timeout
    last, stable_since = None, None
    while time.monotonic() < deadline:
        sig = _signature(page)
        if sig == last and sig[1]:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= quiet:
                return sig
        else:
            last, stable_since = sig, None
        time.sleep(0.08)
    return _signature(page)


def _find_question(page, marker: str):
    """Найти вопрос по подстроке заголовка (маркеры — в answers.py)."""
    m = _norm(marker)
    for q in _qitems(page):
        if m in _norm(_question_title(q)):
            return q
    return None


def _alerts(page) -> List[str]:
    out = []
    for a in page.query_selector_all('[role=alert]'):
        try:
            t = " ".join(a.inner_text().split())
        except Exception:
            continue
        if t and t not in out:
            out.append(t)
    return out


def _is_checked(choice_el) -> Optional[bool]:
    """True/False — состояние варианта. None — форма его не сообщает."""
    known = False
    for el in (choice_el, choice_el.query_selector('[role=radio],[role=checkbox]')):
        if el is None:
            continue
        v = el.get_attribute("aria-checked")
        if v is not None:
            known = True
            if v == "true":
                return True
    inp = choice_el.query_selector('input[type=radio],input[type=checkbox]')
    if inp is not None:
        try:
            return bool(inp.is_checked())
        except Exception:
            pass
    return False if known else None


# ---------- основной движок ----------

class SlamFiller:
    """Заполняет форму. Как контекстный менеджер держит один браузер на всю
    бригаду — это экономит ~2-4 с на каждого работника."""

    def __init__(self, headless: bool = True, slow_mo: int = 0,
                 budget: float = DEFAULT_BUDGET):
        self.headless = headless
        self.slow_mo = slow_mo
        self.budget = budget
        self._pw = None
        self._browser = None

    # --- жизненный цикл браузера ---

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def start(self):
        if self._pw is None:
            self._pw = sync_playwright().start()
        if self._browser is None or not self._browser.is_connected():
            self._browser = self._pw.chromium.launch(
                headless=self.headless, slow_mo=self.slow_mo,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )

    def stop(self):
        try:
            if self._browser is not None and self._browser.is_connected():
                self._browser.close()
        except Exception:
            pass
        finally:
            self._browser = None
            try:
                if self._pw is not None:
                    self._pw.stop()
            except Exception:
                pass
            self._pw = None

    # --- одна форма на одного работника ---

    def fill_one(self, data: WorkerData, submit: bool = False,
                 screenshot_prefix: Optional[str] = None) -> FillResult:
        res = FillResult(ok=True)
        started = time.monotonic()
        deadline = started + self.budget
        standalone = self._pw is None        # вызвали без with — гасим за собой
        context = page = None
        try:
            self.start()
            context = self._browser.new_context(
                viewport={"width": 1280, "height": 1100},
                locale="ru-RU",
            )
            context.set_default_timeout(30000)
            page = context.new_page()
            self._run(page, data, submit, res, deadline, screenshot_prefix)
        except Exception as e:
            res.fail(f"Исключение: {type(e).__name__}: {e}")
            if screenshot_prefix and page is not None:
                try:
                    page.screenshot(path=f"{screenshot_prefix}_error.png", full_page=True)
                except Exception:
                    pass
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            if standalone:
                self.stop()
            res.elapsed = round(time.monotonic() - started, 1)
        return res

    def _run(self, page, data: WorkerData, submit: bool, res: FillResult,
             deadline: float, screenshot_prefix: Optional[str]):
        self._open(page, res)
        if not res.ok:
            return

        self._page1(page, data, res)
        if not res.ok:
            return
        if not self._advance(page, res, A.SECTIONS["personal"]):
            return

        # Страницы 2..N: да/нет, последняя — тексты опасностей.
        for _ in range(MAX_PAGES):
            if time.monotonic() > deadline:
                res.fail("Превышено время на одного работника — прогон прерван.")
                return

            section = _section(page)
            has_submit = bool(page.query_selector(SEL_SUBMIT))
            has_next = bool(page.query_selector(SEL_NEXT))
            texts = page.query_selector_all(SEL_TEXT)

            if texts and (has_submit or not has_next):
                self._control_page(page, section, res)
            else:
                self._yesno_page(page, section, res)
            if not res.ok:
                return

            if screenshot_prefix:
                try:
                    page.screenshot(path=f"{screenshot_prefix}_sec_{_norm(section)[:12]}.png",
                                    full_page=True)
                except Exception:
                    pass

            if page.query_selector(SEL_SUBMIT) and not page.query_selector(SEL_NEXT):
                break
            if not page.query_selector(SEL_NEXT):
                res.fail(f"Раздел «{section[:60]}»: нет ни кнопки «Далее», ни «Отправить».")
                return
            if not self._advance(page, res, section):
                return
        else:
            res.fail(f"Форма не дошла до страницы отправки за {MAX_PAGES} переходов.")
            return

        self._verify_before_submit(page, res)
        self._verify_against_plan(data, res)
        if not res.ok:
            return

        if submit:
            self._submit(page, res, screenshot_prefix)
        else:
            res.log("Все поля заполнены и сверены со сценарием. "
                    "Кнопка «Отправить» НЕ нажималась (submit=False).")

    # ----- этапы -----

    def _open(self, page, res):
        page.goto(FORM_URL, wait_until="domcontentloaded", timeout=60000)

        def start_button():
            for name in ("Начать", "Start now", "Начать сейчас", "Бастау"):
                try:
                    btn = page.get_by_role("button", name=name)
                    if btn.count():
                        return btn.last
                except Exception:
                    continue
            return None

        # Ждём того, что появится раньше: поля формы или кнопка стартового
        # экрана. Ждать поля «на всякий случай» нельзя — у этой формы стартовый
        # экран есть всегда, и фиксированное ожидание просто тратит время.
        ready = _poll(lambda: page.query_selector(SEL_TEXT) or start_button(), timeout=30.0)
        if ready is None:
            res.fail("Форма не загрузилась: за 30 с не появились ни поля ввода, "
                     "ни кнопка «Начать».")
            return
        if page.query_selector(SEL_TEXT) is None:
            try:
                ready.click(timeout=10000)
                page.wait_for_selector(SEL_TEXT, timeout=45000)
            except PlaywrightTimeoutError:
                res.fail("После кнопки «Начать» поля формы не появились за 45 с.")
                return
        _settle(page)
        res.log("Форма открыта, стартовый экран пройден.")

    def _page1(self, page, data: WorkerData, res):
        section = _section(page) or A.SECTIONS["personal"]

        task_text = A.WORK_TASK.get(data.task)
        if not task_text:
            res.fail(f"Неизвестный тип задания: {data.task}")
            return
        object_text = A.OBJECT_OPTIONS.get(data.object_key)
        if not object_text:
            res.fail(f"Неизвестный объект: {data.object_key}")
            return
        company = (data.company or "").strip()
        if not company:
            res.fail("Не указана подрядная организация.")
            return

        # Q1-Q3: текстовые поля. Ищем по заголовку, при неудаче — по порядку.
        texts = page.query_selector_all(SEL_TEXT)
        if len(texts) < 3:
            res.fail(f"Стр.1: ожидалось 3 текстовых поля, найдено {len(texts)}.")
            return
        for pos, (key, value) in enumerate([("fio", data.full_name),
                                            ("workplace", data.workplace),
                                            ("task", task_text)]):
            q = _find_question(page, A.MARKERS_PERSONAL[key])
            el = q.query_selector(SEL_TEXT) if q else None
            title = _question_title(q) if q else ""
            if el is None:
                el, title = texts[pos], A.Q_PERSONAL[key]
                res.warn(f"Стр.1: вопрос «{A.Q_PERSONAL[key]}» не найден по заголовку, "
                         f"заполнено {pos + 1}-е поле по порядку.")
            self._fill_text(el, value, section, title or A.Q_PERSONAL[key], key, res)
        if not res.ok:
            return
        res.log(f"Стр.1: ФИО «{data.full_name}», место «{data.workplace}», "
                f"задание «{task_text}».")

        # Q4: департамент -> Подрядная организация (после этого появляется Q5).
        if not self._pick(page, "dept", A.DEPARTMENT_CONTRACTOR, section, res):
            return
        _settle(page)

        # Q5: объект работ (после этого появляется Q6).
        if not self._pick(page, "object", object_text, section, res):
            return
        _settle(page)

        # Q6: наименование подрядной организации -> «Другое» + текст.
        self._company(page, company, section, res)

    def _company(self, page, company: str, section: str, res):
        q6 = _poll(lambda: _find_question(page, A.MARKERS_PERSONAL["company"]), timeout=8.0)
        if not q6:
            res.fail("Стр.1: не появился вопрос «Наименование подрядной организации» (Q6).")
            return
        title = _question_title(q6) or A.Q_PERSONAL["company"]

        # ВАЖНО: «Другое» в MS Forms — это НЕ choiceItem. Это отдельный radio,
        # лежащий в одном <label> со свободным полем ввода (проверено на живой
        # форме: choiceItem'ов 29, radio 30). Брать «последний вариант списка»
        # нельзя — там реальная компания (Eurohydroservice).
        other = q6.query_selector(f'label:has({SEL_TEXT}) [role=radio]')
        if other is None:                       # запасной путь — по aria-label
            for r in q6.query_selector_all('[role=radio]'):
                label = _norm(r.get_attribute("aria-label") or "")
                if any(m in label for m in ("other", "друг", "басқа")):
                    other = r
                    break
        if other is None:
            res.fail("Стр.1: в вопросе «Наименование подрядной организации» "
                     "не найден вариант «Другое».")
            return
        ti = q6.query_selector(SEL_TEXT)
        if ti is None:
            res.fail("Стр.1: не найдено свободное поле для названия организации.")
            return

        other.click()
        if _is_checked(other) is False:
            other.click()
        if not self._fill_text(ti, company, section, title, "company", res,
                               kind="choice+other"):
            return

        checked = _is_checked(other)
        if checked is False:
            res.fail("Стр.1: вариант «Другое» не отметился — название организации "
                     "не будет учтено формой.")
        elif checked is None:
            res.warn("Стр.1: форма не сообщила состояние варианта «Другое»; "
                     "проверено переходом «Далее».")

    def _yesno_page(self, page, section: str, res):
        is_hazard = any(m in _norm(section) for m in A.HAZARD_SECTION_MARKERS)
        answer = A.NO if is_hazard else A.YES
        questions = _choice_questions(page)
        if not questions:
            res.fail(f"Раздел «{section[:60]}»: не найдено ни одного вопроса да/нет.")
            return
        for q in questions:
            self._pick_in(q, answer, section, _question_title(q),
                          "no" if is_hazard else "yes", res)
        res.log(f"Раздел «{section[:40]}»: {len(questions)} вопрос(ов) -> «{answer}».")

    def _control_page(self, page, section: str, res):
        section = section or A.SECTIONS["control"]
        texts = page.query_selector_all(SEL_TEXT)
        if len(texts) < len(A.CONTROL_FIELDS):
            res.fail(f"Стр. опасностей: ожидалось {len(A.CONTROL_FIELDS)} полей, "
                     f"найдено {len(texts)}.")
            return
        for pos, (key, question, marker) in enumerate(A.CONTROL_FIELDS):
            q = _find_question(page, marker)
            el = q.query_selector(SEL_TEXT) if q else None
            title = _question_title(q) if q else ""
            if el is None:
                el, title = texts[pos], question
                res.warn(f"Стр. опасностей: «{question}» не найден по заголовку, "
                         f"заполнено {pos + 1}-е поле по порядку.")
            self._fill_text(el, A.HAZARDS[key], section, title or question, key, res)
        res.log(f"Раздел «{section[:40]}»: заполнено {len(A.CONTROL_FIELDS)} текстовых поля.")

    def _advance(self, page, res, section: str) -> bool:
        """Нажать «Далее» и убедиться, что форма реально перешла дальше."""
        before = _signature(page)
        btn = page.query_selector(SEL_NEXT)
        if btn is None:
            res.fail(f"Раздел «{section[:60]}»: не найдена кнопка «Далее».")
            return False
        btn.click()
        if _poll(lambda: _signature(page) != before, timeout=20.0):
            _settle(page)
            return True

        msgs = _alerts(page)
        res.fail(f"Раздел «{section[:60]}»: форма не перешла на следующую страницу — "
                 f"похоже, остался незаполненный обязательный вопрос."
                 + (" Сообщение формы: " + " | ".join(msgs[:3]) if msgs else ""))
        return False

    def _submit(self, page, res, screenshot_prefix):
        page.click(SEL_SUBMIT)

        def confirmed():
            try:
                body = _norm(page.inner_text("body"))
            except Exception:
                return False
            return any(m in body for m in CONFIRM_MARKERS)

        if _poll(confirmed, timeout=40.0, interval=0.4):
            res.submitted = True
            res.log("✅ Форма отправлена — получено подтверждение от Microsoft Forms.")
        elif page.query_selector(SEL_SUBMIT):
            # Кнопка на месте => отправки не произошло.
            msgs = _alerts(page)
            res.submitted = False
            res.fail("Форма НЕ отправлена: кнопка «Отправить» осталась на экране."
                     + (" Сообщение формы: " + " | ".join(msgs[:3]) if msgs else ""))
        else:
            # Кнопка исчезла, но подтверждение не распознали — честно говорим «не знаем».
            res.submitted = False
            res.unconfirmed = True
            res.warn("Кнопка «Отправить» нажата, страница ушла, но подтверждение "
                     "не распознано. Проверьте ответ вручную — при повторной "
                     "отправке возможен дубликат.")
            res.log("⚠ Отправка не подтверждена — требуется ручная проверка.")
        if screenshot_prefix:
            try:
                page.screenshot(path=f"{screenshot_prefix}_submitted.png", full_page=True)
            except Exception:
                pass

    # ----- запись значений с перечитыванием -----

    def _fill_text(self, el, value: str, section: str, question: str, key: str,
                   res, kind: str = "text") -> bool:
        actual, err = "", ""
        for attempt in range(3):
            try:
                el.fill(value)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                time.sleep(0.2)
                continue
            try:
                actual = el.input_value()
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                actual = ""
            if actual == value:
                res.record(section, question, kind, actual, True, key=key)
                return True
            time.sleep(0.15)

        res.record(section, question, kind, actual, False,
                   note=err or "значение в поле не совпало с ожидаемым", key=key)
        res.fail(f"Поле «{question[:60]}»: записать не удалось "
                 f"(в поле осталось {actual[:40]!r}{'; ' + err if err else ''}).")
        return False

    def _pick(self, page, key: str, wanted: str, section: str, res) -> bool:
        q = _poll(lambda: _find_question(page, A.MARKERS_PERSONAL[key]), timeout=8.0)
        if q is None:
            res.fail(f"Стр.1: не найден вопрос «{A.Q_PERSONAL[key]}».")
            return False
        return self._pick_in(q, wanted, section, _question_title(q) or A.Q_PERSONAL[key],
                             key, res)

    def _pick_in(self, qitem, wanted: str, section: str, question: str, key: str,
                 res) -> bool:
        want = _norm(wanted)
        choices = qitem.query_selector_all(SEL_CHOICE)
        target = None
        # 1) точное совпадение с одной из частей «Да/ Иә/Yes»
        for c in choices:
            if want in _segments(c.inner_text()):
                target = c
                break
        # 2) запасной вариант — подстрока
        if target is None:
            for c in choices:
                if want in _norm(c.inner_text()):
                    target = c
                    break
        if target is None:
            res.record(section, question, "choice", "", False,
                       note=f"вариант «{wanted}» отсутствует в форме", key=key)
            res.fail(f"Вопрос «{question[:60]}»: не найден вариант «{wanted}».")
            return False

        text = " ".join(target.inner_text().split())
        for attempt in range(2):
            target.click()
            state = _poll(lambda: _is_checked(target) is not False, timeout=2.5)
            checked = _is_checked(target)
            if checked is True:
                res.record(section, question, "choice", text, True, key=key)
                return True
            if checked is None:
                # Форма не сообщает состояние. Не выдаём это за проверку —
                # реальной проверкой станет переход «Далее» (форма не пустит
                # дальше при незаполненном обязательном вопросе).
                res.record(section, question, "choice", text, False,
                           note="форма не сообщает состояние выбора; "
                                "проверено переходом «Далее»", key=key)
                return True
            time.sleep(0.2)

        res.record(section, question, "choice", text, False,
                   note="после нажатия вариант остался невыбранным", key=key)
        res.fail(f"Вопрос «{question[:60]}»: вариант «{text[:40]}» не отметился.")
        return False

    # ----- итоговые проверки -----

    def _verify_before_submit(self, page, res):
        """Все обязательные поля последней страницы действительно заполнены."""
        empty = []
        for q in _qitems(page):
            ti = q.query_selector(SEL_TEXT)
            if ti is None or not q.query_selector(SEL_REQUIRED):
                continue
            try:
                if not ti.input_value().strip():
                    empty.append(_question_title(q)[:60])
            except Exception:
                empty.append(_question_title(q)[:60] + " (не удалось прочитать)")
        if empty:
            res.fail("Пустые обязательные поля перед отправкой: " + "; ".join(empty))
        else:
            res.log("Проверка: обязательные поля финальной страницы заполнены.")

    def _verify_against_plan(self, data: WorkerData, res):
        """Сверка протокола со сценарием, который видит пользователь.

        Ловит рассинхрон «показали одно — записали другое»."""
        plan = A.resolve_plan(data.full_name, data.workplace, data.task,
                              data.object_key, data.company)
        written = {}
        for f in res.fields:
            if f["kind"] != "choice" and f["verified"]:
                written[f["key"]] = f["value"]
        touched = {f["key"] for f in res.fields}

        problems = []
        for page_plan in plan:
            for item in page_plan["items"]:
                key = item["key"]
                if item["kind"] == "choice":
                    # Значение выбора сверяет _pick_in (aria-checked) и переход
                    # «Далее»; здесь ловим только полностью пропущенный вопрос.
                    if key not in ("yes", "no") and key not in touched:
                        problems.append(f"{item['q']} — вопрос не заполнялся")
                    continue
                got = written.get(key)
                if got is None:
                    problems.append(f"{item['q']} — не записано")
                elif got != item["value"]:
                    problems.append(f"{item['q']} — записано другое значение")
        if problems:
            res.fail("Расхождение со сценарием: " + "; ".join(problems[:6]))
            return

        yes = sum(1 for f in res.fields if f["key"] == "yes")
        no = sum(1 for f in res.fields if f["key"] == "no")
        if yes != A.EXPECTED_YES or no != A.EXPECTED_NO:
            res.warn(f"Форма изменилась: ожидалось «Да»×{A.EXPECTED_YES} и "
                     f"«Нет»×{A.EXPECTED_NO}, фактически «Да»×{yes} и «Нет»×{no}. "
                     f"Все вопросы отвечены по общему правилу — проверьте сценарий.")
        res.log(f"Сверка со сценарием пройдена: {len(written)} текстовых поля, "
                f"«Да»×{yes}, «Нет»×{no}.")
