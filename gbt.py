#!/usr/bin/env python3
"""Массовый прогон бэктестов gridbacktest.com.

  python gbt.py login                    вход через Telegram, сессия -> data/session.json
  python gbt.py me                       кто вошёл и открыты ли итоги
  python gbt.py info                     пары, связки входа, лимиты параметров
  python gbt.py sweep sweep.json         прогнать все комбинации из конфига (configs/sweep.json)
  python gbt.py sweep sweep.json --dry-run   только посчитать число прогонов
  python gbt.py top results.csv          лучшие результаты (results/results.csv)

Прогон можно прервать (Ctrl+C) и запустить снова той же командой —
уже посчитанные комбинации пропускаются.

Папки: configs/ — настройки, results/ — результаты, data/ — вход, счёт лимита и кэш.
"""

import argparse
import copy
import csv
import hashlib
import itertools
import json
import os
import sys
import threading
import time
import webbrowser
from calendar import monthrange
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import requests

BASE = "https://gridbacktest.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) gbt-sweep/1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
# по папкам, а не в корне рядом с кодом
CONFIGS_DIR = os.path.join(HERE, "configs")    # настройки прогонов (sweep*.json, конфиг окна)
RESULTS_DIR = os.path.join(HERE, "results")    # результаты: *.csv и *.jsonl
DATA_DIR = os.path.join(HERE, "data")          # служебное: вход, счёт лимита, кэш пар
SESSION_FILE = os.path.join(DATA_DIR, "session.json")
DATA_FILES = ("session.json", "rate.json", "pairs_cache.json")


def migrate_layout():
    """Файлы прежних версий из корня — по папкам. Существующие в папках не затираются."""
    for d in (CONFIGS_DIR, RESULTS_DIR, DATA_DIR):
        os.makedirs(d, exist_ok=True)
    for name in os.listdir(HERE):
        src = os.path.join(HERE, name)
        if not os.path.isfile(src):
            continue
        if name in ("rate.json.lock", "rate.json.tmp", ".gbt_stop"):
            try:
                os.remove(src)
            except OSError:
                pass
            continue
        if name in DATA_FILES:
            dest = DATA_DIR
        elif name.endswith(".json"):
            dest = CONFIGS_DIR
        elif name.endswith((".csv", ".jsonl")):
            dest = RESULTS_DIR
        else:
            continue
        if not os.path.exists(os.path.join(dest, name)):
            try:
                os.replace(src, os.path.join(dest, name))
            except OSError:
                pass


def config_path(path):
    """Конфиг: как указан, а если такого файла нет — из папки configs."""
    if os.path.exists(path) or os.path.isabs(path):
        return path
    alt = os.path.join(CONFIGS_DIR, path)
    return alt if os.path.exists(alt) else path


def results_path(name):
    """Файл результатов: относительный путь — внутри папки results."""
    return name if os.path.isabs(name) else os.path.join(RESULTS_DIR, name)

PARAM_KEYS = ["deposit", "leverage", "orders", "price_overlap", "price_factor",
              "volume_factor", "profit", "reinvest"]
# поле by_trades -> колонка CSV (profit/loss переименованы: profit — это ещё и тейк в параметрах)
RESULT_COLS = {"net": "net", "net_pct": "net_pct", "max_drawdown": "max_drawdown",
               "liquidated": "liquidated", "liq_time": "liq_time", "entries": "entries",
               "cycles": "cycles", "profit": "gross_profit", "loss": "gross_loss", "fees": "fees",
               "funding": "funding", "stops": "stops", "orders_filled": "orders_filled",
               "deepest_level": "deepest_level", "open_at_end": "open_at_end"}
# стоп-лосс сервер не считает — оценка по сделкам из ответа (stop_eval); пусто — без стопа
STOP_COLS = ["stop_loss", "stop_hits", "stop_maybe", "stop_cost", "net_stop", "net_stop_pct",
             "net_stop_worst_pct", "liq_avoided", "stop_wiped"]
CSV_COLS = (["key", "symbol", "position", "from", "to", "months", "entry", "entry_tf",
             "swing_level", "chart_tf"] + PARAM_KEYS + ["fee_maker", "fee_taker"]
            + list(RESULT_COLS.values()) + STOP_COLS + ["seconds", "error"])

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # лог в файл — построчно


# ---------- сессия ----------

def new_session():
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s


def save_session(s):
    jar = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
            "expires": c.expires, "secure": c.secure} for c in s.cookies]
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(jar, f, indent=1)


def load_session():
    s = new_session()
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE, encoding="utf-8") as f:
            for c in json.load(f):
                s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"],
                              expires=c.get("expires"), secure=c.get("secure", True))
    return s


def clone_session(src):
    s = new_session()
    s.cookies.update(src.cookies)
    return s


def get_me(s, fresh=False):
    r = s.get(BASE + "/api/me" + ("?fresh=1" if fresh else ""), timeout=30)
    try:
        return r.json() or {}
    except ValueError:
        return {}


def cmd_login(_args):
    s = new_session()
    out = s.post(BASE + "/api/login/start", json={}, timeout=30).json()
    if not out.get("link"):
        sys.exit("Сервер не выдал ссылку входа: %s" % out)
    print("Откройте ссылку в Telegram и нажмите «Старт»:\n\n  %s\n" % out["link"])
    if out.get("backup"):
        print("Если бот не отвечает — запасной:\n  %s\n" % out["backup"])
    try:
        webbrowser.open(out["link"])
    except Exception:
        pass
    print("Ждём подтверждения (до 2 минут)…")
    deadline = time.time() + 125
    while time.time() < deadline:
        time.sleep(2)
        st = s.post(BASE + "/api/login/poll", json={"nonce": out["nonce"]}, timeout=30).json()
        if st.get("status") == "ok":
            break
        if st.get("status") == "expired" or st.get("error"):
            sys.exit("Вход не удался: %s" % (st.get("error") or "ссылка устарела"))
    else:
        sys.exit("Не дождались «Старт» в Telegram — запустите login ещё раз.")
    save_session(s)
    print("Вход выполнен, сессия сохранена в %s" % SESSION_FILE)
    # сервер проверяет реферала/активного бота не сразу — до двух минут
    me = get_me(s, fresh=True)
    for _ in range(12):
        if me.get("bots") != "wait":
            break
        print("Сервер проверяет аккаунт…")
        time.sleep(10)
        me = get_me(s)
    save_session(s)
    print_me(me)


def print_me(me):
    if not me.get("id"):
        print("Не вошли (или сессия истекла). Войдите: пункт 1 в run.bat или python gbt.py login")
        return
    print("Аккаунт: %s (id %s)  статус ботов: %s" % (me.get("name"), me.get("id"), me.get("bots")))
    if me.get("bots") == "active":
        print("Итоги открыты — можно запускать sweep.")
    else:
        print("ВНИМАНИЕ: итоги закрыты (нужен активный бот Pifagor) — сервер будет отдавать нули.")


def cmd_me(_args):
    print_me(get_me(load_session(), fresh=True))


# ---------- справочник сайта ----------

def get_data(s):
    return s.get(BASE + "/api/data", timeout=60).json()


