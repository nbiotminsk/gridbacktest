"""Разбор результатов: фронт Парето «итог % ↔ просадка %» и лучшие по отношению итог/просадка.

  python analyze.py results.csv [results_indicators.csv ...]
"""
import csv
import os
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gbt import entry_name  # noqa: E402

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)
rows = []
for path in sys.argv[1:]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows += [r for r in csv.DictReader(f) if not r.get("error")]
for r in rows:
    r["np"], r["dd"] = float(r["net_pct"]), float(r["max_drawdown"])
alive = [r for r in rows if r["liquidated"] != "True"]
print("Прогонов: %d, без ликвидации: %d, в плюсе без ликвидации: %d"
      % (len(rows), len(alive), sum(r["np"] > 0 for r in alive)))


def line(r):
    return ("%-5s ov%-4s ord%-3s pf%-4s vf%-4s tp%-4s lev%-3s | итог %+8.2f%%  просадка %5.1f%%  "
            "сделок %-4s | %s" % (r["position"], r["price_overlap"], r["orders"], r["price_factor"],
                                 r["volume_factor"], r["profit"], r["leverage"], r["np"], r["dd"],
                                 r["entries"], entry_name(r)))


# фронт Парето: нет настройки, у которой и итог больше, и просадка меньше
front, best = [], -1e18
for r in sorted(alive, key=lambda r: (r["dd"], -r["np"])):
    if r["np"] > best:
        front.append(r)
        best = r["np"]
print("\nФронт Парето (каждая следующая — прибыльнее, но с большей просадкой):")
for r in front:
    print("  " + line(r))

print("\nЛучшая по итогу при просадке не больше X:")
for cap in (5, 10, 15, 20, 30, 40, 50):
    ok = [r for r in alive if r["dd"] <= cap]
    if ok:
        print("  ≤%2d%%: %s" % (cap, line(max(ok, key=lambda r: r["np"]))))

print("\nТоп-15 по отношению итог / просадка (в плюсе, без ликвидации):")
for r in sorted((r for r in alive if r["np"] > 0),
                key=lambda r: r["np"] / max(r["dd"], 1), reverse=True)[:15]:
    print("  %5.2f  %s" % (r["np"] / max(r["dd"], 1), line(r)))

print("\nПо входам: сколько настроек выжило / лучший итог")
by = defaultdict(list)
for r in rows:
    by[(r["position"], entry_name(r))].append(r)
for (pos, name), rs in sorted(by.items(), key=lambda kv: -max(
        (r["np"] for r in kv[1] if r["liquidated"] != "True"), default=-1e9)):
    ok = [r for r in rs if r["liquidated"] != "True"]
    b = max(ok, key=lambda r: r["np"]) if ok else None
    print("  %-5s %-28s выжило %2d/%-2d  %s" % (pos, name, len(ok), len(rs),
          "лучший %+.1f%% (dd %.1f%%)" % (b["np"], b["dd"]) if b else "—"))
