#!/usr/bin/env python3
"""GUI для массовых бэктестов gridbacktest.com (см. README.md).

Запуск: gui.bat  (или python gui.py).

Внешний вид и подписи — как на сайте: «Пара и период», «Точка входа»,
«Настройки сетки». У каждого параметра сетки — значение (одно или несколько
через запятую) и рядом «перебор»: от / до / шаг. Число комбинаций считается
сразу. Прогон идёт в отдельном процессе python gbt.py sweep — посчитанные
строки пишутся в CSV сразу, остановка безопасна, повторный запуск продолжает с места.
"""

import csv
import json
import os
import queue
import re
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.dont_write_bytecode = True   # без __pycache__ в корне: там только программа

import gbt  # noqa: E402

CFG_PATH = os.path.join(gbt.CONFIGS_DIR, "sweep_gui.json")   # конфиг, с которым окно запускает прогон
PAIRS_CACHE = os.path.join(gbt.DATA_DIR, "pairs_cache.json")
STOP_FILE = os.path.join(gbt.DATA_DIR, ".gbt_stop")   # кнопка «Стоп»: gbt.py видит файл и дописывает текущие прогоны

# цвета и шрифт сайта (style.css, светлая тема)
BG, PANEL, LINE = "#f6f7f9", "#ffffff", "#e2e6ea"
TEXT, DIM, FIELD = "#16191d", "#5f6874", "#cfd6dd"
PLUS, MINUS, WARN = "#0b7a34", "#c02a1e", "#a35c00"
ACCENT, ACCENT_HOVER, TINT = "#1a5fd0", "#164fae", "#eef3fc"
CHIP, CHIP_HOVER = "#f1f4f8", "#e3e9f2"
FONT = "Segoe UI"

# ключ, подпись (как на сайте), по умолчанию, (мин, макс), целое?, перебор по умолчанию (от, до, шаг)
# границы — запасные: после загрузки справочника берутся лимиты сервера
PARAMS = [
    ("deposit",       "Депозит, $",     1000, (10, 10 ** 6), True,  (500, 2000, 500)),
    ("leverage",      "Плечо",           10,  (1, 50),       True,  (5, 20, 5)),
    ("orders",        "Ордеров",         10,  (1, 50),       True,  (10, 20, 5)),
    ("price_overlap", "Перекрытие, %",   25,  (0.1, 90),     False, (15, 40, 5)),
    ("price_factor",  "Коэф. цены",      1.6, (0.5, 3),      False, (1.4, 1.8, 0.2)),
    ("volume_factor", "Коэф. объёма",    1.2, (0.5, 3),      False, (1.05, 1.45, 0.2)),
    ("profit",        "Тейк, %",         1,   (0.01, 50),    False, (0.5, 1.5, 0.5)),
]
REINVEST = [(0, "Выкл"), (25, "25"), (50, "50"), (100, "100")]
PARAM_NAMES = [p[0] for p in PARAMS] + ["reinvest"]
# стоп-лосс — не параметр сервера: оценивается по сделкам ответа (gbt.stop_eval), 0 — без стопа
STOP_ROW = ("stop_loss", "Стоп-лосс, %", 0, (0, 90), False, (2, 10, 2))
# кнопки «Настроек сетки» сайта (app.js, PRESETS)
PRESETS = {
    "calm": {"price_overlap": 40, "orders": 20, "price_factor": 1.4, "volume_factor": 1.05, "profit": 0.5},
    "mid":  {"price_overlap": 25, "orders": 15, "price_factor": 1.6, "volume_factor": 1.2,  "profit": 1},
    "hot":  {"price_overlap": 15, "orders": 10, "price_factor": 1.8, "volume_factor": 1.4,  "profit": 1.5},
}
TFS = [(15, "15м"), (30, "30м"), (60, "1ч"), (240, "4ч"), (1440, "1д")]
YEARS = [1, 2, 3, 4, 5, 6]
DEFAULT_SYMBOLS = {"BTCUSDT", "ETHUSDT", "NEARUSDT"}
MAX_VALUES = 500          # значений в одном интервале
MAX_PARAM_SETS = 100000   # наборов параметров во всей сетке
LEFT_LIMIT = 300000       # до стольки прогонов фоном считаем, сколько уже есть в CSV

INDICATORS_EXAMPLE = '[\n  {"ind": "rsi", "period": [7, 14], "value": [25, 30], "tf": [15, 60]}\n]'
# параметры индикатора в окне ⚙: числа пишутся в поле (можно несколько через запятую),
# остальное — кнопками (можно несколько)
NUM_PARAMS = {"period": True, "hours": True, "mult": False}     # ключ -> целое?
FORM_KEYS = {"ind", "tf", "op", "value", "right"} | set(gbt.IND_KEYS)
SETS_EXAMPLE = '[\n  {"orders": 15, "price_overlap": 25, "price_factor": 1.6}\n]'


def norm(v):
    """1.0 -> 1: одинаковые числа дают одинаковый ключ прогона (иначе досчёт их повторит)."""
    return int(v) if isinstance(v, float) and v.is_integer() else v


def num(text, integer):
    t = str(text).strip().replace(",", ".")
    if not t:
        raise ValueError("пустое значение")
    try:
        v = float(t)
    except ValueError:
        raise ValueError("«%s» — не число" % t)
    if v != v or v in (float("inf"), float("-inf")):      # float() пропускает nan и inf
        raise ValueError("«%s» — не число" % t)
    if integer:
        if abs(v - round(v)) > 1e-9:
            raise ValueError("нужно целое: %s" % t)
        return int(round(v))
    return norm(v)


def pieces(text):
    return [p for p in re.split(r"[;,\s]+", str(text).strip()) if p]


def make_range(lo, hi, step):
    """Значения от lo до hi включительно с шагом step."""
    if step <= 0:
        raise ValueError("шаг должен быть больше нуля")
    if lo > hi:
        raise ValueError("«от» больше «до»")
    eps = max(abs(lo), abs(hi), 1.0) * 1e-9
    count = int((hi - lo + eps) / step) + 1      # число значений — до построения списка
    if count > MAX_VALUES:
        raise ValueError("%d значений (лимит %d) — увеличьте шаг" % (count, MAX_VALUES))
    return [norm(round(lo + step * i, 10)) for i in range(count)]


def as_range(vals):
    """Список -> (от, до, шаг), если это ровный интервал из 3+ значений, иначе None."""
    if len(vals) < 3 or not all(isinstance(v, (int, float)) for v in vals):
        return None
    vals = sorted(vals)
    step = norm(round(vals[1] - vals[0], 10))
    try:
        if step > 0 and make_range(vals[0], vals[-1], step) == [norm(v) for v in vals]:
            return vals[0], vals[-1], step
    except ValueError:
        pass
    return None


def fmt_vals(vals):
    return ", ".join(("%g" % v) if isinstance(v, float) else str(v) for v in vals)


def parse_json_list(text, what):
    text = text.strip()
    if not text:
        return None
    try:
        val = json.loads(text)
    except ValueError as e:
        raise ValueError("%s — неверный JSON: %s" % (what, e))
    if not isinstance(val, list) or not all(isinstance(x, dict) for x in val):
        raise ValueError("%s: нужен список объектов [{...}, ...]" % what)
    return val


def plural(n, one, few, many):
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def spaced(n):
    return "{:,}".format(n).replace(",", " ")


def base(sym):
    return sym[:-4] if sym.endswith("USDT") else sym


def short_list(names, total_label, total):
    if not names:
        return "не выбрано"
    if len(names) == total and total > 1:
        return "%s (%d)" % (total_label, total)
    return ", ".join(names[:4]) + (" +%d" % (len(names) - 4) if len(names) > 4 else "")


def one(vals):
    """[x] -> x: в конфиге одно значение пишется без списка."""
    vals = list(dict.fromkeys(vals))
    return vals[0] if len(vals) == 1 else vals


# ---------- элементы в стиле сайта ----------

class Toggle(tk.Label):
    """Кнопка-переключатель, как .presets / .tfs на сайте."""

    def __init__(self, parent, text, on=False, command=None, padx=10):
        super().__init__(parent, text=text, font=(FONT, 9, "bold"), padx=padx, pady=5,
                         cursor="hand2", bd=0)
        self.on, self.command, self.hover = on, command, False
        self.bind("<Button-1>", lambda _e: self.command and self.command(self))
        self.bind("<Enter>", lambda _e: self._hover(True))
        self.bind("<Leave>", lambda _e: self._hover(False))
        self.paint()

    def _hover(self, h):
        self.hover = h
        self.paint()

    def set(self, on):
        self.on = bool(on)
        self.paint()

    def paint(self):
        if self.on:
            self.config(bg=ACCENT, fg="white")
        elif self.hover:
            self.config(bg=CHIP_HOVER, fg=ACCENT)
        else:
            self.config(bg=CHIP, fg=DIM)


class ToggleGroup:
    """Ряд переключателей. multi — можно несколько; need_one — нельзя снять последний."""

    def __init__(self, parent, items, on=(), multi=True, need_one=True, command=None,
                 stretch=False, bg=PANEL):
        self.frame = tk.Frame(parent, bg=bg)
        self.multi, self.need_one, self.command, self.stretch = multi, need_one, command, stretch
        self.btns = {}
        for value, text in items:
            self.add(value, text, value in on)

    def add(self, value, text, on=False):
        t = Toggle(self.frame, text, on=on, command=lambda _t, v=value: self._click(v))
        t.pack(side="left", padx=(0 if not self.btns else 4, 0),
               fill="x" if self.stretch else None, expand=self.stretch)
        self.btns[value] = t
        return t

    def _click(self, v):
        t = self.btns[v]
        if self.multi:
            if t.on and self.need_one and len(self.get()) == 1:
                return
            t.set(not t.on)
        else:
            if t.on and self.need_one:
                return
            on = not t.on
            for k, b in self.btns.items():
                b.set(on and k == v)
        if self.command:
            self.command(v)

    def get(self):
        return [v for v, b in self.btns.items() if b.on]

    def set(self, values):
        for v, b in self.btns.items():
            b.set(v in values)


def ghost(parent, text, command, bg=PANEL):
    """Кнопка с рамкой, как button.ghost на сайте. Упаковывать .box."""
    box = tk.Frame(parent, bg=FIELD)
    b = tk.Button(box, text=text, command=command, bg=bg, fg=ACCENT, activebackground=TINT,
                  activeforeground=ACCENT, disabledforeground="#a3abb5", relief="flat", bd=0,
                  font=(FONT, 9, "bold"), padx=10, pady=4, cursor="hand2")
    b.pack(padx=1, pady=1, fill="both", expand=True)
    b.box = box
    return b


def primary(parent, text, command):
    return tk.Button(parent, text=text, command=command, bg=ACCENT, fg="white",
                     activebackground=ACCENT_HOVER, activeforeground="white",
                     disabledforeground="#9fb8e6", relief="flat", bd=0,
                     font=(FONT, 11, "bold"), pady=9, cursor="hand2")


def field_label(parent, text, bg=PANEL):
    return tk.Label(parent, text=text, bg=bg, fg=DIM, font=(FONT, 8, "bold"))


def hint(parent, text="", bg=PANEL, fg=DIM, wrap=330):
    return tk.Label(parent, text=text, bg=bg, fg=fg, font=(FONT, 8), justify="left",
                    anchor="w", wraplength=wrap)


def panel(parent):
    return tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=LINE,
                    highlightcolor=LINE, padx=16, pady=14)


def h2(parent, text, second=False):
    if second:
        tk.Frame(parent, bg=LINE, height=1).pack(fill="x", pady=(14, 12))
    row = tk.Frame(parent, bg=PANEL)
    row.pack(fill="x", pady=(0, 8))
    tk.Frame(row, bg=ACCENT, width=3).pack(side="left", fill="y")
    tk.Label(row, text=text, bg=PANEL, fg=TEXT, font=(FONT, 11, "bold")).pack(side="left", padx=(8, 0))
    return row


def pick_button(parent, command):
    """Поле-кнопка, как «Пара» на сайте: по нажатию — окно выбора с поиском."""
    box = tk.Frame(parent, bg=FIELD)
    b = tk.Button(box, text="", command=command, bg=PANEL, fg=TEXT, activebackground=TINT,
                  activeforeground=TEXT, relief="flat", bd=0, anchor="w", font=(FONT, 10),
                  padx=8, pady=4, cursor="hand2")
    b.pack(padx=1, pady=1, fill="both", expand=True)
    b.box = box
    return b