def cmd_info(_args):
    d = get_data(new_session())
    print("Пары (первый месяц — последний, всего месяцев):")
    for p, m in sorted(d["pairs"].items()):
        print("  %-14s %s — %s  (%d)" % (p, m[0], m[-1], len(m)))
    print("\nСвязки входа (entries):")
    for t in d["templates"]:
        print("  %-11s long: %s | short: %s" % (t["id"], t["long"]["name"], t["short"]["name"]))
    print("\nИндикаторы для своих правил (indicators): id, параметры по умолчанию, норма long | short")
    for c in d["catalog"]:
        n = d["norms"].get(c["id"], {})
        side = lambda k: ("%s %s" % (n[k]["op"], n[k].get("value", "")) if k in n else "")
        print("  %-12s %-34s %-40s %s | %s" % (
            c["id"], c["name"], json.dumps(c.get("defaults", {}), ensure_ascii=False),
            side("long"), side("short")))
        if c.get("lines"):
            print("  %-12s line: %s" % ("", ", ".join(c["lines"])))
    lim = d["entry_limits"]
    print("  лимиты: period %s, mult %s, hours %s, shift %s; до %d групп ИЛИ, до %d условий И"
          % (lim["period"], lim["mult"], lim["hours"], lim["shift"], lim["groups"], lim["filters"]))
    print("\nИнтервалы связок (entry_tfs):", d["entry_tfs"])
    print("Интервалы графика (chart_tf):", d["chart_tfs"])
    print("\nЛимиты параметров:")
    for k, v in d["limits"].items():
        print("  %-14s %s" % (k, v))
    print("  reinvest — только 0 / 25 / 50 / 100")


# ---------- построение прогонов ----------

def month_edge(month, end):
    y, m = map(int, month.split("-"))
    return "%04d-%02d-%02d" % (y, m, monthrange(y, m)[1] if end else 1)


def period_for(all_months, spec):
    """(from, to, months) для пары. spec: {"years": N} или {"from": .., "to": ..}."""
    if "years" in spec:
        months = all_months[-min(len(all_months), int(spec["years"]) * 12):]
        if not months:
            return None
        return month_edge(months[0], False), month_edge(months[-1], True), months
    lo = spec.get("from") or month_edge(all_months[0], False)
    hi = spec.get("to") or month_edge(all_months[-1], True)
    months = [m for m in all_months if lo[:7] <= m <= hi[:7]]
    if not months:
        return None
    # не начинаем раньше истории пары и не заканчиваем позже неё
    lo = max(lo, month_edge(months[0], False))
    hi = min(hi, month_edge(months[-1], True))
    return lo, hi, months


def as_list(v):
    return v if isinstance(v, list) else [v]


def entry_tfs_of(cfg, data):
    tfs = cfg.get("entry_tfs", [60])
    return data["entry_tfs"] if tfs == "all" else as_list(tfs)


FLIP_OP = {"lt": "gt", "gt": "lt", "cross_up": "cross_down", "cross_down": "cross_up"}
# параметры индикатора, которые можно перебирать списком (кроме tf/op/value)
IND_KEYS = ["period", "mult", "line", "method", "price", "hours", "shift"]
SHORT_IND = {"drawdown": "runup", "runup": "drawdown"}
BB_FLIP = {"lower": "upper", "upper": "lower"}   # полоса Боллинджера в шорте — зеркальная
LEAF_KEYS = {"ind", "tf", "op", "value", "right", "short_ind", "short_op", "short_value"} | set(IND_KEYS)


def num_text(v):
    return ("%g" % v) if isinstance(v, float) else str(v)


def ind_text(ind, params):
    return "%s(%s)" % (ind, ",".join(num_text(v) for v in params.values())) if params else ind


def ind_meta(ind, data):
    meta = next((c for c in data["catalog"] if c["id"] == ind), None)
    if not meta:
        sys.exit("Неизвестный индикатор: %s (см. python gbt.py info)" % ind)
    return meta


def plain_leaf(spec):
    """Короткая запись {"ind": "bb" | "ma", ...} — цена против полосы / средней:
    -> {"ind": "price", "right": {"ind": "bb", ...}}. Ширина полос (line: width) — число, как есть."""
    if spec["ind"] not in ("bb", "ma") or "right" in spec or "width" in as_list(spec.get("line", [])):
        return spec
    out = {k: v for k, v in spec.items() if k not in IND_KEYS}
    out["right"] = dict({"ind": spec["ind"]}, **{k: spec[k] for k in IND_KEYS if k in spec})
    out["ind"] = "price"
    return out


def check_params(ind, axes, prefix, data):
    """Границы сайта для period / mult / hours / shift (значение по умолчанию — всегда можно)."""
    meta, lim = ind_meta(ind, data), data["entry_limits"]
    for k in ("period", "mult", "hours", "shift"):
        for v in axes.get(prefix + k, []):
            if k in lim and not lim[k][0] <= v <= lim[k][1] and v != meta.get("defaults", {}).get(k):
                sys.exit("%s: %s %s вне границ сайта %s…%s" % (ind, k, num_text(v), lim[k][0], lim[k][1]))


def leaf_variants(spec, side, cfg, data):
    """Одно условие -> [(подпись, [[filter]])] по всем комбинациям его списков.

    Справа — число (value) или другой индикатор (right: {"ind": ..., параметры}).
    value/op задаются для лонга; для шорта берутся short_value/short_op, а если их нет —
    зеркально по норме сайта (RSI ниже 30 -> выше 70, CCI ниже −100 -> выше +100);
    у индикаторов, где норма одна для обеих сторон (ADX, всплеск объёма), — как есть.
    Полоса Боллинджера в шорте — зеркальная (нижняя -> верхняя).
    drawdown в шорте сам становится runup (как у связки swing); иначе — short_ind.
    """
    short = side == "short"
    unknown = set(spec) - LEAF_KEYS
    if unknown:
        sys.exit("Условие %s: неизвестные поля %s" % (spec["ind"], ", ".join(sorted(unknown))))
    spec = plain_leaf(spec)
    ind = spec["ind"]
    if short:   # как у связки swing: в лонг — откат от максимума, в шорт — отскок от минимума
        ind = spec.get("short_ind", SHORT_IND.get(ind, ind))
    meta = ind_meta(ind, data)
    norm = data["norms"].get(ind, {})
    right = spec.get("right")
    if right is None and "value" not in spec and "right" in norm.get("long", {}):
        # цена / средняя / полоса без числа — с чем сравнивает сайт (цена ниже нижней полосы)
        right = {k: v for k, v in norm["long"]["right"].items() if k != "type"}
    if right is not None:
        if not isinstance(right, dict) or "ind" not in right or set(right) - {"ind"} - set(IND_KEYS):
            sys.exit("Условие %s: right должен быть {\"ind\": ..., параметры: %s}"
                     % (ind, ", ".join(IND_KEYS)))
        ind_meta(right["ind"], data)
    same_norm = norm.get("long", {}).get("op") == norm.get("short", {}).get("op")

    axes = {"tf": as_list(spec.get("tf", entry_tfs_of(cfg, data)))}
    for k in IND_KEYS:
        if k in spec:
            axes[k] = as_list(spec[k])
        if right and k in right:
            axes["right." + k] = as_list(right[k])
    for k in ("op", "value"):
        if short and ("short_" + k) in spec:
            axes["short_" + k] = as_list(spec["short_" + k])
        elif k in spec:
            axes[k] = as_list(spec[k])
    check_params(ind, axes, "", data)
    if right:
        check_params(right["ind"], axes, "right.", data)

    out = []
    for combo in itertools.product(*axes.values()):
        a = dict(zip(axes, combo))
        ip = {k: a[k] for k in IND_KEYS if k in a}
        width = ind == "bb" and ip.get("line") == "width"     # ширина полос, %, — одна для обеих сторон
        if ind == "bb" and not width:
            ip.setdefault("line", "lower")
            if short:
                ip["line"] = BB_FLIP.get(ip["line"], ip["line"])
        keep = same_norm or width
        if "short_op" in a:
            op = a["short_op"]
        elif "op" in a:
            op = a["op"] if (not short or keep) else FLIP_OP.get(a["op"], a["op"])
        elif side in norm and not width:
            op = norm[side]["op"]
        else:
            op = "lt" if short and not keep else "gt"
        if right:
            rind = right["ind"]
            rp = {k: a["right." + k] for k in IND_KEYS if "right." + k in a}
            if rind == "bb" and rp.get("line") != "width":
                rp.setdefault("line", "lower")
                if short:
                    rp["line"] = BB_FLIP.get(rp["line"], rp["line"])
            rhs, rtext = dict({"type": "ind", "ind": rind}, **rp), ind_text(rind, rp)
        else:
            if "short_value" in a:
                value = a["short_value"]
            elif "value" in a:
                value = a["value"]
                # уровни цены (unit «цена») не зеркалятся: 50 000 в лонг — 50 000 и в шорт
                if short and not keep and meta.get("unit") != "цена":
                    lo, hi = meta.get("scale", [0, 0])
                    value = round(lo + hi - value, 6)
            elif "value" in norm.get(side, {}) and not width:
                value = norm[side]["value"]
            else:
                sys.exit("Условие %s: укажите value или right (с чем сравнивать)" % ind)
            rhs, rtext = {"type": "const", "value": value}, num_text(value)
        f = {"left": dict({"ind": ind}, **ip), "op": op, "right": rhs, "tf": a["tf"]}
        out.append(("%s %s %s @%s" % (ind_text(ind, ip), op, rtext, a["tf"]), [[f]]))
    return out


def expand_rule(spec, side, cfg, data):
    """Правило входа -> [(подпись, groups)], groups — «ИЛИ» групп, внутри группы «И».

    spec: {"ind": ...} | {"all": [spec, ...]} (И) | {"any": [spec, ...]} (ИЛИ)
    """
    if "ind" in spec:
        return leaf_variants(spec, side, cfg, data)
    key = "all" if "all" in spec else "any" if "any" in spec else None
    if not key:
        sys.exit("Правило входа должно содержать ind, all или any: %s" % spec)
    parts = [expand_rule(s, side, cfg, data) for s in spec[key]]
    out = []
    for combo in itertools.product(*parts):
        if key == "all":       # (A1|A2) & (B1|B2) = A1&B1 | A1&B2 | ...
            groups = [[]]
            for _, g in combo:
                groups = [x + y for x in groups for y in g]
            label = " & ".join(("(%s)" % l) if " | " in l else l for l, _ in combo)
        else:
            groups = [g for _, gs in combo for g in gs]
            label = " | ".join(l for l, _ in combo)
        out.append((label, groups))
    return out


def entry_variants(cfg, data, position):
    """Список (подпись, tf, swing_level, entry) для стороны сделки."""
    names = cfg.get("entries", [] if "indicators" in cfg else ["none"])
    tpls = {t["id"]: t for t in data["templates"]}
    if names == "all":
        names = ["none"] + list(tpls)
    tfs = entry_tfs_of(cfg, data)
    holds = as_list(cfg.get("entry_hold", 60))
    lim = data["entry_limits"]
    out = []
    for name in names:
        if name == "none":
            out.append(("none", None, None, {"kind": "none"}))
            continue
        if name not in tpls:
            sys.exit("Неизвестная связка входа: %s (см. python gbt.py info)" % name)
        levels = as_list(cfg.get("swing_levels", [1])) if name == "swing" else [None]
        for tf, lvl, hold in itertools.product(tfs, levels, holds):
            f = copy.deepcopy(tpls[name][position]["filter"])
            f["tf"] = tf
            f["tpl"] = name
            if lvl is not None:
                f["right"]["value"] = lvl
            out.append((name, tf, lvl, {"kind": "rules", "hold": hold,
                                        "groups": [{"filters": [f]}]}))
    for spec in cfg.get("indicators", []):
        for label, groups in expand_rule(spec, position, cfg, data):
            if len(groups) > lim["groups"] or any(len(g) > lim["filters"] for g in groups):
                sys.exit("Правило «%s» больше лимита сайта: до %d групп «ИЛИ» и до %d условий «И»"
                         % (label, lim["groups"], lim["filters"]))
            for hold in holds:
                out.append((label, None, None, {"kind": "rules", "hold": hold,
                                                "groups": [{"filters": g} for g in groups]}))
    return out


def param_sets(cfg):
    """Комбинации параметров сетки: декартово произведение grid + явные sets."""
    grid = cfg.get("grid", {})
    sets = []
    if grid:
        keys = list(grid)
        for vals in itertools.product(*[v if isinstance(v, list) else [v] for v in grid.values()]):
            sets.append(dict(zip(keys, vals)))
    sets += cfg.get("sets", [])
    base = {"deposit": 1000, "leverage": 10, "orders": 10, "price_overlap": 25,
            "price_factor": 1.6, "volume_factor": 1.2, "profit": 1, "reinvest": 0}
    base.update(cfg.get("defaults", {}))
    return [dict(base, **s) for s in sets] or [base]


def check_limits(p, limits):
    for k, v in p.items():
        lim = limits.get(k)
        if lim and not (lim[0] <= v <= lim[1]):
            return "%s=%s вне [%s, %s]" % (k, v, lim[0], lim[1])
    if p.get("reinvest") not in (0, 25, 50, 100):
        return "reinvest должен быть 0/25/50/100"
    return None


def build_jobs(cfg, data):
    symbols = cfg.get("symbols", ["BTCUSDT"])
    if symbols == "all":
        symbols = sorted(data["pairs"])
    positions = cfg.get("positions", ["long"])
    periods = cfg.get("periods", [{"years": 4}])
    chart_tfs = cfg.get("chart_tfs", [60])
    chart_tfs = data["chart_tfs"] if chart_tfs == "all" else as_list(chart_tfs)
    fees = cfg.get("fees", {"fee_maker": 0.0002, "fee_taker": 0.0005})
    psets = param_sets(cfg)
    for p in psets:
        bad = check_limits(dict(p, **fees), data["limits"])
        if bad:
            sys.exit("Недопустимый набор параметров %s: %s" % (p, bad))

    jobs = []
    for sym in symbols:
        if sym not in data["pairs"]:
            sys.exit("Нет такой пары: %s" % sym)
        for spec in periods:
            per = period_for(data["pairs"][sym], spec)
            if not per:
                continue
            lo, hi, months = per
            for pos in positions:
                for ename, etf, lvl, entry in entry_variants(cfg, data, pos):
                    # интервал графика влияет на счёт только при входе без условий
                    for ctf in (chart_tfs if ename == "none" else [chart_tfs[0]]):
                        for p in psets:
                            params = dict(p, position=pos, **fees)
                            req = {"symbol": sym, "months": months, "from": lo, "to": hi,
                                   "params": params, "entry": entry, "chart_tf": ctf}
                            key = hashlib.sha1(json.dumps(req, sort_keys=True).encode()).hexdigest()[:16]
                            row = {"key": key, "symbol": sym, "position": pos, "from": lo, "to": hi,
                                   "months": len(months), "entry": ename, "entry_tf": etf or "",
                                   "swing_level": "" if lvl is None else lvl, "chart_tf": ctf}
                            row.update({k: params[k] for k in PARAM_KEYS})
                            row.update(fees)
                            jobs.append((key, req, row))
    return jobs