class Picker(tk.Toplevel):
    """Окно выбора, как «Пара» на сайте: поиск сверху и список; можно отметить несколько."""

    def __init__(self, root, title, items, selected, on_done, all_text=None, all_on=False,
                 find_hint="Поиск"):
        super().__init__(root, bg=PANEL, padx=16, pady=14)
        self.title(title)
        self.transient(root)
        self.resizable(False, True)
        self.items = items                    # [(ключ, подпись, текст для поиска)]
        self.sel = set(selected)
        self.on_done = on_done

        h2(self, title)
        field_label(self, find_hint).pack(anchor="w")
        self.find = tk.StringVar()
        e = ttk.Entry(self, textvariable=self.find, width=36)
        e.pack(fill="x", pady=(2, 8))
        self.find.trace_add("write", lambda *_: self._render())

        self.all_var = tk.BooleanVar(value=all_on)
        if all_text:
            ttk.Checkbutton(self, text=all_text, variable=self.all_var,
                            command=self._render).pack(anchor="w", pady=(0, 6))

        box = tk.Frame(self, bg=FIELD)
        box.pack(fill="both", expand=True)
        self.lb = tk.Listbox(box, height=16, width=40, bd=0, highlightthickness=0, font=(FONT, 10),
                             activestyle="none", selectmode="single", bg=PANEL, fg=TEXT,
                             selectbackground=PANEL, selectforeground=TEXT)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.lb.yview)
        self.lb.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 1), pady=1)
        self.lb.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=1)
        self.lb.bind("<Button-1>", self._click)

        row = tk.Frame(self, bg=PANEL)
        row.pack(fill="x", pady=(8, 0))
        ghost(row, "Отметить все", lambda: self._mark(True)).box.pack(side="left")
        ghost(row, "Снять", lambda: self._mark(False)).box.pack(side="left", padx=6)
        self.count = hint(row)
        self.count.pack(side="right")
        primary(self, "Готово", self._done).pack(fill="x", pady=(10, 0))

        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Return>", lambda _e: self._done())
        self._render()
        self.update_idletasks()
        x = root.winfo_rootx() + max(0, (root.winfo_width() - self.winfo_width()) // 3)
        y = root.winfo_rooty() + 60
        self.geometry("+%d+%d" % (x, y))
        e.focus_set()
        try:
            self.wait_visibility()
            self.grab_set()           # модально, как окно «Пара» на сайте
        except tk.TclError:
            pass

    def _render(self):
        q = self.find.get().strip().lower()
        self.visible = [it for it in self.items if not q or q in it[2]]
        top = self.lb.yview()[0]
        everything = self.all_var.get()
        self.lb.delete(0, "end")
        for i, (key, text, _) in enumerate(self.visible):
            on = everything or key in self.sel
            self.lb.insert("end", ("  ✓  " if on else "       ") + text)
            self.lb.itemconfig(i, background=TINT if on else PANEL,
                               foreground=DIM if everything else (ACCENT if on else TEXT),
                               selectbackground=TINT if on else PANEL,
                               selectforeground=ACCENT if on else TEXT)
        self.lb.yview_moveto(top)
        self.count.config(text="выбраны все" if everything else
                          "выбрано: %d из %d" % (len(self.sel), len(self.items)))

    def _click(self, e):
        if self.all_var.get() or not self.visible:
            return "break"
        i = self.lb.nearest(e.y)
        box = self.lb.bbox(i)
        if box and box[1] <= e.y <= box[1] + box[3]:
            key = self.visible[i][0]
            self.sel.symmetric_difference_update({key})
            self._render()
        return "break"

    def _mark(self, on):
        keys = {it[0] for it in self.visible}
        self.sel = (self.sel | keys) if on else (self.sel - keys)
        self._render()

    def _done(self):
        self.on_done(self.sel, self.all_var.get())
        self.destroy()


class Results(tk.Toplevel):
    """Таблица результатов из CSV: сортировка по клику на заголовок, ликвидации по флажку."""

    COLS = [("symbol", "Пара", 84), ("position", "Сторона", 58), ("price_overlap", "Перекр., %", 74),
            ("orders", "Ордеров", 60), ("price_factor", "Коэф. цены", 78),
            ("volume_factor", "Коэф. объёма", 90), ("profit", "Тейк, %", 56), ("leverage", "Плечо", 48),
            ("reinvest", "Реинв., %", 64), ("stop_loss", "Стоп, %", 56), ("stop_hits", "Стопов", 62),
            ("_net", "Итог, %", 76), ("net_stop_worst_pct", "Хуже, %", 70), ("net_pct", "Без стопа, %", 88),
            ("max_drawdown", "Просадка, %", 82), ("entries", "Сделок", 56),
            ("entry", "Вход", 160), ("liq_time", "Ликвидация", 180)]
    TEXT_COLS = {"symbol", "position", "entry", "liq_time", "stop_hits"}

    def __init__(self, root, path):
        super().__init__(root, bg=PANEL, padx=16, pady=14)
        self.path = path
        self.title("Результаты — %s" % os.path.basename(path))
        self.geometry("%dx620+%d+%d" % (min(1480, root.winfo_screenwidth() - 40), root.winfo_rootx() + 20,
                                        root.winfo_rooty() + 60))
        self.sort_by, self.desc = "_net", True

        head = h2(self, "Результаты · %s" % os.path.basename(path))
        ghost(head, "Обновить", self.reload).box.pack(side="right")
        ghost(head, "Открыть CSV", lambda: App._open(path)).box.pack(side="right", padx=6)
        self.summary = hint(self, wrap=1200, fg=TEXT)
        self.summary.pack(fill="x")
        self.with_liq = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, text="Показывать ликвидированные и неполные", variable=self.with_liq,
                        command=self._fill).pack(anchor="w", pady=(6, 6))

        box = tk.Frame(self, bg=FIELD)
        box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(box, columns=[c for c, _, _ in self.COLS], show="headings")
        for key, title, width in self.COLS:
            self.tree.heading(key, text=title, command=lambda k=key: self._sort(k))
            self.tree.column(key, width=width, anchor="w" if key in self.TEXT_COLS else "center",
                             stretch=key == "entry")
        self.tree.tag_configure("plus", foreground=PLUS)
        self.tree.tag_configure("minus", foreground=MINUS)
        self.tree.tag_configure("liq", foreground=MINUS, background="#fdf0ee")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(box, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=sb.set, xscrollcommand=hs.set)
        hs.pack(side="bottom", fill="x", padx=1, pady=(0, 1))
        sb.pack(side="right", fill="y", padx=(0, 1), pady=1)
        self.tree.pack(fill="both", expand=True, padx=(1, 0), pady=1)
        hint(self, "Клик по заголовку — сортировка. «Итог» со стопом — оценка по сделкам прогона: "
                   "сервер стоп не считает, а сделок, которые бот открыл бы после стопа, в прогоне нет. "
                   "«Стопов»: точно + возможно (?) — до стопа дошло только внутри свечи закрытия; "
                   "«Хуже» — итог, если и возможные стопы сработали. "
                   "Строки с ошибкой сервера в таблицу не попадают.", wrap=1250).pack(fill="x", pady=(6, 0))
        self.reload()

    def reload(self):
        self.rows, self.errors = [], 0
        try:
            with open(self.path, encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("error") or r.get("net_pct") in (None, ""):
                        self.errors += 1
                        continue
                    r["entry"] = gbt.entry_name(r)
                    r["_net"], r["_liq"] = gbt.effective(r)
                    if r.get("stop_wiped") not in (None, "", "False"):
                        r["liq_time"] = "депозит кончился на стопах %s" % str(r["stop_wiped"])[:10]
                    elif r.get("liquidated") != "True":
                        r["liq_time"] = ""
                    elif gbt.partial(r):
                        r["liq_time"] = "неполный: стоп до ликвидации %s" % str(
                            gbt.fmt_time(r.get("liq_time")))[:10]
                    else:
                        r["liq_time"] = gbt.fmt_time(r.get("liq_time"))
                    if r.get("stop_loss"):
                        r["stop_hits"] = "%s%s" % (r.get("stop_hits") or 0, " +%s?" % r["stop_maybe"]
                                                   if r.get("stop_maybe") not in (None, "", "0") else "")
                    else:
                        r["stop_loss"] = r["stop_hits"] = "—"
                    self.rows.append(r)
        except (OSError, csv.Error, KeyError) as e:
            self.summary.config(text="Не прочитал %s: %s" % (self.path, e), fg=MINUS)
            return
        alive = [r for r in self.rows if not r["_liq"]]
        if self.rows and not alive:
            self.with_liq.set(True)       # иначе таблица была бы пустой
        plus = sum(1 for r in alive if r["_net"] > 0)
        part = sum(1 for r in self.rows if gbt.partial(r))
        self.summary.config(text="Вариантов: %d · без ликвидации: %d · в плюсе без ликвидации: %d · "
                                 "с ликвидацией: %d · неполных: %d · с ошибкой: %d"
                                 % (len(self.rows), len(alive), plus,
                                    len(self.rows) - len(alive) - part, part, self.errors), fg=TEXT)
        if part:
            self.summary.config(text=self.summary["text"] + "\nНеполные — стоп сработал бы раньше "
                                "ликвидации, но после ликвидации сервер не считал: итог только до её "
                                "даты. Чтобы увидеть весь период — уменьшите плечо или увеличьте "
                                "перекрытие, чтобы прогон обходился без ликвидации.")
        self._fill()

    def _sort(self, key):
        self.desc = not self.desc if key == self.sort_by else key not in self.TEXT_COLS and key != "max_drawdown"
        self.sort_by = key
        self._fill()

    def _fill(self):
        key = self.sort_by

        def k(r):
            if key in self.TEXT_COLS:
                return (0, str(r.get(key) or ""))
            try:
                return (0, float(r.get(key)))
            except (TypeError, ValueError):
                return (1, 0)
        rows = [r for r in self.rows if self.with_liq.get() or not r["_liq"]]
        rows.sort(key=k, reverse=self.desc)
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            net = r["_net"]
            tag = "liq" if r["_liq"] else ("plus" if net > 0 else "minus")
            vals = []
            for c, _, _ in self.COLS:
                v = r.get(c, "")
                if c == "_net":
                    v = "%+.2f" % net
                elif c in ("net_pct", "net_stop_worst_pct") and v not in (None, ""):
                    v = "%+.2f" % float(v)
                elif c == "net_stop_worst_pct":
                    v = "—"
                vals.append(v)
            self.tree.insert("", "end", values=vals, tags=(tag,))
        for c, title, _ in self.COLS:
            arrow = (" ▼" if self.desc else " ▲") if c == key else ""
            self.tree.heading(c, text=title + arrow)


# ---------- «Свои условия» ----------
# Условие в окне — словарь: ind, tf [мин], op, value (текст: числа через запятую),
# p {параметр: текст | [варианты]}, shift (текст), keep — параметры, заданные явно (из конфига
# или нормы сайта: в запрос идут, даже если равны умолчанию), right — None или {ind, p, shift, keep}.

def ind_info(data, ind):
    return next((c for c in data["catalog"] if c["id"] == ind), None)


def param_caption(meta, k):
    return {"period": "Период, свечей", "hours": "За сколько часов",
            "mult": "Ширина полос, σ" if meta["id"] == "bb" else "Множитель",
            "method": "Тип средней", "price": "Считать по цене",
            "line": meta.get("line_caption", "Линия")}.get(k, k)


def param_defaults(meta, given=None):
    """Параметры индикатора для окна; given — значения из конфига или нормы сайта."""
    p = {}
    for k in meta.get("params", []):
        vals = gbt.as_list((given or {}).get(k, meta.get("defaults", {}).get(k)))
        p[k] = fmt_vals(vals) if k in NUM_PARAMS else list(vals)
    return p


def norm_cond(data, ind, tf=(60,), shift="0"):
    """Новое условие по норме сайта для лонга: RSI ниже 30, цена ниже нижней полосы…"""
    n = data["norms"].get(ind, {}).get("long", {})
    c = {"ind": ind, "tf": list(tf), "shift": shift, "p": param_defaults(ind_info(data, ind)), "keep": [],
         "op": n.get("op", "gt"), "value": fmt_vals([n["value"]]) if "value" in n else "", "right": None}
    if "right" in n:
        c["right"] = norm_side(data, n["right"])
    elif not c["value"]:
        c["value"] = "0"
    return c


def norm_side(data, r):
    """Правая часть по норме сайта ({"ind": "bb", "period": 20, …}) -> индикатор окна."""
    return {"ind": r["ind"], "shift": "0", "p": param_defaults(ind_info(data, r["ind"]), r),
            "keep": [k for k in r if k in gbt.IND_KEYS]}


def side_spec(data, ind, p, shift, keep, where):
    """Индикатор условия (левый или правый) -> {"ind": ..., параметры} для gbt."""
    meta = ind_info(data, ind)
    out = {"ind": ind}
    for k in meta.get("params", []):
        cap = param_caption(meta, k)
        if k in NUM_PARAMS:
            try:
                vals = [num(t, NUM_PARAMS[k]) for t in pieces(p.get(k, ""))]
            except ValueError as e:
                raise ValueError("%s: %s — %s" % (where, cap, e))
        else:
            vals = list(p.get(k) or [])
        if not vals:
            raise ValueError("%s: %s — пусто" % (where, cap))
        # значение по умолчанию сервер подставит сам, как у готовых связок; без него запрос
        # тот же, что из конфига, и досчёт узнаёт уже посчитанное
        if k in keep or one(vals) != meta.get("defaults", {}).get(k):
            out[k] = one(vals)
    try:
        shifts = [num(t, True) for t in pieces(shift)]
    except ValueError as e:
        raise ValueError("%s: сдвиг — %s" % (where, e))
    if shifts and (shifts != [0] or "shift" in keep):
        out["shift"] = one(shifts)
    return out


def cond_spec(data, c, where):
    """Условие окна -> условие gbt (indicators)."""
    s = side_spec(data, c["ind"], c["p"], c["shift"], c["keep"], where)
    if not c["tf"]:
        raise ValueError("%s: выберите размер свечей" % where)
    s["tf"] = one(c["tf"])
    s["op"] = c["op"]
    if c["right"]:
        r = c["right"]
        s["right"] = side_spec(data, r["ind"], r["p"], r["shift"], r["keep"], where + ", справа")
    else:
        try:
            vals = [num(t, False) for t in pieces(c["value"])]
        except ValueError as e:
            raise ValueError("%s: значение — %s" % (where, e))
        if not vals:
            raise ValueError("%s: впишите значение" % where)
        s["value"] = one(vals)
    return s


def spec_side(meta, spec):
    """Параметры индикатора из конфига -> (p, shift, keep) окна; None — параметр не этого индикатора."""
    keys = [k for k in gbt.IND_KEYS if k in spec and k != "shift"]
    if any(k not in meta.get("params", []) for k in keys):
        return None
    return (param_defaults(meta, {k: spec[k] for k in keys}), fmt_vals(gbt.as_list(spec.get("shift", 0))),
            [k for k in gbt.IND_KEYS if k in spec])


def spec_cond(data, spec, tfs):
    """Условие из конфига -> условие окна; None — в окне его не показать (short_value и т.п.)."""
    if not isinstance(spec, dict) or "ind" not in spec or set(spec) - FORM_KEYS:
        return None
    spec = gbt.plain_leaf(spec)
    meta = ind_info(data, spec["ind"])
    tf = gbt.as_list(spec.get("tf", tfs))
    if not meta or not tf or any(t not in dict(TFS) for t in tf):
        return None
    side = spec_side(meta, spec)
    op = spec.get("op")
    if side is None or (op is not None and (not isinstance(op, str) or op not in data["ops"])):
        return None
    c = norm_cond(data, spec["ind"], tf)
    c["p"], c["shift"], c["keep"] = side
    if op:
        c["op"] = op
    if "right" in spec:
        r = spec["right"]
        rmeta = isinstance(r, dict) and ind_info(data, r.get("ind"))
        rside = rmeta and not set(r) - {"ind"} - set(gbt.IND_KEYS) and spec_side(rmeta, r)
        if not rside:
            return None
        c["right"] = dict(zip(("p", "shift", "keep"), rside), ind=r["ind"])
        c["value"] = ""
    elif "value" in spec:
        c["right"] = None
        c["value"] = fmt_vals(gbt.as_list(spec["value"]))
    return c


def rule_groups(data, rule, tfs):
    """Правило из конфига -> группы условий окна; None — правило окну не по силам."""
    if not isinstance(rule, dict):
        return None
    if "ind" in rule:
        raw = [[rule]]
    elif set(rule) == {"all"}:
        raw = [rule["all"]]
    elif set(rule) == {"any"} and isinstance(rule["any"], list):
        raw = []
        for x in rule["any"]:
            if isinstance(x, dict) and "ind" in x:
                raw.append([x])
            elif isinstance(x, dict) and set(x) == {"all"}:
                raw.append(x["all"])
            else:
                return None
    else:
        return None
    lim = data["entry_limits"]
    if not raw or len(raw) > lim["groups"]:
        return None
    groups = []
    for g in raw:
        if not isinstance(g, list) or not g or len(g) > lim["filters"]:
            return None
        conds = [spec_cond(data, s, tfs) for s in g]
        if any(c is None for c in conds):
            return None
        groups.append(conds)
    return groups


def groups_rule(data, groups):
    """Группы условий окна -> одно правило gbt: условие, {"all": [...]} или {"any": [...]}."""
    parts, n = [], 0
    for conds in groups:
        specs = []
        for c in conds:
            n += 1
            specs.append(cond_spec(data, c, "Условие %d (%s)" % (n, ind_info(data, c["ind"])["name"])))
        parts.append(specs[0] if len(specs) == 1 else {"all": specs})
    return parts[0] if len(parts) == 1 else {"any": parts}


def mini(parent, text, command, fg=ACCENT):
    """Маленькая кнопка-значок (⚙, ×), как .mini на сайте."""
    return tk.Button(parent, text=text, command=command, bg=CHIP, fg=fg, activebackground=CHIP_HOVER,
                     activeforeground=fg, relief="flat", bd=0, font=(FONT, 9, "bold"), width=2,
                     cursor="hand2")


class ScrollBox(tk.Frame):
    """Рамка растёт по содержимому до max_h, дальше — прокрутка колесом и полосой."""

    def __init__(self, parent, max_h):
        super().__init__(parent, bg=PANEL)
        self.max_h = max_h
        self.canvas = tk.Canvas(self, bg=PANEL, highlightthickness=0, bd=0, width=1, height=1)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=PANEL)
        self.canvas.create_window(0, 0, window=self.inner, anchor="nw", tags="inner")
        self.inner.bind("<Configure>", self._fit)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig("inner", width=e.width))
        self.bind_all("<MouseWheel>", self._wheel_any, add="+")

    def _fit(self, _e=None):
        w, h = self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()
        self.canvas.config(width=w, height=min(h, self.max_h), scrollregion=(0, 0, w, h))
        if h > self.max_h:
            self.sb.pack(side="right", fill="y")
        else:
            self.sb.pack_forget()
            self.canvas.yview_moveto(0)

    def wheel(self, e):
        if self.inner.winfo_reqheight() > self.max_h:
            self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        return "break"

    def _wheel_any(self, e):
        # путь окна под мышью строкой: winfo_containing падает на внутренних окнах Tk
        # (выпадающий список Combobox — «popdown»), которых tkinter не знает
        path = str(self.tk.call("winfo", "containing", e.x_root, e.y_root))
        me = str(self)
        # над открытым списком колесо листает сам список, а не условия под ним
        if (path == me or path.startswith(me + ".")) and ".popdown" not in path:
            return self.wheel(e)