def count_jobs(cfg, data):
    """Сколько прогонов даст build_jobs — без сборки запросов (быстро, для счётчика в GUI)."""
    symbols = cfg.get("symbols", ["BTCUSDT"])
    if symbols == "all":
        symbols = sorted(data["pairs"])
    chart_tfs = cfg.get("chart_tfs", [60])
    chart_tfs = data["chart_tfs"] if chart_tfs == "all" else as_list(chart_tfs)
    grid = cfg.get("grid", {})
    n_sets = len(cfg.get("sets", []))
    if grid:
        n = 1
        for v in grid.values():
            n *= len(as_list(v))
        n_sets += n
    n_sets = n_sets or 1
    per_pair = 0
    for pos in cfg.get("positions", ["long"]):
        per_pair += sum(len(chart_tfs) if e[0] == "none" else 1
                        for e in entry_variants(cfg, data, pos))
    total = 0
    for sym in symbols:
        if sym not in data["pairs"]:
            sys.exit("Нет такой пары: %s" % sym)
        for spec in cfg.get("periods", [{"years": 4}]):
            if period_for(data["pairs"][sym], spec):
                total += per_pair * n_sets
    return total


# ---------- стоп-лосс (оценка по сделкам из ответа сервера) ----------
#
# Сервер стоп не принимает (09.10.2026: в /api/run нет такого параметра), но в ответе есть все
# сделки: цена входа, цены доборов, закрытие. Стоп бота считается от средней цены позиции и
# переносится при каждом доборе, поэтому:
#   * добор k ниже стопа после добора k−1 (в лонге) — цена прошла через стоп раньше: стоп точно;
#   * после последнего добора точной ленты нет — смотрим свечи графика; если до стопа дошло
#     только в свече закрытия (или открытия без доборов), порядок внутри свечи неизвестен —
#     это «возможный» стоп, отдельным счётчиком.
# Сделок, которые бот открыл бы после стопа, сервер не считал — итог со стопом это оценка.

def stop_list(cfg):
    """Значения стопа из конфига, % от средней цены; 0 — без стопа."""
    return [norm_num(v) for v in as_list(cfg.get("stop_loss", [0]))] or [0]


def norm_num(v):
    v = float(v)
    return int(v) if v.is_integer() else v


def stop_str(v):
    """Значение стопа в колонке stop_loss: '' — без стопа."""
    return "" if v in (None, "", 0, "0") or float(v) == 0 else "%g" % float(v)


def grid_sizes(p):
    """Объёмы ордеров сетки в USDT: растут в volume_factor раз (совпадает со средней сервера)."""
    w = [p["volume_factor"] ** i for i in range(int(p["orders"]))]
    total = p["deposit"] * p["leverage"]
    return [total * x / sum(w) for x in w]


def stop_data(out):
    """Сделки ответа в сжатом виде для stop_eval:
    [вход, доборов, закрытие (только у ликвидации), вид|None, итог $|None, экстремум точно,
     экстремум с сомнением, время закрытия|None]. Доборы — числом: их цены — уровни сетки
    (grid_offsets), так .jsonl в разы меньше; старый формат со списком цен тоже читается.
    Экстремум — минимум (лонг) или максимум (шорт) цены после последнего добора до закрытия."""
    long = out.get("params", {}).get("position", "long") == "long"
    candles = out.get("candles") or []
    step = candles[1][0] - candles[0][0] if len(candles) > 1 else 0
    end = (out.get("span") or [0, 0])[1] / 1000
    pick = min if long else max
    res = []
    for d in out.get("deals") or []:
        t_open, p_open, t_close, p_close, kind, pnl, adds = d[:7]
        t_last = adds[-1][0] if adds else t_open
        t_end = t_close or end
        sure, maybe = [], []
        for c in candles:
            if c[0] + step <= t_last or c[0] >= t_end:
                continue
            v = c[3] if long else c[2]
            in_close = t_close is not None and c[0] <= t_close < c[0] + step
            in_open = not adds and c[0] <= t_open < c[0] + step
            (maybe if in_close or in_open else sure).append(v)
        res.append([p_open, len(adds), p_close if kind == "liq" else None, kind, pnl,
                    sig(pick(sure)) if sure else None,
                    sig(pick(sure + maybe)) if sure or maybe else None, t_close])
    return res


def grid_offsets(p):
    """Уровни сетки, % от цены входа (как gridShape на сайте; совпадает с grid.levels сервера)."""
    n, ov, f = max(1, round(p["orders"])), p["price_overlap"], p["price_factor"]
    if n < 2:
        return [0]
    if abs(f - 1) < 1e-12:
        gaps = [ov / (n - 1)] * (n - 1)
    else:
        first = ov * (f - 1) / (f ** (n - 1) - 1)
        gaps = [first * f ** i for i in range(n - 1)]
    offs = [0]
    for g in gaps:
        offs.append(offs[-1] + g)
    return offs


def sig(v):
    return float("%.7g" % v)


def stop_eval(sd, p, stop_pct):
    """Что было бы со стопом stop_pct % от средней. Два счёта: «точно» — только стопы, в которых
    сомнений нет; «хуже» — ещё и возможные (до стопа дошло только внутри свечи закрытия).

    -> dict(hits, maybe, cost, delta, delta_worst, wiped, wiped_worst, liq_avoided).
    delta — на сколько $ изменился бы итог: минус прибыль остановленных кругов, минус потери по
    стопу. Счёт ведётся по сделкам по порядку: если он дошёл до нуля — депозит закончился (wiped)."""
    long = p.get("position", "long") == "long"
    s = float(stop_pct) / 100
    sizes = grid_sizes(p)
    offs = grid_offsets(p)
    fm, ft = p.get("fee_maker", 0.0002), p.get("fee_taker", 0.0005)
    dep = p["deposit"]
    hits = maybe = 0
    cost = 0.0
    liq_avoided = False
    acc = {"sure": [0.0, dep, None], "worst": [0.0, dep, None]}   # [delta, счёт, когда кончился]

    def crossed(price, stop):
        return price <= stop if long else price >= stop

    def book(name, result, pnl, t):
        a = acc[name]
        if a[2]:
            return
        a[0] += result - (pnl or 0)
        a[1] += result
        if a[1] <= 0:
            a[2] = fmt_time(t) if t else "да"

    for d in sd:
        if acc["sure"][2]:
            break                       # депозит кончился — дальше бот не торговал бы
        p_open, adds, p_close, kind, pnl, ext_sure, ext_any = d[:7]
        t = d[7] if len(d) > 7 else None
        if isinstance(adds, int):       # число доборов -> их цены по уровням сетки
            adds = [p_open * (1 - offs[i] / 100) if long else p_open * (1 + offs[i] / 100)
                    for i in range(1, min(adds, len(offs) - 1) + 1)]
        q = c = 0.0
        stop = None
        hit = False
        for i, pr in enumerate([p_open] + adds):
            if i and crossed(pr, stop):
                hit = True              # до этого добора цена прошла через стоп
                break
            q += sizes[min(i, len(sizes) - 1)]
            c += sizes[min(i, len(sizes) - 1)] / pr
            avg = q / c
            stop = avg * (1 - s) if long else avg * (1 + s)
        possible = False
        if not hit:
            k = len(adds) + 1           # следующий, не исполненный уровень сетки
            nxt = None
            if k < len(offs):
                nxt = p_open * (1 - offs[k] / 100) if long else p_open * (1 + offs[k] / 100)
            ext = p_close if kind == "liq" else ext_sure
            if ext is not None and crossed(ext, stop):
                hit = True
            elif nxt is not None and kind != "liq" and crossed(stop, nxt):
                pass                    # до следующего уровня цена не дошла, а стоп за ним — точно нет
            elif ext_any is not None and crossed(ext_any, stop):
                possible = True
        if hit or possible:
            loss = (q - c * stop) if long else (c * stop - q)
            result = -(loss + sizes[0] * ft + (q - sizes[0]) * fm + c * stop * ft)
        if hit:
            hits += 1
            cost -= result
            book("sure", result, pnl, t)
            book("worst", result, pnl, t)
            liq_avoided = liq_avoided or kind == "liq"
        else:
            book("sure", pnl or 0, pnl, t)
            if possible:
                maybe += 1
                book("worst", result, pnl, t)
            else:
                book("worst", pnl or 0, pnl, t)
    return {"hits": hits, "maybe": maybe, "cost": cost, "liq_avoided": liq_avoided,
            "delta": acc["sure"][0], "wiped": acc["sure"][2],
            "delta_worst": acc["worst"][0], "wiped_worst": acc["worst"][2]}


def result_rows(row, out, stops):
    """Строки CSV для одного ответа сервера: по одной на каждое значение стопа."""
    row = dict(row)
    bt = out.get("by_trades") or {}
    row.update({col: bt.get(k) for k, col in RESULT_COLS.items()})
    row["liq_time"] = fmt_time(row.get("liq_time"))
    row["seconds"] = out.get("seconds")
    if out.get("locked"):     # нули гостя — после входа пересчитаются
        row["error"] = "locked"
    rows = []
    sd = out.get("stopdata")
    if sd is None and out.get("deals") is not None:
        sd = stop_data(out)
    for st in stops:
        r = dict(row, stop_loss=stop_str(st))
        if r["stop_loss"] and sd is not None and not r.get("error"):
            e = stop_eval(sd, dict(out.get("params") or {}, position=row["position"]), st)
            dep = row["deposit"]
            net = -dep if e["wiped"] else (bt.get("net") or 0) + e["delta"]
            worst = -dep if e["wiped_worst"] else (bt.get("net") or 0) + e["delta_worst"]
            r.update(stop_hits=e["hits"], stop_maybe=e["maybe"], stop_cost=round(e["cost"], 2),
                     net_stop=round(net, 2), net_stop_pct=round(net / dep * 100, 2),
                     net_stop_worst_pct=round(worst / dep * 100, 2),
                     liq_avoided=e["liq_avoided"], stop_wiped=e["wiped"] or "")
        rows.append(r)
    return rows


def effective(r):
    """(итог %, исключить из лучших?) строки CSV с учётом оценки стопа (по точным стопам).

    Исключаются: ликвидация; депозит, кончившийся на стопах (stop_wiped); и неполные — стоп
    сработал бы раньше ликвидации (liq_avoided), но после ликвидации сервер не считал, так что
    итог известен только до её даты, а не за весь период."""
    if r.get("stop_loss") and r.get("net_stop_pct") not in (None, ""):
        return float(r["net_stop_pct"]), (r.get("liquidated") == "True"
                                          or r.get("stop_wiped") not in (None, "", "False"))
    return float(r["net_pct"]), r.get("liquidated") == "True"


def partial(r):
    """Строка со стопом, итог которой известен только до даты ликвидации (см. effective)."""
    return bool(r.get("stop_loss")) and r.get("liq_avoided") == "True"


# ---------- прогон ----------

class Stop(Exception):
    pass


ALLOW_LOCKED = False   # --force: считать и с закрытыми итогами (для проверки конфига)
STOP = threading.Event()   # остановка прогона: Ctrl+C или кнопка «Стоп» в GUI (файл GBT_STOP_FILE)


RATE_FILE = os.path.join(DATA_DIR, "rate.json")