class ParamFields:
    """Параметры индикатора в окне ⚙: числа — полем, остальное — кнопками, и сдвиг."""

    def __init__(self, parent, data, ind, p, shift):
        meta = ind_info(data, ind)
        self.frame = tk.Frame(parent, bg=PANEL)
        self.getters = {}
        r = 0
        for k in meta.get("params", []):
            tk.Label(self.frame, text=param_caption(meta, k), bg=PANEL, fg=TEXT, font=(FONT, 9)).grid(
                row=r, column=0, sticky="w", padx=(0, 10), pady=3)
            if k in NUM_PARAMS:
                var = tk.StringVar(value=p.get(k, ""))
                ttk.Entry(self.frame, textvariable=var, width=16, font=(FONT, 10)).grid(
                    row=r, column=1, sticky="w")
                self.getters[k] = var.get
            else:
                items = {"method": data["methods"], "price": data["prices"]}.get(k) or meta.get("lines", {})
                tg = ToggleGroup(self.frame, [(v, t.split(" (")[0]) for v, t in items.items()],
                                 on=p.get(k) or ())
                tg.frame.grid(row=r, column=1, sticky="w")
                self.getters[k] = tg.get
            r += 1
        tk.Label(self.frame, text="Смотреть на N свечей назад", bg=PANEL, fg=TEXT, font=(FONT, 9)).grid(
            row=r, column=0, sticky="w", padx=(0, 10), pady=3)
        self.shift = tk.StringVar(value=shift)
        ttk.Entry(self.frame, textvariable=self.shift, width=16, font=(FONT, 10)).grid(
            row=r, column=1, sticky="w")

    def get(self):
        return {k: f() for k, f in self.getters.items()}, self.shift.get()


class CondDialog(tk.Toplevel):
    """⚙ у условия, как на сайте: свечи, параметры индикатора, сдвиг и с чем сравнивать."""

    def __init__(self, root, data, cond, on_done):
        super().__init__(root, bg=PANEL, padx=16, pady=14)
        self.data, self.c, self.on_done = data, cond, on_done
        self.title("Настройки условия")
        self.transient(root)
        self.resizable(False, False)

        h2(self, ind_info(data, cond["ind"])["name"])
        field_label(self, "Свечи какого размера").pack(anchor="w", pady=(0, 3))
        self.tf = ToggleGroup(self, TFS, on=cond["tf"])
        self.tf.frame.pack(anchor="w")
        self.left = ParamFields(self, data, cond["ind"], cond["p"], cond["shift"])
        self.left.frame.pack(fill="x", pady=(8, 0))

        h2(self, "Сравнить с", second=True)
        self.mode = ToggleGroup(self, [("const", "числом"), ("ind", "индикатором")],
                                on=("ind" if cond["right"] else "const",), multi=False,
                                command=lambda _v: self._show_right())
        self.mode.frame.pack(anchor="w")
        self.rbox = tk.Frame(self, bg=PANEL)
        self.rbox.pack(fill="x")
        self.right, self.rfields = dict(cond["right"] or self._default_right()), None
        self._show_right()

        hint(self, "Несколько значений — через запятую, несколько кнопок — переберутся все "
                   "сочетания. Настройки — для Long; для Short условие и полоса Боллинджера "
                   "зеркалятся сами.", wrap=440).pack(fill="x", pady=(12, 0))
        primary(self, "Готово", self._done).pack(fill="x", pady=(10, 0))
        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Return>", lambda _e: self._done())
        self.update_idletasks()
        self.geometry("+%d+%d" % (root.winfo_rootx() + 380, root.winfo_rooty() + 80))
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass

    def _default_right(self):
        """С чем сравнивает сайт (у цены — нижняя полоса), иначе — средняя EMA(20)."""
        return norm_side(self.data, self.data["norms"].get(self.c["ind"], {}).get("long", {}).get("right")
                         or {"ind": "ma"})

    def _keep_right(self):
        if self.rfields:
            self.right["p"], self.right["shift"] = self.rfields.get()
        self.rfields = None

    def _show_right(self):
        self._keep_right()
        for w in self.rbox.winfo_children():
            w.destroy()
        if self.mode.get() != ["ind"]:
            hint(self.rbox, "Число — в строке условия; можно несколько через запятую.").pack(
                fill="x", pady=(6, 0))
            return
        cat = self.data["catalog"]
        cb = ttk.Combobox(self.rbox, values=[c["name"] for c in cat], state="readonly", width=34,
                          font=(FONT, 9))
        cb.current(next(i for i, c in enumerate(cat) if c["id"] == self.right["ind"]))
        cb.pack(anchor="w", pady=(8, 0))

        def picked(_e):
            self.rfields = None
            ind = cat[cb.current()]["id"]
            self.right = norm_side(self.data, {"ind": ind})
            self.after_idle(self._show_right)
        cb.bind("<<ComboboxSelected>>", picked)
        self.rfields = ParamFields(self.rbox, self.data, self.right["ind"], self.right["p"],
                                   self.right["shift"])
        self.rfields.frame.pack(fill="x", pady=(6, 0))

    def _done(self):
        c = self.c
        c["tf"] = self.tf.get()
        c["p"], c["shift"] = self.left.get()
        if self.mode.get() == ["ind"]:
            self._keep_right()
            c["right"] = self.right
        else:
            c["right"] = None
            if not c["value"].strip():
                c["value"] = norm_cond(self.data, c["ind"])["value"] or "0"
        self.destroy()
        self.on_done()