class FileLock:
    """Блокировка между процессами (окно, командная строка, второе окно) — файл RATE_FILE.lock."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.f = open(self.path, "a+b")
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
        finally:
            self.f.close()


def read_rate():
    """(время прогонов за последний час, пауза до) из RATE_FILE; старый формат — просто список."""
    try:
        with open(RATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return [], 0.0
    if isinstance(d, list):
        return sorted(d), 0.0
    return sorted(d.get("stamps", [])), float(d.get("block_until", 0))


def write_rate(stamps, block_until):
    """Через временный файл — обрыв посреди записи не портит rate.json."""
    tmp = RATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"stamps": stamps, "block_until": block_until}, f)
    for _ in range(20):
        try:
            os.replace(tmp, RATE_FILE)
            return
        except PermissionError:       # Windows: файл на миг открыт другим процессом
            time.sleep(0.05)


class RateLimiter:
    """Лимиты сервера на прогоны — общий счёт на все потоки и все процессы.

    Замер 07.10.2026: не больше 20 прогонов в минуту (21-й — 429 «слишком часто,
    подождите минуту») и не больше 150 за последний час (429 «лимит 150»,
    с Retry-After). Лимит — на аккаунт, поэтому счёт общий: перед каждым прогоном
    процесс под блокировкой файла читает rate.json, проверяет лимит и дописывает себя.
    Два окна, окно и командная строка — все видят один счёт; перезапуск его не обнуляет.
    """

    MINUTE, HOUR = 60, 3600

    def __init__(self, per_min=18, per_hour=145):
        self.per_min, self.per_hour = per_min, per_hour
        self.said_until = 0.0
        self.lock = threading.Lock()

    def _pause(self, stamps, block_until, now):
        pause = block_until - now
        for span, limit in ((self.MINUTE, self.per_min), (self.HOUR, self.per_hour)):
            inside = [t for t in stamps if now - t < span]
            if len(inside) >= limit:
                pause = max(pause, inside[len(inside) - limit] + span - now + 0.5)
        return pause

    def acquire(self):
        while True:
            if STOP.is_set():
                raise Stop("остановлено по запросу")
            with self.lock, FileLock(RATE_FILE + ".lock"):
                now = time.time()
                stamps, block_until = read_rate()
                stamps = [t for t in stamps if now - t < self.HOUR]
                pause = self._pause(stamps, block_until, now)
                if pause <= 0:
                    stamps.append(now)
                    write_rate(stamps, block_until)
                    return
            if pause > 90 and now + pause > self.said_until + 60:
                self.said_until = now + pause
                print("  …часовой лимит сервера (%d прогонов/ч): ждём %d мин, до %s"
                      % (self.per_hour, pause // 60 + 1,
                         time.strftime("%H:%M", time.localtime(now + pause))))
            STOP.wait(min(pause, 5))

    def penalize(self, seconds=None):
        """Сервер ответил 429 — пауза для всех процессов."""
        seconds = max(61, seconds or 0)
        with self.lock, FileLock(RATE_FILE + ".lock"):
            stamps, block_until = read_rate()
            now = time.time()
            if block_until <= now:
                print("  …сервер просит подождать %d мин (лимит), ждём до %s"
                      % ((seconds + 59) // 60, time.strftime("%H:%M", time.localtime(now + seconds))))
            write_rate([t for t in stamps if now - t < self.HOUR], max(block_until, now + seconds))


LIMITER = RateLimiter()   # у сервера 20/мин и 150/ч — держим с запасом
_local = threading.local()


def run_one(base_session, req, retries=5):
    if not hasattr(_local, "s"):
        _local.s = clone_session(base_session)
    s = _local.s
    delay, fails, waits = 3, 0, 0
    while fails < retries and waits < 30:
        LIMITER.acquire()
        try:
            r = s.post(BASE + "/api/run", json=req, timeout=(15, 120))  # прогон идёт ~1 с; зависший запрос — повтор
        except requests.RequestException as e:
            err = "сеть: %s" % e
        else:
            if r.status_code == 401:
                raise Stop("Сессия истекла — войдите заново (пункт 1 в run.bat)")
            if r.status_code == 200:
                out = r.json()
                if out.get("locked") and not ALLOW_LOCKED:
                    raise Stop("Сервер отдаёт итоги закрытыми (locked) — нужен вход с активным ботом")
                return out, None
            try:
                err = r.json().get("error") or "HTTP %d" % r.status_code
            except ValueError:
                err = "HTTP %d" % r.status_code
            if r.status_code == 429:         # лимит частоты — ждём и повторяем, это не ошибка
                ra = r.headers.get("Retry-After", "")
                LIMITER.penalize(int(ra) + 2 if ra.isdigit() else None)
                waits += 1
                continue
            if r.status_code < 500:         # ошибка в параметрах — повтор не поможет
                return None, "HTTP %d: %s" % (r.status_code, err)
        fails += 1
        if STOP.wait(delay):
            raise Stop("остановлено по запросу")
        delay = min(delay * 2, 60)
    return None, err


def done_pairs(path):
    """{(key, стоп)} посчитанных строк CSV; стоп — как в колонке stop_loss ('' — без стопа)."""
    pairs = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                # пустая ошибка или 4xx (неверные параметры) — посчитано окончательно;
                # сеть, 5xx и locked — пересчитать при следующем запуске
                if not row.get("error") or row["error"].startswith("HTTP 4"):
                    pairs.add((row["key"], stop_str(row.get("stop_loss"))))
    return pairs


def done_keys(path):
    return {k for k, _ in done_pairs(path)}


def plan(cfg, jobs, out_csv, pairs=None):
    """Что считать: (на сервере [(job, стопы)], без сервера [(job, стопы)]).

    Без сервера — если прогон уже есть в CSV, не хватает только строк с другими стопами, а в
    .jsonl сохранены его сделки: стоп пересчитывается по ним, лимит сервера не тратится."""
    stops = stop_list(cfg)
    pairs = done_pairs(out_csv) if pairs is None else pairs
    keys = {k for k, _ in pairs}
    server, local = [], []
    for job in jobs:
        need = [st for st in stops if (job[0], stop_str(st)) not in pairs]
        if need:
            (local if job[0] in keys else server).append((job, need))
    if local:
        have = stored_results(os.path.splitext(out_csv)[0] + ".jsonl", {j[0] for j, _ in local})
        server += [(j, need) for j, need in local if j[0] not in have]
        local = [(j, need, have[j[0]]) for j, need in local if j[0] in have]
    return server, local


def stored_results(path, keys):
    """{key: result} из .jsonl для нужных ключей — только ответы со сделками (stopdata)."""
    found = {}
    if not keys or not os.path.exists(path):
        return found
    with open(path, encoding="utf-8") as f:
        for line in f:
            if '"stopdata"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("key") in keys and d.get("result", {}).get("stopdata") is not None:
                found[d["key"]] = d["result"]
    return found


def upgrade_csv(path):
    """Старый CSV без колонок стопа — переписать с новой шапкой, иначе дописанные строки съедут."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        if rd.fieldnames == CSV_COLS:
            return
        rows = list(rd)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def cmd_sweep(args):
    args.config = config_path(args.config)
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    s = load_session()
    data = get_data(s)
    jobs = build_jobs(cfg, data)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    out_csv = results_path(args.out or cfg.get("output", "results.csv"))
    out_jsonl = os.path.splitext(out_csv)[0] + ".jsonl"
    if not os.path.isdir(os.path.dirname(os.path.abspath(out_csv))):
        sys.exit("Папки для файла результатов нет: %s" % os.path.dirname(os.path.abspath(out_csv)))
    stops = stop_list(cfg)
    todo, local = plan(cfg, jobs, out_csv)
    LIMITER.per_min = max(1, int(cfg.get("rate_per_min", 18)))
    LIMITER.per_hour = max(1, int(cfg.get("rate_per_hour", 145)))
    n_done = len(jobs) - len(todo)       # на сервере; local — только новые значения стопа
    used = rate_usage()
    workers = max(1, int(args.workers or cfg.get("workers", 2)))
    print("Комбинаций всего: %d, уже посчитано: %d, осталось: %d (примерно %s: сервер даёт %d прогонов "
          "в минуту и %d в час%s)"
          % (len(jobs), n_done, len(todo),
             fmt_eta(eta_minutes(len(todo), LIMITER.per_min, LIMITER.per_hour, used, workers)),
             LIMITER.per_min, LIMITER.per_hour,
             ", за последний час уже %d" % used[1] if used[1] else ""))
    if any(stops):
        print("Стоп-лосс: %s %% от средней цены — %d %s на каждый прогон, сервер их не считает "
              "(оценка по сделкам, новых прогонов не нужно)"
              % (", ".join(stop_str(v) or "без стопа" for v in stops), len(stops),
                 "вариант" if len(stops) == 1 else "варианта" if len(stops) < 5 else "вариантов"))
    if local:
        print("Без сервера (новые значения стопа по сохранённым сделкам): %d прогонов" % len(local))
    if args.dry_run:
        names = sorted({row["entry"] for _, _, row in jobs})
        print("Вариантов входа: %d" % len(names))
        for nm in names[:40]:
            print("  " + nm)
        if len(names) > 40:
            print("  … ещё %d" % (len(names) - 40))
    if args.dry_run or not (todo or local):
        return

    upgrade_csv(out_csv)
    new_file = not os.path.exists(out_csv)
    fcsv = open(out_csv, "a", encoding="utf-8-sig" if new_file else "utf-8", newline="")
    w = csv.DictWriter(fcsv, fieldnames=CSV_COLS, extrasaction="ignore")
    if new_file:
        w.writeheader()
    for (key, req, row), need, out in local:
        w.writerows(result_rows(row, out, need))
    fcsv.flush()
    if not todo:
        fcsv.close()
        print("Готово: пересчитан стоп для %d прогонов. Результаты: %s" % (len(local), out_csv))
        show_top(out_csv, 15)
        return

    me = get_me(s, fresh=True)
    if me.get("bots") != "active":
        print_me(me)
        fcsv.close()
        if not args.force:
            sys.exit("Остановлено. (--force — считать всё равно, итоги будут нулями)")
        global ALLOW_LOCKED
        ALLOW_LOCKED = True
        fcsv = open(out_csv, "a", encoding="utf-8", newline="")
        w = csv.DictWriter(fcsv, fieldnames=CSV_COLS, extrasaction="ignore")

    workers = max(1, int(args.workers or cfg.get("workers", 2)))
    fjs = open(out_jsonl, "a", encoding="utf-8")

    started, n, errors = time.time(), 0, 0
    pending = {}
    it = iter(todo)

    def submit(pool):
        item = None if STOP.is_set() else next(it, None)
        if item is None:
            return False
        pending[pool.submit(run_one, s, item[0][1])] = item
        return True

    stop_file = os.environ.get("GBT_STOP_FILE")   # GUI создаёт этот файл по кнопке «Стоп»
    if stop_file:
        def watch_stop():
            while not STOP.wait(0.5):
                if os.path.exists(stop_file):
                    STOP.set()
        threading.Thread(target=watch_stop, daemon=True).start()

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        for _ in range(workers * 2):
            if not submit(pool):
                break
        while pending:
            # с таймаутом: иначе на Windows Ctrl+C не прерывает ожидание
            finished, _ = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            # сначала записать готовые итоги, потом — исключения (Stop), чтобы не терять посчитанное
            for fut in sorted(finished, key=lambda f: f.exception() is not None):
                (key, req, row), need = pending.pop(fut)
                out, err = fut.result()        # Stop пробрасывается наружу
                if out:
                    out = dict(out)
                    out["stopdata"] = stop_data(out)   # сделки — чтобы менять стоп без сервера
                    rows = result_rows(row, out, need)
                    slim = {k: v for k, v in out.items()
                            if k not in ("candles", "marks", "deals", "bot")}
                    fjs.write(json.dumps({"key": key, "request": req, "result": slim},
                                         ensure_ascii=False) + "\n")
                else:
                    rows = [dict(row, stop_loss=stop_str(st), error=err) for st in need]
                    errors += 1
                w.writerows(rows)
                fcsv.flush()
                fjs.flush()
                n += 1
                el = time.time() - started
                eta = el / n * (len(todo) - n)
                row = rows[0]
                # итог — в начале строки: в узком журнале GUI он виден без прокрутки
                tag = ("ОШИБКА " + err) if err else "итог %+.2f%% просадка %s%%%s" % (
                    row.get("net_pct") or 0, row.get("max_drawdown"),
                    " ЛИКВИДАЦИЯ %s" % (row.get("liq_time") or "") if row.get("liquidated") else "")
                for r in rows:
                    if r.get("stop_hits") not in (None, ""):
                        tag += " | стоп %s%%: %s%s, итог %+.2f%%%s" % (
                            r["stop_loss"], r["stop_hits"],
                            " (+%s?)" % r["stop_maybe"] if r["stop_maybe"] else "",
                            r["net_stop_pct"],
                            " (неполный: стоп до ликвидации, дальше сервер не считал)"
                            if r["liq_avoided"] else "")
                print("[%d/%d] %s | %s %s ov%s ord%s pf%s vf%s tp%s | %s | ETA %dм"
                      % (n, len(todo), tag, row["symbol"], row["position"], row["price_overlap"],
                         row["orders"], row["price_factor"], row["volume_factor"], row["profit"],
                         entry_name(row), eta // 60))
                submit(pool)
            if STOP.is_set():
                raise Stop("остановлено по запросу")
    except Stop as e:
        print("\nОстановлено: %s. Запустите ту же команду снова — продолжит с места остановки." % e)
    except KeyboardInterrupt:
        print("\nПрервано. Запустите ту же команду снова — продолжит с места остановки.")
    finally:
        STOP.set()   # потоки, ждущие лимит, сразу выходят — иначе процесс не завершится
        pool.shutdown(wait=False, cancel_futures=True)
        fcsv.close()
        fjs.close()
    print("Готово: %d прогонов, ошибок %d. Результаты: %s" % (n, errors, out_csv))
    if n or local:
        show_top(out_csv, 15)


# ---------- отчёт ----------

def entry_name(row):
    """Короткая подпись входа для экрана."""
    if row["entry"] == "none":
        return "всегда в рынке (пауза %s)" % row["chart_tf"]
    if row.get("entry_tf"):                         # готовая связка сайта
        lvl = row.get("swing_level")
        return "%s%s @%s" % (row["entry"], " %s%%" % lvl if lvl not in (None, "") else "",
                             row["entry_tf"])
    return row["entry"]                            # своё правило: tf уже в подписи


RUN_SECONDS = 2.0   # сколько в среднем идёт один прогон (замер 09.10.2026: 0.2–3 с, по 2 потока)


def rate_usage():
    """(прогонов за последнюю минуту, за последний час) — по rate.json, общий на все запуски."""
    stamps, _ = read_rate()
    now = time.time()
    return sum(now - t < 60 for t in stamps), sum(now - t < 3600 for t in stamps)


def eta_minutes(n, per_min, per_hour, used=None, workers=2):
    """Сколько минут займут n прогонов при лимитах сервера.

    Лимиты скользящие: до per_hour прогонов за час идут подряд, по per_min в минуту; дальше
    каждая следующая порция ждёт, пока первые прогоны часа «выйдут» из окна. used — уже
    израсходовано (за минуту, за час), см. rate_usage; плюс время самих прогонов.
    """
    if n <= 0:
        return 0
    used_min, used_hour = used or (0, 0)
    now_ok = max(0, per_hour - used_hour)          # можно ещё в этом часе
    if n <= now_ok:
        mins = (n + min(used_min, per_min) - 1) // per_min
    else:
        rest = n - now_ok
        mins = ((rest - 1) // per_hour + 1) * 60 + ((rest - 1) % per_hour) // per_min
    return mins + n * RUN_SECONDS / max(1, workers) / 60


def fmt_eta(mins):
    if mins < 1:
        return "меньше минуты"
    if mins < 120:
        return "%d мин" % mins
    if mins < 48 * 60:
        return "%.1f ч" % (mins / 60)
    return "%.1f сут" % (mins / 60 / 24)


def fmt_time(v):
    """Время сервера (мс или с от 1970) -> «2023-10-12 04:46» UTC; остальное — как есть."""
    try:
        t = float(v)
    except (TypeError, ValueError):
        return v
    if t < 1e9:
        return v
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(t / 1000 if t > 1e11 else t))


def show_top(path, limit, allow_liq=False, by="net_pct"):
    """Лучшие строки CSV. Итог и ликвидация — с учётом оценки стопа (effective)."""
    rows, liq = [], 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("error") or r.get(by) in (None, ""):
                continue
            r["_net"], r["_liq"] = effective(r)
            if r["_liq"]:
                liq += 1
                if not allow_liq:
                    continue
            rows.append(r)
    if not rows and liq:
        print("\nВсе %d вариантов с ликвидацией (или неполные — стоп до ликвидации) — показываю их:"
              % liq)
        return show_top(path, limit, True, by)
    has_stop = any(r.get("stop_loss") for r in rows)
    sort_key = {"net_pct": lambda r: r["_net"]}.get(by, lambda r: float(r[by]))
    rows.sort(key=sort_key, reverse=by != "max_drawdown")
    print("\nТоп-%d по %s%s%s:" % (limit, by, "" if allow_liq else " (без ликвидаций)",
                                  " — итог со стопом: оценка по сделкам" if has_stop else ""))
    stop_head = " %5s %7s" % ("стоп%", "стопов") if has_stop else ""
    print("%-12s %-7s %6s %5s %5s %5s %5s %4s%s %9s %8s %6s  %s"
          % ("пара", "сторона", "перекр", "ордер", "pf", "vf", "тейк", "лев", stop_head,
             "итог %", "просадка", "сделок", "вход"))
    for r in rows[:limit]:
        stop = ""
        if has_stop:
            hits = r.get("stop_hits") or ""
            if hits and r.get("stop_maybe") not in (None, "", "0"):
                hits += "+%s?" % r["stop_maybe"]
            stop = " %5s %7s" % (r.get("stop_loss") or "—", hits or "—")
        liq_note = ""
        if r.get("stop_wiped") not in (None, "", "False"):
            liq_note = "  ДЕПОЗИТ КОНЧИЛСЯ НА СТОПАХ %s" % r["stop_wiped"]
        elif partial(r):
            liq_note = ("  НЕПОЛНЫЙ: стоп спас бы от ликвидации %s, дальше сервер не считал"
                        % fmt_time(r.get("liq_time")))
        elif r.get("liquidated") == "True":
            liq_note = "  ЛИКВИДАЦИЯ %s" % fmt_time(r.get("liq_time"))
        print("%-12s %-7s %6s %5s %5s %5s %5s %4s%s %9.2f %8s %6s  %s%s"
              % (r["symbol"], r["position"], r["price_overlap"], r["orders"], r["price_factor"],
                 r["volume_factor"], r["profit"], r["leverage"], stop, r["_net"],
                 r["max_drawdown"], r["entries"], entry_name(r), liq_note))
    if not rows:
        print("  (нет посчитанных строк)")
    elif liq and not allow_liq:
        print("Скрыто с ликвидацией и неполных: %d (показать: python gbt.py top %s --with-liq)"
              % (liq, os.path.basename(path)))


def cmd_top(args):
    if not os.path.exists(args.file):
        args.file = results_path(args.file)
    if not os.path.exists(args.file):
        sys.exit("Файла %s ещё нет — сначала запустите прогон." % args.file)
    show_top(args.file, args.n, args.with_liq, args.by)


# ---------- меню (run.bat) ----------

MENU = """
==============================================
   gridbacktest.com — массовые бэктесты
==============================================
  1. Войти через Telegram
  2. Проверить вход
  3. Пары, связки входа, лимиты
  4. Посчитать число прогонов (dry-run)
  5. Пробный прогон (sweep_test.json)
  6. Прогон: связки сайта × параметры сетки (sweep.json)
  7. Прогон: перебор индикаторов (sweep_indicators.json)
  8. Лучшие результаты
  9. Открыть файл результатов
 10. Редактировать конфиг
  0. Выход
"""


def ask(prompt, default=""):
    v = input("%s [%s]: " % (prompt, default) if default else prompt + ": ").strip()
    return v or default


def ask_config(default="sweep.json"):
    found = sorted(f for f in os.listdir(CONFIGS_DIR) if f.endswith(".json"))
    if found:
        print("  в папке configs: " + ", ".join(found))
    cfg = ask("Конфиг", default)
    path = config_path(cfg)
    if not os.path.exists(path):
        print("Файл %s не найден, беру %s" % (cfg, default))
        path = os.path.join(CONFIGS_DIR, default)
    return path


def ask_results():
    found = sorted(f for f in os.listdir(RESULTS_DIR) if f.endswith(".csv"))
    if found:
        print("  в папке results: " + ", ".join(found))
    return results_path(ask("Файл", found[0] if len(found) == 1 else "results.csv"))


def sweep_args(config, **kw):
    return argparse.Namespace(**dict(dict(config=config, out=None, workers=None,
                                          dry_run=False, force=False), **kw))


def menu_action(ch):
    if ch == "1":
        cmd_login(None)
    elif ch == "2":
        cmd_me(None)
    elif ch == "3":
        cmd_info(None)
    elif ch == "4":
        cmd_sweep(sweep_args(ask_config(), dry_run=True))
    elif ch == "5":
        cmd_sweep(sweep_args(os.path.join(CONFIGS_DIR, "sweep_test.json")))
    elif ch in ("6", "7"):
        cmd_sweep(sweep_args(ask_config("sweep.json" if ch == "6" else "sweep_indicators.json")))
    elif ch == "8":
        f = ask_results()
        n = ask("Сколько строк", "30")
        print("  1 — по доходности, 2 — по просадке, 3 — по доходности вместе с ликвидациями")
        how = ask("Сортировка", "1")
        cmd_top(argparse.Namespace(file=f, n=int(n) if n.isdigit() else 30,
                                   by="max_drawdown" if how == "2" else "net_pct",
                                   with_liq=how == "3"))
    elif ch == "9":
        f = ask_results()
        if os.path.exists(f):
            os.startfile(f)
        else:
            print("Файла %s ещё нет." % f)
    elif ch == "10":
        os.startfile(ask_config())
        return False
    else:
        return False
    return True


def cmd_menu(_args):
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print(MENU)
        try:
            ch = input("Выбор: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if ch == "0":
            return
        try:
            shown = menu_action(ch)
        except SystemExit as e:          # sys.exit с текстом ошибки — показать и вернуться в меню
            if e.code not in (None, 0):
                print(e.code)
            shown = True
        except KeyboardInterrupt:
            print("\nПрервано.")
            shown = True
        except Exception as e:
            print("Ошибка: %s" % e)
            shown = True
        if shown:
            try:
                input("\nEnter — в меню…")
            except (EOFError, KeyboardInterrupt):
                return


def main():
    migrate_layout()
    ap = argparse.ArgumentParser(description="Массовые бэктесты gridbacktest.com")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("menu", help="интерактивное меню").set_defaults(fn=cmd_menu)
    sub.add_parser("login", help="вход через Telegram").set_defaults(fn=cmd_login)
    sub.add_parser("me", help="проверить сессию").set_defaults(fn=cmd_me)
    sub.add_parser("info", help="пары, связки, лимиты").set_defaults(fn=cmd_info)
    p = sub.add_parser("sweep", help="прогнать комбинации из конфига")
    p.add_argument("config")
    p.add_argument("--out", help="CSV с результатами (по умолчанию из конфига / results.csv)")
    p.add_argument("--workers", type=int, help="параллельных запросов")
    p.add_argument("--dry-run", action="store_true", help="только посчитать комбинации")
    p.add_argument("--force", action="store_true", help="считать, даже если итоги закрыты")
    p.set_defaults(fn=cmd_sweep)
    p = sub.add_parser("top", help="лучшие результаты из CSV")
    p.add_argument("file", nargs="?", default=results_path("results.csv"))
    p.add_argument("-n", type=int, default=30)
    p.add_argument("--by", default="net_pct", choices=["net_pct", "net", "max_drawdown"])
    p.add_argument("--with-liq", action="store_true", help="включать ликвидированные")
    p.set_defaults(fn=cmd_top)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