class CondForm:
    """«Свои условия», как на сайте: условия группы — все сразу («И»), группы — любая («ИЛИ»).
    Отличие от сайта: в полях можно несколько значений через запятую, а размеров свечей —
    несколько кнопок; переберутся все сочетания."""

    def __init__(self, parent, app):
        self.app = app
        self.groups = []
        self.frame = ScrollBox(parent, max_h=330)
        self.box = self.frame.inner

    def changed(self):
        self.render()
        self.app._schedule()

    def add(self, gi=None):
        data = self.app.data
        if gi is None:
            self.groups.append([norm_cond(data, "rsi")])
        else:
            self.groups[gi].append(norm_cond(data, "rsi", self.groups[gi][-1]["tf"]))
        self.changed()

    def drop(self, gi, ci=None):
        if ci is not None:
            del self.groups[gi][ci]
        if ci is None or not self.groups[gi]:
            del self.groups[gi]
        self.changed()

    def rules(self):
        """[] или [правило gbt]; ошибка в полях -> ValueError."""
        return [groups_rule(self.app.data, self.groups)] if self.groups else []

    def render(self):
        for w in self.box.winfo_children():
            w.destroy()
        data = self.app.data
        if not data:
            hint(self.box, "Справочник индикаторов не загружен — «Обновить пары».").pack(
                fill="x", pady=(10, 0))
            return
        lim = data["entry_limits"]
        many = len(self.groups) > 1
        for gi, conds in enumerate(self.groups):
            if gi:
                tk.Label(self.box, text="ИЛИ", bg=PANEL, fg=ACCENT, font=(FONT, 9, "bold")).pack(
                    pady=(6, 0))
            # как на сайте: рамка и «Группа N» — только когда групп несколько
            g = tk.Frame(self.box, bg=PANEL, highlightthickness=1 if many else 0,
                         highlightbackground=LINE, padx=8 if many else 0, pady=4 if many else 0)
            g.pack(fill="x", pady=(10 if not gi else 4, 0))
            if many:
                head = tk.Frame(g, bg=PANEL)
                head.pack(fill="x")
                tk.Label(head, text="Группа %d" % (gi + 1), bg=PANEL, fg=TEXT,
                         font=(FONT, 9, "bold")).pack(side="left")
                mini(head, "×", lambda gi=gi: self.drop(gi), fg=MINUS).pack(side="right")
            for ci, c in enumerate(conds):
                self._row(g, gi, ci, c)
            if len(conds) < lim["filters"]:
                ghost(g, "Добавить фильтр", lambda gi=gi: self.add(gi)).box.pack(anchor="w", pady=(6, 0))
        if not self.groups:
            ghost(self.box, "Добавить фильтр", self.add).box.pack(anchor="w", pady=(10, 0))
        elif len(self.groups) < lim["groups"]:
            ghost(self.box, "Добавить группу ИЛИ", self.add).box.pack(anchor="w", pady=(8, 0))

    def _row(self, parent, gi, ci, c):
        data = self.app.data
        cat = data["catalog"]
        ops = list(data["ops"].items())
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", pady=(6, 0))
        row.columnconfigure(0, weight=1)

        ind = ttk.Combobox(row, values=[x["name"] for x in cat], state="readonly", width=18, font=(FONT, 9))
        ind.current(next(i for i, x in enumerate(cat) if x["id"] == c["ind"]))
        ind.bind("<<ComboboxSelected>>", lambda _e: self._set_ind(c, cat[ind.current()]["id"]))
        ind.grid(row=0, column=0, sticky="ew")

        op = ttk.Combobox(row, values=[t for _, t in ops], state="readonly", width=16, font=(FONT, 9))
        op.current(next((i for i, (k, _) in enumerate(ops) if k == c["op"]), 0))
        op.bind("<<ComboboxSelected>>", lambda _e: (c.update(op=ops[op.current()][0]), self.app._schedule()))
        op.grid(row=0, column=1, padx=(4, 0))
        boxes = [ind, op]

        if c["right"]:
            name = ind_info(data, c["right"]["ind"])["name"]
            tk.Label(row, text=name if len(name) <= 12 else name[:11] + "…", bg=PANEL, fg=TEXT,
                     font=(FONT, 9), width=10, anchor="w").grid(row=0, column=2, padx=(4, 0))
        else:
            var = tk.StringVar(value=c["value"])
            var.trace_add("write", lambda *_a: (c.update(value=var.get()), self.app._schedule()))
            levels = data["norms"].get(c["ind"], {}).get("levels", [])
            # как на сайте: уровни на выбор, своё число (или несколько через запятую) — вписать
            val = ttk.Combobox(row, textvariable=var, values=[fmt_vals([v]) for v in levels], width=9,
                               font=(FONT, 9))
            val.grid(row=0, column=2, padx=(4, 0))
            boxes.append(val)
        for b in boxes:
            b.bind("<MouseWheel>", self.frame.wheel)     # колесо листает условия, а не меняет выбор
        mini(row, "⚙", lambda: CondDialog(self.app.root, data, c, self.changed)).grid(
            row=0, column=3, padx=(4, 0))
        mini(row, "×", lambda: self.drop(gi, ci), fg=MINUS).grid(row=0, column=4, padx=(4, 0))
        hint(row, self._summary(c), wrap=420).grid(row=1, column=0, columnspan=5, sticky="w")

    def _set_ind(self, c, ind):
        """Сменили индикатор — условие и уровень по норме сайта; свечи и сдвиг остаются."""
        new = norm_cond(self.app.data, ind, c["tf"], c["shift"])
        c.clear()
        c.update(new)
        self.app.root.after_idle(self.changed)    # строку пересобираем не внутри её же события

    def _params_text(self, ind, p, shift):
        data = self.app.data
        meta = ind_info(data, ind)
        bits = []
        for k in meta.get("params", []):
            v = p.get(k)
            if k == "period":
                bits.append("период %s" % v)
            elif k == "hours":
                bits.append("за %s ч" % v)
            elif k == "mult":
                bits.append("%s %s" % ("σ" if ind == "bb" else "множитель", v))
            else:
                names = {"method": data["methods"], "price": data["prices"]}.get(k) or meta.get("lines", {})
                bits.append(", ".join(names.get(x, x).split(" (")[0] for x in v or ()))
        if pieces(shift) not in ([], ["0"]):
            bits.append("сдвиг %s" % shift)
        return bits

    def _summary(self, c):
        tfs = dict(TFS)
        text = " · ".join(["свечи " + ", ".join(tfs.get(t, str(t)) for t in c["tf"])]
                          + self._params_text(c["ind"], c["p"], c["shift"]))
        if c["right"]:
            r = c["right"]
            bits = self._params_text(r["ind"], r["p"], r["shift"])
            text += " · с: %s%s" % (ind_info(self.app.data, r["ind"])["name"],
                                    " (%s)" % ", ".join(bits) if bits else "")
        return text


# ---------- окно ----------

class App:
    def __init__(self, root):
        self.root = root
        self.data = None
        self.proc = None
        self.proc_args = None
        self.stopping = False
        self.q = queue.Queue()
        self._data_loading = False
        self._filling = False          # программное заполнение полей — не сбрасывать пресет/годы
        self._recalc_id = None
        self._left_gen = 0
        self._done_cache = {}          # путь CSV -> (mtime, size, ключи)

        self.sel_symbols = set(DEFAULT_SYMBOLS)
        self.all_symbols = False
        self.sel_tpls = None           # None — до загрузки справочника выбираются все связки

        root.title("gridbacktest — массовые бэктесты")
        root.configure(bg=BG)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry("%dx%d" % (min(1440, sw - 40), min(900, sh - 80)))
        root.minsize(1180, 700)
        self._style()

        self._header()
        self.status = tk.Label(root, text="Готово", bg=BG, fg=DIM, anchor="w",
                               font=(FONT, 9), padx=14, pady=4)
        self.status.pack(side="bottom", fill="x")
        body = tk.Frame(root, bg=BG, padx=12, pady=12)
        body.pack(fill="both", expand=True)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(2, weight=1)
        left, mid, right = panel(body), panel(body), panel(body)
        left.grid(row=0, column=0, sticky="new", padx=(0, 12))
        mid.grid(row=0, column=1, sticky="new", padx=(0, 12))
        right.grid(row=0, column=2, sticky="nsew")
        self._pair_panel(left)
        self._grid_panel(mid)
        self._log_panel(right)
        self._advanced()

        self._refresh_entry()
        self._update_pickers()
        self._show_period()
        root.after(100, self._poll)
        if os.path.exists(PAIRS_CACHE):
            try:
                with open(PAIRS_CACHE, encoding="utf-8") as f:
                    self._apply_data(json.load(f), say=False)
            except (OSError, ValueError, KeyError, IndexError):
                pass
        self.refresh_data()
        self.check_session()
        self._schedule()

    def _style(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure("TEntry", fieldbackground=PANEL, foreground=TEXT, bordercolor=FIELD,
                     lightcolor=PANEL, darkcolor=PANEL, padding=(6, 4), insertcolor=TEXT)
        st.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
               fieldbackground=[("disabled", "#f2f4f6")], foreground=[("disabled", "#9aa3ae")])
        st.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=TEXT,
                     bordercolor=FIELD, lightcolor=PANEL, darkcolor=PANEL, arrowcolor=DIM,
                     padding=(4, 3))
        st.map("TCombobox", bordercolor=[("focus", ACCENT)],
               fieldbackground=[("readonly", PANEL)], selectbackground=[("readonly", PANEL)],
               selectforeground=[("readonly", TEXT)])
        self.root.option_add("*TCombobox*Listbox.font", (FONT, 9))
        st.configure("TCheckbutton", background=PANEL, foreground=TEXT, font=(FONT, 9),
                     indicatorbackground=PANEL, indicatorforeground=ACCENT)
        st.map("TCheckbutton", background=[("active", PANEL)])
        for sb in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            st.configure(sb, background=CHIP, troughcolor=PANEL, bordercolor=PANEL,
                         arrowcolor=DIM, lightcolor=CHIP, darkcolor=CHIP)
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                     font=(FONT, 9), rowheight=24, bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL)
        st.map("Treeview", background=[("selected", TINT)], foreground=[("selected", ACCENT)])
        st.configure("Treeview.Heading", background=CHIP, foreground=DIM, font=(FONT, 9, "bold"),
                     bordercolor=LINE, lightcolor=CHIP, darkcolor=CHIP, relief="flat")
        st.map("Treeview.Heading", background=[("active", CHIP_HOVER)])

    def _entry(self, parent, var, width=10, center=True):
        e = ttk.Entry(parent, textvariable=var, width=width, justify="center" if center else "left",
                      font=(FONT, 10))
        return e

    def _var(self, value="", watch=True):
        v = tk.StringVar(value=value)
        if watch:
            v.trace_add("write", self._schedule)
        return v

    # ---------- шапка ----------

    def _header(self):
        bar = tk.Frame(self.root, bg=PANEL, padx=20, pady=10, highlightthickness=1,
                       highlightbackground=LINE)
        bar.pack(side="top", fill="x")
        brand = tk.Frame(bar, bg=PANEL)
        brand.pack(side="left")
        tk.Label(brand, text="Pifagor Trading Bot", bg=PANEL, fg=TEXT,
                 font=(FONT, 12, "bold")).pack(anchor="w")
        tk.Label(brand, text="Массовые бэктесты gridbacktest.com", bg=PANEL, fg=DIM,
                 font=(FONT, 9, "italic")).pack(anchor="w")
        for text, cmd in (("Обновить пары", self.refresh_data),
                          ("Проверить вход", lambda: self.run_cmd(["me"])),
                          ("Войти через Telegram", lambda: self.run_cmd(["login"]))):
            ghost(bar, text, cmd).box.pack(side="right", padx=(6, 0))
        self.session = tk.Label(bar, text="вход: проверяю…", bg=PANEL, fg=DIM, font=(FONT, 9, "bold"))
        self.session.pack(side="right", padx=(0, 12))

    # ---------- «Пара и период» + «Точка входа» ----------

    def _pair_panel(self, p):
        h2(p, "Пара и период")
        row = tk.Frame(p, bg=PANEL)
        row.pack(fill="x")
        row.columnconfigure(0, weight=1, uniform="r")
        row.columnconfigure(1, weight=1, uniform="r")
        field_label(row, "Пара").grid(row=0, column=0, pady=(0, 3))
        field_label(row, "Сторона").grid(row=0, column=1, pady=(0, 3))
        self.pair_btn = pick_button(row, self.pick_pairs)
        self.pair_btn.box.grid(row=1, column=0, sticky="ew", padx=(0, 4))
        self.side = ToggleGroup(row, [("long", "Long"), ("short", "Short")], on=("long", "short"),
                                command=self._schedule, stretch=True)
        self.side.frame.grid(row=1, column=1, sticky="ew", padx=(4, 0))

        row = tk.Frame(p, bg=PANEL)
        row.pack(fill="x", pady=(8, 0))
        row.columnconfigure(0, weight=1, uniform="r")
        row.columnconfigure(1, weight=1, uniform="r")
        field_label(row, "с").grid(row=0, column=0, pady=(0, 3))
        field_label(row, "по").grid(row=0, column=1, pady=(0, 3))
        self.date_from = self._var("2025-10-01")
        self.date_to = self._var("2026-09-30")
        for col, var in ((0, self.date_from), (1, self.date_to)):
            e = self._entry(row, var, width=12)
            e.grid(row=1, column=col, sticky="ew", padx=(0, 4) if col == 0 else (4, 0))
            var.trace_add("write", self._dates_edited)

        row = tk.Frame(p, bg=PANEL)
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, text="лет", bg=PANEL, fg=DIM, font=(FONT, 9, "bold")).pack(side="left", padx=(0, 6))
        self.years = ToggleGroup(row, [(y, str(y)) for y in YEARS], on=(4,), multi=False,
                                 need_one=True, command=self._years_clicked, stretch=True)
        self.years.frame.pack(side="left", fill="x", expand=True)
        self.period_note = hint(p)
        self.period_note.pack(fill="x", pady=(6, 0))

        # --- точка входа ---
        h2(p, "Точка входа", second=True)
        # как на сайте: способ входа один — кнопки переключают вкладки, а не отмечают несколько
        self.modes = ToggleGroup(p, [("none", "Всегда в рынке"), ("tpl", "Готовые связки"),
                                     ("own", "Свои условия")], on=("none",), multi=False,
                                 command=self._mode_clicked, stretch=True)
        self.modes.frame.pack(fill="x")

        self.ent_body = tk.Frame(p, bg=PANEL)
        self.ent_body.pack(fill="x")

        self.pane_none = tk.Frame(self.ent_body, bg=PANEL)
        field_label(self.pane_none, "Пауза перезахода").pack(anchor="w", pady=(10, 3))
        self.chart_tf = ToggleGroup(self.pane_none, TFS, on=(60,), command=self._schedule)
        self.chart_tf.frame.pack(anchor="w")
        hint(self.pane_none, "Бот входит снова после закрытия сделки, но не чаще одной "
                             "свечи этого интервала").pack(fill="x", pady=(4, 0))

        self.pane_tpl = tk.Frame(self.ent_body, bg=PANEL)
        field_label(self.pane_tpl, "Связки").pack(anchor="w", pady=(10, 3))
        self.tpl_btn = pick_button(self.pane_tpl, self.pick_tpls)
        self.tpl_btn.box.pack(fill="x")
        self.swing_row = tk.Frame(self.pane_tpl, bg=PANEL)
        field_label(self.swing_row, "Откат/отскок за 4 ч (swing), %").pack(side="left")
        self.swing = self._var("1")
        self._entry(self.swing_row, self.swing, width=12).pack(side="right")

        self.pane_tf = tk.Frame(self.ent_body, bg=PANEL)
        field_label(self.pane_tf, "Интервал связки").pack(anchor="w", pady=(10, 3))
        self.ent_tf = ToggleGroup(self.pane_tf, TFS, on=(60,), command=self._schedule)
        self.ent_tf.frame.pack(anchor="w")

        self.pane_own = tk.Frame(self.ent_body, bg=PANEL)
        self.form = CondForm(self.pane_own, self)
        self.form.frame.pack(fill="x")
        hint(self.pane_own, "Условия группы — все сразу («И»), группы — любая («ИЛИ»). Условие и "
                            "число — для Long, для Short зеркалятся сами (RSI < 30 → > 70). Через "
                            "запятую и кнопками ⚙ — несколько значений: переберутся все.").pack(
            fill="x", pady=(6, 0))
        self.json_btn = ghost(self.pane_own, "", self._show_json)
        self.json_btn.box.pack(anchor="w", pady=(8, 0))
        self.json_filled = False

        # ещё правила JSON — отдельным окном, как «Дополнительно»: в колонке им нет места
        w = self.json_win = tk.Toplevel(self.root, bg=PANEL, padx=16, pady=14)
        w.withdraw()
        w.title("Правила JSON")
        w.transient(self.root)
        w.protocol("WM_DELETE_WINDOW", self._hide_json)
        head = h2(w, "Ещё правила (indicators, JSON)")
        ghost(head, "Пример", lambda: self._put_example(self.ind_text, INDICATORS_EXAMPLE)).box.pack(side="right")
        box = tk.Frame(w, bg=FIELD)
        box.pack(fill="both", expand=True)
        self.ind_text = tk.Text(box, height=14, width=70, wrap="none", font=("Consolas", 9),
                                bd=0, highlightthickness=0, bg=PANEL, fg=TEXT, insertbackground=TEXT)
        self.ind_text.pack(fill="both", expand=True, padx=1, pady=1)
        self._watch_text(self.ind_text)
        hint(w, "Каждое правило — отдельный вариант входа, в дополнение к условиям в окне. Поля и "
                "примеры — в README. Без tf берётся «Интервал связки».", wrap=500).pack(fill="x", pady=(4, 0))
        primary(w, "Готово", self._hide_json).pack(fill="x", pady=(10, 0))
        self._json_label()

    def _show_json(self):
        self.json_win.deiconify()
        self.json_win.lift()
        self.json_win.geometry("+%d+%d" % (self.root.winfo_rootx() + 420, self.root.winfo_rooty() + 120))
        self.ind_text.focus_set()

    def _hide_json(self):
        self.json_win.withdraw()
        self._schedule()

    def _json_label(self):
        filled = bool(self.ind_text.get("1.0", "end").strip())
        self.json_btn.config(text="Ещё правила JSON…" + (" ●" if filled else ""))
        if filled != self.json_filled:
            self.json_filled = filled
            self._refresh_entry()

    def _mode_clicked(self, _v):
        # включили «Свои условия» — сразу одно условие, как на сайте
        if "own" in self.modes.get() and not self.form.groups and self.data:
            self.form.add()
        self._refresh_entry()
        self._schedule()

    def _refresh_entry(self):
        modes = self.modes.get()
        for f in (self.pane_none, self.pane_tpl, self.pane_tf, self.pane_own):
            f.pack_forget()
        if "none" in modes:
            self.pane_none.pack(fill="x")
        if "tpl" in modes:
            self.pane_tpl.pack(fill="x")
        # у своих условий свечи — в ⚙ каждого; общий интервал нужен связкам и правилам JSON без tf
        if "tpl" in modes or ("own" in modes and self.json_filled):
            self.pane_tf.pack(fill="x")
        if "own" in modes:
            self.pane_own.pack(fill="x")
        self.swing_row.pack_forget()
        if "swing" in (self.sel_tpls or ()):
            self.swing_row.pack(fill="x", pady=(8, 0))

    def _years_clicked(self, _v):
        self._show_period()
        self._schedule()

    def _dates_edited(self, *_):
        if not self._filling:
            self.years.set([])        # вписали дату — значит, диапазон дат, а не «последние N лет»
            self._show_period()

    def _show_period(self):
        """Как на сайте: кнопка «лет» подставляет даты (по первой выбранной паре)."""
        y = self.years.get()
        if not y:
            self.period_note.config(text="С %s по %s; у пар с более короткой историей — с её начала."
                                    % (self.date_from.get(), self.date_to.get()))
            return
        syms = sorted(self.data["pairs"]) if (self.data and self.all_symbols) else sorted(self.sel_symbols)
        sym = next((s for s in syms if self.data and s in self.data["pairs"]), None)
        text = "Последние %d %s каждой пары" % (y[0], plural(y[0], "год", "года", "лет"))
        if sym:
            lo, hi, months = gbt.period_for(self.data["pairs"][sym], {"years": y[0]})
            self._filling = True
            self.date_from.set(lo)
            self.date_to.set(hi)
            self._filling = False
            if len(months) < y[0] * 12:
                text += " (у %s история на сайте короче — берётся вся: %s — %s)" % (base(sym), lo, hi)
            else:
                text += " (у %s: %s — %s)" % (base(sym), lo, hi)
        self.period_note.config(text=text + ".")

    # ---------- «Настройки сетки» ----------

    def _grid_panel(self, p):
        head = h2(p, "Настройки сетки")
        ghost(head, "Дополнительно…", self.show_advanced).box.pack(side="right")

        self.presets = ToggleGroup(p, [("calm", "Консервативно"), ("mid", "Оптимально"),
                                       ("hot", "Агрессивно")], multi=False, need_one=False,
                                   command=self._preset, stretch=True)
        self.presets.frame.pack(fill="x", pady=(0, 10))

        tbl = tk.Frame(p, bg=PANEL)
        tbl.pack(fill="x")
        tbl.columnconfigure(1, weight=1)
        for c, text in enumerate(("", "Значение", "", "от", "до", "шаг", "вариантов")):
            field_label(tbl, text).grid(row=0, column=c, pady=(0, 2))
        self.rows = {}
        for r, spec in enumerate(PARAMS, 1):
            self._param_row(tbl, r, spec)

        r = len(PARAMS) + 1
        tk.Label(tbl, text="Реинвест, %", bg=PANEL, fg=TEXT, font=(FONT, 9)).grid(
            row=r, column=0, sticky="w", pady=3)
        self.reinvest = ToggleGroup(tbl, REINVEST, on=(0,), command=self._schedule)
        self.reinvest.frame.grid(row=r, column=1, columnspan=5, sticky="w")
        self.reinvest_count = tk.Label(tbl, text="1", bg=PANEL, fg=DIM, font=(FONT, 9, "bold"), width=8)
        self.reinvest_count.grid(row=r, column=6)
        tk.Frame(tbl, bg=LINE, height=1).grid(row=r + 1, column=0, columnspan=7, sticky="ew", pady=(3, 1))
        self._param_row(tbl, r + 2, STOP_ROW)

        hint(p, "«Значение» — одно или через запятую (дробные — через точку); «перебор» — от/до/шаг. "
                "Стоп — % от средней, переносится при доборе, 0 — без стопа; считается по сделкам "
                "прогона, без новых прогонов на сервере.", wrap=550).pack(fill="x", pady=(4, 0))

        fees = tk.Frame(p, bg=PANEL)
        fees.pack(fill="x", pady=(6, 0))
        fees.columnconfigure(0, weight=1, uniform="f")
        fees.columnconfigure(1, weight=1, uniform="f")
        field_label(fees, "Комиссия мейкер, %").grid(row=0, column=0, pady=(0, 3))
        field_label(fees, "Комиссия тейкер, %").grid(row=0, column=1, pady=(0, 3))
        self.fee_maker = self._var("0.02")
        self.fee_taker = self._var("0.05")
        self._entry(fees, self.fee_maker).grid(row=1, column=0, sticky="ew", padx=(0, 4))
        self._entry(fees, self.fee_taker).grid(row=1, column=1, sticky="ew", padx=(4, 0))

        self.err = hint(p, fg=MINUS, wrap=540)
        self.err.pack(fill="x", pady=(2, 0))

        cnt = tk.Frame(p, bg=TINT, padx=14, pady=10)
        cnt.pack(fill="x", pady=(4, 0))
        top = tk.Frame(cnt, bg=TINT)
        top.pack(anchor="w")
        self.total_lbl = tk.Label(top, text="—", bg=TINT, fg=ACCENT, font=(FONT, 20, "bold"))
        self.total_lbl.pack(side="left")
        self.total_word = tk.Label(top, text="комбинаций", bg=TINT, fg=TEXT, font=(FONT, 11))
        self.total_word.pack(side="left", padx=(6, 0), pady=(8, 0))
        self.parts_lbl = hint(cnt, bg=TINT, wrap=520)
        self.parts_lbl.pack(fill="x")
        self.eta_lbl = hint(cnt, bg=TINT, fg=TEXT, wrap=520)
        self.eta_lbl.pack(fill="x", pady=(2, 0))

        self.run_btn = primary(p, "Запустить прогон", self.start_sweep)
        self.run_btn.pack(fill="x", pady=(10, 0))
        row = tk.Frame(p, bg=PANEL)
        row.pack(fill="x", pady=(6, 0))
        self.resume_btn = ghost(row, "Продолжить досчёт", lambda: self.start_sweep(force=True))
        self.resume_btn.box.pack(side="left")
        self.stop_btn = ghost(row, "■ Стоп", self.stop_cmd)
        self.stop_btn.config(state="disabled", fg=MINUS, activeforeground=MINUS)
        self.stop_btn.box.pack(side="left", padx=6)
        ghost(row, "Открыть CSV", self.open_csv).box.pack(side="right")
        ghost(row, "Лучшие результаты", self.show_top).box.pack(side="right", padx=6)

    def _param_row(self, tbl, r, spec):
        key, label, default, lim, isint, (lo, hi, step) = spec
        tk.Label(tbl, text=label, bg=PANEL, fg=TEXT, font=(FONT, 9)).grid(
            row=r, column=0, sticky="w", padx=(0, 10), pady=3)
        row = {"label": label, "limit": lim, "int": isint, "default": default,
               "value": self._var("%g" % default), "lo": self._var("%g" % lo),
               "hi": self._var("%g" % hi), "step": self._var("%g" % step)}
        if key in PRESETS["mid"]:      # правка поля снимает подсветку пресета, как на сайте
            row["value"].trace_add("write", lambda *_a: self._filling or self.presets.set([]))
        row["e_value"] = self._entry(tbl, row["value"], width=12)
        row["e_value"].grid(row=r, column=1, sticky="ew")
        row["range"] = Toggle(tbl, "перебор", padx=8,
                              command=lambda t, k=key: self._toggle_range(k))
        row["range"].grid(row=r, column=2, padx=8)
        for c, k in ((3, "lo"), (4, "hi"), (5, "step")):
            row["e_" + k] = self._entry(tbl, row[k], width=7)
            row["e_" + k].grid(row=r, column=c, padx=(0, 4))
        row["count"] = tk.Label(tbl, text="1", bg=PANEL, fg=DIM, font=(FONT, 9, "bold"), width=8)
        row["count"].grid(row=r, column=6)
        self.rows[key] = row
        self._row_state(key)

    def _toggle_range(self, key):
        row = self.rows[key]
        row["range"].set(not row["range"].on)
        self._row_state(key)
        self.presets.set([])
        self._schedule()

    def _row_state(self, key):
        row = self.rows[key]
        on = row["range"].on
        row["e_value"].config(state="disabled" if on else "normal")
        for k in ("lo", "hi", "step"):
            row["e_" + k].config(state="normal" if on else "disabled")

    def _preset(self, name):
        if name not in self.presets.get():
            return
        self._filling = True
        for k, v in PRESETS[name].items():
            self.rows[k]["range"].set(False)
            self.rows[k]["value"].set("%g" % v)
            self._row_state(k)
        self._filling = False
        self._schedule()

    # ---------- журнал ----------

    def _log_panel(self, p):
        head = h2(p, "Журнал")
        ghost(head, "Очистить", self._clear_log).box.pack(side="right")
        box = tk.Frame(p, bg=LINE)
        box.pack(fill="both", expand=True)
        # без переноса строк: таблица «Лучших результатов» должна стоять колонками
        self.log = tk.Text(box, wrap="none", state="disabled", font=("Consolas", 9), bd=0,
                           highlightthickness=0, bg=PANEL, fg=TEXT, padx=6, pady=4)
        self.log.tag_config("err", foreground=MINUS)
        self.log.tag_config("warn", foreground=WARN)
        self.log.tag_config("ok", foreground=PLUS)
        self.log.tag_config("cmd", foreground=ACCENT)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.log.yview)
        hs = ttk.Scrollbar(box, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=sb.set, xscrollcommand=hs.set)
        hs.pack(side="bottom", fill="x", padx=1, pady=(0, 1))
        sb.pack(side="right", fill="y", padx=(0, 1), pady=1)
        self.log.pack(fill="both", expand=True, padx=(1, 0), pady=1)

    def _clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    # ---------- «Дополнительно» ----------

    def _advanced(self):
        w = self.adv = tk.Toplevel(self.root, bg=PANEL, padx=16, pady=14)
        w.withdraw()
        w.title("Дополнительно")
        w.transient(self.root)
        w.protocol("WM_DELETE_WINDOW", self._hide_advanced)
        h2(w, "Прогон")

        def line(label, var, width=14):
            r = tk.Frame(w, bg=PANEL)
            r.pack(fill="x", pady=2)
            tk.Label(r, text=label, bg=PANEL, fg=TEXT, font=(FONT, 9), width=30, anchor="w").pack(side="left")
            self._entry(r, var, width=width, center=False).pack(side="left", fill="x", expand=True)

        self.workers = self._var("2")
        self.rate_min = self._var("18")
        self.rate_hour = self._var("145")
        self.output = self._var("results.csv")
        self.entry_hold = self._var("60")
        self.extra_periods = self._var("")
        line("Файл результатов (CSV, в папке results)", self.output)
        line("Параллельных запросов", self.workers)
        line("Прогонов в минуту (сервер: 20)", self.rate_min)
        line("Прогонов в час (сервер: 150)", self.rate_hour)
        line("Вход открыт после сигнала, мин", self.entry_hold)

        h2(w, "Ещё периоды и наборы", second=True)
        line("Ещё периоды (JSON)", self.extra_periods, width=40)
        hint(w, 'например: [{"years": 2}, {"from": "2022-01-01", "to": "2024-12-31"}]',
             wrap=480).pack(fill="x")
        r = tk.Frame(w, bg=PANEL)
        r.pack(fill="x", pady=(10, 3))
        field_label(r, "Явные наборы параметров (sets, JSON)").pack(side="left")
        ghost(r, "Пример", lambda: self._put_example(self.sets_text, SETS_EXAMPLE)).box.pack(side="right")
        box = tk.Frame(w, bg=FIELD)
        box.pack(fill="x")
        self.sets_text = tk.Text(box, height=5, width=60, wrap="none", font=("Consolas", 9), bd=0,
                                 highlightthickness=0, bg=PANEL, fg=TEXT, insertbackground=TEXT)
        self.sets_text.pack(fill="x", padx=1, pady=1)
        self._watch_text(self.sets_text)
        hint(w, "Добавляются к сетке; недостающие параметры берутся из «Значение». Если перебора "
                "нет нигде, считаются только эти наборы.", wrap=480).pack(fill="x", pady=(4, 0))

        h2(w, "Конфиг", second=True)
        r = tk.Frame(w, bg=PANEL)
        r.pack(fill="x")
        ghost(r, "Сохранить…", self.save_cfg).box.pack(side="left")
        ghost(r, "Загрузить…", self.load_cfg).box.pack(side="left", padx=6)
        ghost(r, "Папка configs", lambda: self._open(gbt.CONFIGS_DIR)).box.pack(side="left")
        ghost(r, "Папка results", lambda: self._open(gbt.RESULTS_DIR)).box.pack(side="left", padx=6)
        primary(w, "Готово", self._hide_advanced).pack(fill="x", pady=(14, 0))

    def show_advanced(self):
        self.adv.deiconify()
        self.adv.lift()
        self.adv.geometry("+%d+%d" % (self.root.winfo_rootx() + 420, self.root.winfo_rooty() + 80))

    def _hide_advanced(self):
        self.adv.withdraw()
        self._schedule()

    def _watch_text(self, t):
        def changed(_e):
            if t.edit_modified():
                t.edit_modified(False)
                self._schedule()
        t.bind("<<Modified>>", changed)

    @staticmethod
    def _put_example(text, example):
        if text.get("1.0", "end").strip() and not messagebox.askyesno(
                "Пример", "Заменить содержимое поля примером?"):
            return
        text.delete("1.0", "end")
        text.insert("1.0", example)

    # ---------- выбор пары и связок ----------

    def pick_pairs(self):
        if not self.data:
            messagebox.showinfo("Пара", "Список пар ещё не загружен — «Обновить пары».")
            return
        items = []
        for s in sorted(self.data["pairs"]):
            m = self.data["pairs"][s][0]
            items.append((s, "%s  ·  с %s.%s" % (base(s), m[5:7], m[:4]), s.lower()))
        Picker(self.root, "Пара", items, self.sel_symbols, self._pairs_done,
               all_text="Все пары, включая будущие новые (\"all\")", all_on=self.all_symbols,
               find_hint="Поиск: btc, sol, near…")

    def _pairs_done(self, sel, everything):
        self.sel_symbols, self.all_symbols = set(sel), everything
        self._update_pickers()
        self._show_period()
        self._schedule()

    def pick_tpls(self):
        if not self.data:
            messagebox.showinfo("Связки", "Справочник ещё не загружен — «Обновить пары».")
            return
        items = [(t["id"], "%s — %s / %s" % (t["id"], t["long"]["name"], t["short"]["name"]),
                  ("%s %s %s" % (t["id"], t["long"]["name"], t["short"]["name"])).lower())
                 for t in self.data.get("templates", [])]
        Picker(self.root, "Готовые связки", items, self.sel_tpls or (), self._tpls_done,
               find_hint="Поиск: rsi, bb, swing…")

    def _tpls_done(self, sel, _everything):
        self.sel_tpls = set(sel)
        self._update_pickers()
        self._refresh_entry()
        self._schedule()

    def _tpl_ids(self):
        return [t["id"] for t in (self.data or {}).get("templates", [])]

    def _update_pickers(self):
        if self.all_symbols:
            n = len(self.data["pairs"]) if self.data else 0
            self.pair_btn.config(text="Все пары%s  ▾" % (" (%d)" % n if n else ""))
        else:
            known = sorted(self.data["pairs"]) if self.data else []
            names = [base(s) for s in known if s in self.sel_symbols]
            names += [base(s) for s in sorted(self.sel_symbols - set(known))]
            self.pair_btn.config(text=short_list(names, "Все пары", len(known)) + "  ▾")
        ids = self._tpl_ids()
        names = [i for i in ids if i in (self.sel_tpls or ())]
        self.tpl_btn.config(text=short_list(names, "Все связки", len(ids)) + "  ▾")

    # ---------- служебное ----------

    def say(self, line="", tag=None):
        self.log.config(state="normal")
        self.log.insert("end", line.rstrip("\n") + "\n", tag or ())
        self.log.see("end")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 4000:
            self.log.delete("1.0", "%d.0" % (lines - 2000))
        self.log.config(state="disabled")

    def set_status(self, text):
        self.status.config(text=text)

    def on_error(self, exc, val, tb):
        """Ошибка в обработчике кнопки — в журнал: консоли у окна нет (pythonw)."""
        self.say("ОШИБКА в окне: %s: %s" % (exc.__name__, val), "err")
        for line in traceback.format_exception(exc, val, tb)[-4:]:
            self.say("  " + line.rstrip(), "err")
        self.set_status("Ошибка в окне — подробности в журнале")

    def _apply_data(self, data, say=True):
        self.data = data
        if self.sel_tpls is None:
            self.sel_tpls = set(self._tpl_ids())
        for key, row in self.rows.items():
            lim = (data.get("limits") or {}).get(key)
            if lim:
                row["limit"] = (norm(float(lim[0])), norm(float(lim[1])))
        self._update_pickers()
        self._refresh_entry()
        self.form.render()
        self._show_period()
        self._schedule()
        if say:
            self.say("Справочник обновлён: пар %d, связок входа %d, индикаторов %d"
                     % (len(data.get("pairs", {})), len(data.get("templates", [])),
                        len(data.get("catalog", []))), "ok")

    def refresh_data(self):
        if self._data_loading:
            return
        self._data_loading = True
        self.set_status("Загружаю список пар…")
        threading.Thread(target=self._fetch_data, daemon=True).start()

    def _fetch_data(self):
        data, err = None, None
        try:
            data = gbt.get_data(gbt.new_session())
        except Exception as e:
            err = e
        if data:
            try:
                os.makedirs(gbt.DATA_DIR, exist_ok=True)
                with open(PAIRS_CACHE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
            except OSError:
                pass
            self.q.put(("data", data))
        elif self.data:
            self.q.put(("nodata", "Сервер недоступен (%s) — оставил кэш пар" % err))
        else:
            self.q.put(("nodata", "Не удалось получить список пар: %s" % err))
        self._data_loading = False

    def check_session(self):
        def work():
            try:
                me = gbt.get_me(gbt.load_session(), fresh=True)
            except Exception:
                me = None
            self.q.put(("me", me))
        threading.Thread(target=work, daemon=True).start()

    def _show_session(self, me):
        if me is None:
            self.session.config(text="вход: сервер недоступен", fg=DIM)
        elif not me.get("id"):
            self.session.config(text="● не вошли", fg=MINUS)
        elif me.get("bots") == "active":
            self.session.config(text="● %s — итоги открыты" % (me.get("name") or me["id"]), fg=PLUS)
        else:
            self.session.config(text="● %s — итоги закрыты (бот: %s)"
                                % (me.get("name") or me["id"], me.get("bots")), fg=WARN)

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "data":
                    self._apply_data(payload)
                    self.set_status("Готово")
                elif kind == "nodata":
                    self.say(payload, "err")
                    self.set_status(payload)
                elif kind == "me":
                    self._show_session(payload)
                elif kind == "left":
                    self._show_left(*payload)
                elif kind == "line":
                    self.say(payload, "err" if ("ОШИБКА" in payload or "Traceback" in payload)
                             else "warn" if "ЛИКВИДАЦИ" in payload
                             else ("cmd" if payload.startswith("$ ") else None))
                elif kind == "done":
                    self._on_done(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    # ---------- сборка конфига ----------

    def _row_values(self, key):
        """Значения параметра сетки (список); ошибка -> ValueError с подписью параметра."""
        if key == "reinvest":
            vals = self.reinvest.get()
            if not vals:
                raise ValueError("Реинвест: выберите значение")
            return vals
        row = self.rows[key]
        (lo_lim, hi_lim), isint = row["limit"], row["int"]
        try:
            if row["range"].on:
                # у целых параметров шаг тоже целый — иначе в интервал попадут 1.5 ордера
                vals = make_range(num(row["lo"].get(), isint), num(row["hi"].get(), isint),
                                  num(row["step"].get(), isint))
            else:
                vals = [num(t, isint) for t in pieces(row["value"].get())]
                if not vals:
                    raise ValueError("пусто")
            for v in vals:
                if not (lo_lim <= v <= hi_lim):
                    raise ValueError("%s вне границ сайта %g…%g" % (v, lo_lim, hi_lim))
        except ValueError as e:
            raise ValueError("%s: %s" % (row["label"], e))
        return list(dict.fromkeys(vals))

    @staticmethod
    def _int_list(var, label):
        vals = []
        for t in pieces(var.get()):
            try:
                vals.append(int(t))
            except ValueError:
                raise ValueError("%s: «%s» — не целое число" % (label, t))
        if not vals:
            raise ValueError("%s: пусто" % label)
        return vals

    @staticmethod
    def _pos_int(var, label):
        try:
            v = int(var.get().strip())
        except ValueError:
            raise ValueError("%s: нужно целое число" % label)
        if v < 1:
            raise ValueError("%s: должно быть больше нуля" % label)
        return v

    def _periods(self):
        y = self.years.get()
        if y:
            period = {"years": y[0]}
        else:
            lo, hi = self.date_from.get().strip(), self.date_to.get().strip()
            for t, nm in ((lo, "дата «с»"), (hi, "дата «по»")):
                try:
                    datetime.strptime(t, "%Y-%m-%d")
                except ValueError:
                    raise ValueError("%s: нужна дата ГГГГ-ММ-ДД, получено «%s»" % (nm, t))
            if lo > hi:
                raise ValueError("дата «с» позже даты «по»")
            period = {"from": lo, "to": hi}
        extra = parse_json_list(self.extra_periods.get(), "Ещё периоды") or []
        for spec in extra:
            if not ("years" in spec or "from" in spec or "to" in spec):
                raise ValueError("Ещё периоды: нужен years или from/to: %s" % json.dumps(spec))
        return [period] + extra

    def build_cfg(self):
        """-> (cfg, None) либо (None, текст ошибки)"""
        try:
            if self.all_symbols:
                symbols = "all"
            else:
                symbols = sorted(self.sel_symbols)
                if not symbols:
                    raise ValueError("выберите хотя бы одну пару")

            modes = self.modes.get()
            entries = ["none"] if "none" in modes else []
            if "tpl" in modes:
                tpls = [i for i in self._tpl_ids() if i in (self.sel_tpls or ())]
                if not tpls:
                    raise ValueError("Готовые связки: выберите хотя бы одну (или выключите кнопку)")
                entries += tpls
            indicators = None
            if "own" in modes:
                if not self.data:
                    raise ValueError("Свои условия: справочник индикаторов не загружен — «Обновить пары»")
                indicators = self.form.rules() + (
                    parse_json_list(self.ind_text.get("1.0", "end"), "Правила JSON") or [])
                if not indicators:
                    raise ValueError("Свои условия: добавьте условие или выключите кнопку")

            swing = [1]
            if "swing" in entries:
                try:
                    swing = [num(t, False) for t in pieces(self.swing.get())]
                except ValueError as e:
                    raise ValueError("Порог swing: %s" % e)
                if not swing or not all(0.1 <= v <= 50 for v in swing):
                    raise ValueError("Порог swing: от 0.1 до 50 %")

            defaults, grid = {}, {}
            for key in PARAM_NAMES:
                vals = self._row_values(key)
                if len(vals) == 1:
                    defaults[key] = vals[0]
                else:
                    grid[key] = vals
            n_sets = 1
            for v in grid.values():
                n_sets *= len(v)
            if n_sets > MAX_PARAM_SETS:
                raise ValueError("сетка даёт %s наборов параметров (лимит %s) — сократите перебор"
                                 % (spaced(n_sets), spaced(MAX_PARAM_SETS)))

            sets = parse_json_list(self.sets_text.get("1.0", "end"), "sets")
            for s in sets or []:
                bad = [k for k in s if k not in PARAM_NAMES]
                if bad:
                    raise ValueError("sets: неизвестные параметры %s (можно: %s)"
                                     % (", ".join(bad), ", ".join(PARAM_NAMES)))

            fees = {}
            for k, var, label in (("fee_maker", self.fee_maker, "Комиссия мейкер"),
                                  ("fee_taker", self.fee_taker, "Комиссия тейкер")):
                try:
                    pct = num(var.get(), False)
                except ValueError as e:
                    raise ValueError("%s: %s" % (label, e))
                if not 0 <= pct <= 1:
                    raise ValueError("%s: от 0 до 1 %%" % label)
                fees[k] = norm(round(pct / 100, 10))   # на сайте — в %, в запросе — доля

            cfg = {
                "symbols": symbols,
                "positions": self.side.get(),
                "periods": self._periods(),
                "entries": entries,
                "entry_tfs": self.ent_tf.get(),
                "swing_levels": swing,
                "chart_tfs": self.chart_tf.get(),
                "entry_hold": self._int_list(self.entry_hold, "Вход открыт после сигнала"),
                "defaults": defaults,
                "grid": grid,
                "fees": fees,
                "workers": self._pos_int(self.workers, "Параллельных запросов"),
                "rate_per_min": self._pos_int(self.rate_min, "Прогонов в минуту"),
                "rate_per_hour": self._pos_int(self.rate_hour, "Прогонов в час"),
                "output": self.output.get().strip() or "results.csv",
            }
            os.makedirs(gbt.RESULTS_DIR, exist_ok=True)
            folder = os.path.dirname(gbt.results_path(cfg["output"]))
            if not os.path.isdir(folder):
                raise ValueError("Файл результатов: папки «%s» нет" % folder)
            stops = self._row_values("stop_loss")
            if stops != [0]:
                cfg["stop_loss"] = stops
            if sets:
                cfg["sets"] = sets
            if indicators:
                cfg["indicators"] = indicators
            cfg["_comment"] = "создано gui.py"
            return cfg, None
        except (ValueError, tk.TclError) as e:
            return None, str(e)

    def _gbt(self, fn, cfg):
        """Вызов gbt.build_jobs / count_jobs: sys.exit и прочие ошибки -> ValueError."""
        if not self.data:
            raise ValueError("нет справочника пар — нажмите «Обновить пары»")
        try:
            return fn(cfg, self.data)
        except SystemExit as e:
            raise ValueError(str(e.code or e))
        except Exception as e:      # кривые «Свои условия» и т.п. — показать, а не молча упасть
            raise ValueError("не удалось разобрать конфиг: %s: %s" % (type(e).__name__, e))

    @staticmethod
    def _out_path(cfg):
        return gbt.results_path(cfg["output"])

    def _done_pairs(self, path):
        """gbt.done_pairs с кэшем по времени изменения файла."""
        st = os.stat(path)
        cached = self._done_cache.get(path)
        if cached and cached[:2] == (st.st_mtime, st.st_size):
            return cached[2]
        pairs = gbt.done_pairs(path)
        self._done_cache[path] = (st.st_mtime, st.st_size, pairs)
        return pairs

    def _left(self, cfg, jobs):
        """(осталось прогонов на сервере, досчитать без сервера) — как считает gbt.py при досчёте."""
        path = self._out_path(cfg)
        try:
            pairs = self._done_pairs(path) if os.path.exists(path) else set()
            server, local = gbt.plan(cfg, jobs, path, pairs)
        except (OSError, ValueError, KeyError, UnicodeDecodeError):
            return len(jobs), 0
        return len(server), len(local)

    # ---------- живой счётчик комбинаций ----------

    def _schedule(self, *_):
        if self._recalc_id:
            self.root.after_cancel(self._recalc_id)
        self._recalc_id = self.root.after(250, self._recalc)

    def _recalc(self):
        self._recalc_id = None
        self._json_label()
        for key in PARAM_NAMES + ["stop_loss"]:
            lbl = self.reinvest_count if key == "reinvest" else self.rows[key]["count"]
            try:
                n = len(self._row_values(key))
                lbl.config(text="×%d" % n if n > 1 else "1", fg=ACCENT if n > 1 else DIM)
            except ValueError:
                lbl.config(text="ошибка", fg=MINUS)
        self._left_gen += 1
        cfg, err = self.build_cfg()
        if not err:
            try:
                total = self._gbt(gbt.count_jobs, cfg)
                entries = self._gbt(lambda c, d: gbt.entry_variants(c, d, cfg["positions"][0]), cfg)
                if total == 0 and entries:
                    err = ("0 прогонов: ни у одной выбранной пары нет истории за этот период — "
                           "выберите другие даты или «лет»")
            except ValueError as e:
                err = str(e)
        if err:
            self.err.config(text=err)
            self.total_lbl.config(text="—")
            self.parts_lbl.config(text="")
            self.eta_lbl.config(text="")
            self.run_btn.config(state="disabled" if not self.proc else self.run_btn["state"])
            return
        self.err.config(text="")
        if not self.proc:
            self.run_btn.config(state="normal")
        n_pairs = len(self.data["pairs"]) if cfg["symbols"] == "all" else len(cfg["symbols"])
        n_ent = sum(len(cfg["chart_tfs"]) if e[0] == "none" else 1 for e in entries)
        n_sets = len(cfg.get("sets", []))
        if cfg["grid"]:
            g = 1
            for v in cfg["grid"].values():
                g *= len(v)
            n_sets += g
        n_sets = n_sets or 1
        n_per = len(cfg["periods"])
        n_side = len(cfg["positions"])
        parts = ["%d %s" % (n_pairs, plural(n_pairs, "пара", "пары", "пар"))]
        if n_per > 1:
            parts.append("%d %s" % (n_per, plural(n_per, "период", "периода", "периодов")))
        parts += ["%d %s" % (n_side, plural(n_side, "сторона", "стороны", "сторон")),
                  "%d %s" % (n_ent, plural(n_ent, "вход", "входа", "входов")),
                  "%s %s" % (spaced(n_sets), plural(n_sets, "набор", "набора", "наборов"))]
        self.total_lbl.config(text=spaced(total))
        self.total_word.config(text=plural(total, "прогон", "прогона", "прогонов") + " на сервере")
        n_stop = len(cfg.get("stop_loss", [0]))
        self.parts_lbl.config(text=" × ".join(parts)
                              + ("" if total == n_pairs * n_per * n_side * n_ent * n_sets
                                 else " (не у всех пар есть история за период)")
                              + ("" if n_stop == 1 else
                                 "\n× %d %s стопа = %s %s в таблице — стоп считается по сделкам, "
                                 "без новых прогонов" % (
                                     n_stop, plural(n_stop, "вариант", "варианта", "вариантов"),
                                     spaced(total * n_stop),
                                     plural(total * n_stop, "строка", "строки", "строк"))))
        self._show_eta(cfg, total, None)
        path = self._out_path(cfg)
        if os.path.exists(path) and total <= LEFT_LIMIT:
            gen = self._left_gen
            threading.Thread(target=self._left_worker, args=(gen, cfg, total), daemon=True).start()

    def _left_worker(self, gen, cfg, total):
        try:
            jobs = gbt.build_jobs(cfg, self.data)
            left = self._left(cfg, jobs)
        except (SystemExit, Exception):
            return
        self.q.put(("left", (gen, cfg, total, left)))

    def _show_left(self, gen, cfg, total, left):
        if gen == self._left_gen:
            self._show_eta(cfg, total, left)

    def _show_eta(self, cfg, total, left):
        """left — (осталось на сервере, без сервера) или None, пока не посчитано."""
        n, local = (total, 0) if left is None else left
        rpm, rph = cfg["rate_per_min"], cfg["rate_per_hour"]
        used = gbt.rate_usage()
        eta = gbt.fmt_eta(gbt.eta_minutes(n, rpm, rph, used, cfg["workers"]))
        if n:
            text = "≈ %s (сервер: %d в минуту, %d в час%s)" % (
                eta, rpm, rph, "; за последний час уже %d" % used[1] if used[1] else "")
        else:
            text = "на сервере считать нечего" if local else "считать нечего"
        if local:
            text += "; ещё %s — новые значения стопа по сохранённым сделкам, без сервера" % spaced(local)
        if left is not None and n < total:
            text = "В %s уже посчитано %s, осталось %s — %s" % (
                cfg["output"], spaced(total - n), spaced(n), text)
        else:
            text = "Файл: %s · %s" % (cfg["output"], text)
        self.eta_lbl.config(text=text)

    # ---------- действия ----------

    def start_sweep(self, force=False):
        if self.proc:
            return
        cfg, err = self.build_cfg()
        if err:
            messagebox.showerror("Настройки", err)
            return
        try:
            jobs = self._gbt(gbt.build_jobs, cfg)
        except ValueError as e:
            messagebox.showerror("Настройки", str(e))
            return
        n = len(jobs)
        if n == 0:
            messagebox.showerror("Настройки", "Нулевое число прогонов — проверьте настройки")
            return
        if os.path.exists(self._out_path(cfg)):
            left, local = self._left(cfg, jobs)
            if left == 0 and local == 0:
                messagebox.showinfo("Прогон", "Все %d комбинаций уже посчитаны в %s."
                                    % (n, cfg["output"]))
                return
            extra = (" (+%d — только новый стоп, без сервера)" % local) if local else ""
            if force:
                self.say("Досчёт в %s: осталось %d из %d%s — продолжаю без вопросов."
                         % (cfg["output"], left, n, extra))
            elif not messagebox.askyesno(
                    "Прогон",
                    "Файл %s уже есть: посчитано %d из %d.\n"
                    "Продолжить досчёт оставшихся %d%s?\n(Да — продолжить, Нет — отмена)"
                    % (cfg["output"], n - left, n, left, extra)):
                return
        elif force:
            self.say("Файла %s ещё нет — считаю с начала." % cfg["output"])
        try:
            os.makedirs(gbt.CONFIGS_DIR, exist_ok=True)
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=1)
        except OSError as e:
            messagebox.showerror("Конфиг", "Не записал %s: %s" % (CFG_PATH, e))
            return
        self.run_cmd(["sweep", os.path.relpath(CFG_PATH, HERE).replace("\\", "/")])

    def show_top(self):
        out = gbt.results_path(self.output.get().strip() or "results.csv")
        if not os.path.exists(out):
            self.say("Файла %s ещё нет — сначала запустите прогон." % os.path.basename(out))
            return
        Results(self.root, out)

    def open_csv(self):
        out = gbt.results_path(self.output.get().strip() or "results.csv")
        if os.path.exists(out):
            self._open(out)
        else:
            self.say("Файла %s ещё нет." % os.path.basename(out))

    @staticmethod
    def _open(path):
        if os.path.exists(path):
            try:
                os.startfile(path)
            except OSError as e:
                messagebox.showerror("Открыть", str(e))
        else:
            messagebox.showinfo("Открыть", "Файла нет: %s" % path)

    def save_cfg(self):
        cfg, err = self.build_cfg()
        if err:
            messagebox.showerror("Настройки", err)
            return
        path = filedialog.asksaveasfilename(initialdir=gbt.CONFIGS_DIR, initialfile="sweep_my.json",
                                             defaultextension=".json",
                                             filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=1)
        except OSError as e:
            messagebox.showerror("Конфиг", str(e))
            return
        self.say("Конфиг записан: %s" % path, "ok")

    def load_cfg(self):
        path = filedialog.askopenfilename(initialdir=gbt.CONFIGS_DIR, filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError) as e:
            messagebox.showerror("Конфиг", "Не прочитал: %s" % e)
            return
        try:
            self._apply_cfg(cfg)
        except Exception as e:
            messagebox.showerror("Конфиг", "Не перенёс в форму: %s" % e)
            return
        self.say("Конфиг загружен: %s" % path, "ok")

    def _set_tfs(self, group, val, label):
        known = [v for v, _ in TFS]
        vals = known if val == "all" else [int(v) for v in gbt.as_list(val)]
        bad = [v for v in vals if v not in known]
        if bad:
            self.say("%s: интервалов %s нет на сайте — пропущены" % (label, fmt_vals(bad)), "err")
        group.set([v for v in vals if v in known] or [60])

    def _apply_cfg(self, cfg):
        self._filling = True
        try:
            sym = cfg.get("symbols", ["BTCUSDT"])
            self.all_symbols = sym == "all"
            if not self.all_symbols:
                self.sel_symbols = set(gbt.as_list(sym))

            pos = [p for p in gbt.as_list(cfg.get("positions", ["long"])) if p in ("long", "short")]
            self.side.set(pos or ["long"])

            periods = cfg.get("periods") or [{"years": 4}]
            spec = periods[0]
            if "years" in spec:
                y = int(spec["years"])
                if y not in self.years.btns:
                    self.years.add(y, str(y))
                self.years.set([y])
            else:
                self.years.set([])
                self.date_from.set(spec.get("from", ""))
                self.date_to.set(spec.get("to", ""))
            self.extra_periods.set(json.dumps(periods[1:], ensure_ascii=False) if len(periods) > 1 else "")

            # как в gbt.py: без entries — "none", а если есть indicators — только они
            en = cfg.get("entries", [] if "indicators" in cfg else ["none"])
            names = ["none"] if en == "all" else gbt.as_list(en)
            tpls = [n for n in names if n != "none"]
            modes = (["none"] if "none" in names else []) \
                + (["tpl"] if tpls or en == "all" else []) \
                + (["own"] if cfg.get("indicators") else [])
            # в окне способ входа один: берём последний (свои условия > связки > всегда в рынке)
            mode = (modes or ["none"])[-1]
            if len(modes) > 1:
                titles = {"none": "«Всегда в рынке»", "tpl": "«Готовые связки»", "own": "«Свои условия»"}
                self.say("В конфиге несколько способов входа — в окне один: %s; пропущены %s."
                         % (titles[mode], ", ".join(titles[m] for m in modes[:-1])), "warn")
            self.modes.set([mode])
            if en == "all":       # все связки; до загрузки справочника — выберутся при загрузке
                self.sel_tpls = set(self._tpl_ids()) or None
            elif tpls:
                self.sel_tpls = set(tpls)
            # первое правило, если окну по силам, — в форму условий, остальные — в JSON
            rest = gbt.as_list(cfg.get("indicators") or [])
            groups = None
            if rest and self.data:
                groups = rule_groups(self.data, rest[0], gbt.entry_tfs_of(cfg, self.data))
                if groups is not None:
                    rest = rest[1:]
            self.form.groups = groups or []
            self.form.render()
            self.ind_text.delete("1.0", "end")
            if rest:
                self.ind_text.insert("1.0", "[\n  %s\n]" % ",\n  ".join(     # правило — строка
                    json.dumps(r, ensure_ascii=False) for r in rest))
            self._json_label()

            self._set_tfs(self.ent_tf, cfg.get("entry_tfs", [60]), "Интервал связки")
            self._set_tfs(self.chart_tf, cfg.get("chart_tfs", [60]), "Пауза перезахода")
            self.swing.set(fmt_vals(gbt.as_list(cfg.get("swing_levels", [1]))))
            self.entry_hold.set(fmt_vals(gbt.as_list(cfg.get("entry_hold", 60))))

            defaults, grid = cfg.get("defaults", {}), cfg.get("grid", {})
            grid = dict(grid, stop_loss=gbt.as_list(cfg.get("stop_loss", [0])))
            for key in PARAM_NAMES + ["stop_loss"]:
                if key in grid:
                    vals = gbt.as_list(grid[key])
                elif key in defaults:
                    vals = [defaults[key]]
                else:
                    vals = [0] if key == "reinvest" else [self.rows[key]["default"]]
                vals = [norm(float(v)) for v in vals]
                if key == "reinvest":
                    self.reinvest.set([v for v in vals if v in (0, 25, 50, 100)] or [0])
                    continue
                row = self.rows[key]
                rng = as_range(vals)
                row["range"].set(bool(rng))
                if rng:
                    for k, v in zip(("lo", "hi", "step"), rng):
                        row[k].set("%g" % v)
                else:
                    row["value"].set(fmt_vals(vals))
                self._row_state(key)
            self.presets.set([])

            fees = cfg.get("fees", {})
            self.fee_maker.set("%g" % round(fees.get("fee_maker", 0.0002) * 100, 8))
            self.fee_taker.set("%g" % round(fees.get("fee_taker", 0.0005) * 100, 8))
            self.workers.set(str(cfg.get("workers", 2)))
            self.rate_min.set(str(cfg.get("rate_per_min", 18)))
            self.rate_hour.set(str(cfg.get("rate_per_hour", 145)))
            self.output.set(cfg.get("output", "results.csv"))
            self.sets_text.delete("1.0", "end")
            if cfg.get("sets"):
                self.sets_text.insert("1.0", json.dumps(cfg["sets"], ensure_ascii=False, indent=2))
        finally:
            self._filling = False
        self._update_pickers()
        self._refresh_entry()
        self._show_period()
        self._schedule()

    # ---------- запуск дочерних команд ----------

    def run_cmd(self, args):
        if self.proc:
            self.say("Уже идёт команда — дождитесь окончания или нажмите «Стоп».")
            return
        try:
            os.remove(STOP_FILE)
        except OSError:
            pass
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", GBT_STOP_FILE=STOP_FILE)
        cmd = [console_python(), os.path.join(HERE, "gbt.py")] + list(args)
        self.say("$ " + " ".join(["python", "gbt.py"] + list(args)), "cmd")
        try:
            # без окна консоли: окно программы запущено через pythonw, и дочерний процесс
            # иначе открыл бы своё чёрное окно
            self.proc = subprocess.Popen(
                cmd, cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            self.proc = None
            self.say("Не запустилось: %s" % e, "err")
            return
        self.proc_args = list(args)
        self.stopping = False
        self.run_btn.config(state="disabled")
        self.resume_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.set_status("Идёт: %s" % " ".join(args))
        threading.Thread(target=self._read_output, args=(self.proc,), daemon=True).start()

    def _read_output(self, proc):
        for line in proc.stdout:
            self.q.put(("line", line.rstrip("\n")))
        code = proc.wait()
        self.q.put(("done", code))

    def _on_done(self, code):
        args, self.proc = self.proc_args, None
        try:
            os.remove(STOP_FILE)
        except OSError:
            pass
        self.run_btn.config(state="normal")
        self.resume_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        if self.stopping:
            self.say("Остановлено. «Продолжить досчёт» — продолжит с места.", "ok")
            self.set_status("Остановлено")
        elif code:
            self.say("Завершено с кодом %d" % code, "err")
            self.set_status("Ошибка (код %d) — см. журнал" % code)
        else:
            self.say("Завершено успешно.", "ok")
            self.set_status("Готово")
            if args and args[0] == "sweep":
                self.show_top()       # как «Результаты» на сайте: итоги сразу после прогона
        if args and args[0] in ("login", "me"):
            self.check_session()
        self._schedule()      # обновить «уже посчитано»

    def stop_cmd(self):
        if not self.proc:
            return
        proc = self.proc
        if self.stopping or self.proc_args[0] != "sweep":
            # повторное нажатие или не прогон (вход, топ) — снять сразу
            self.stopping = True
            self.say("Останавливаю…", "err")
            try:
                proc.terminate()
            except OSError:
                pass
            threading.Thread(target=self._kill_soon, args=(proc, 3), daemon=True).start()
            return
        self.stopping = True
        try:
            with open(STOP_FILE, "w", encoding="utf-8") as f:
                f.write("stop")
        except OSError as e:
            self.say("Не создал файл остановки (%s) — снимаю процесс." % e, "err")
            proc.terminate()
            return
        self.say("Останавливаю: дописываю текущие прогоны… (ещё раз «Стоп» — снять сразу)", "err")
        self.set_status("Остановка…")
        threading.Thread(target=self._kill_soon, args=(proc, 30), daemon=True).start()

    @staticmethod
    def _kill_soon(proc, timeout):
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass


def console_python():
    """python.exe рядом с pythonw.exe: у gbt.py должен быть настоящий вывод для журнала."""
    exe = sys.executable
    if os.path.basename(exe).lower() == "pythonw.exe":
        console = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(console):
            return console
    return exe


def main():
    gbt.migrate_layout()   # файлы прежних версий из корня — по папкам configs, results, data
    # gui.bat запускает окно через pythonw — консоли нет, поэтому ошибки показываем в окне
    try:
        root = tk.Tk()
    except tk.TclError as e:
        sys.exit("Не открылось окно: %s\nПроверьте, что Python установлен с tkinter." % e)
    try:
        app = App(root)
    except Exception:
        messagebox.showerror("gridbacktest", "Окно не открылось:\n\n" + traceback.format_exc())
        raise
    root.report_callback_exception = app.on_error
    root.mainloop()


if __name__ == "__main__":
    main()
