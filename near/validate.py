"""Проверка лучших настроек на другом периоде (вне подбора)."""
import json, sys, copy
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")
import gbt
WANT = [("long", 90, 20, 1.4, 1.4, 2), ("long", 60, 20, 1.4, 1.4, 2), ("long", 60, 10, 1.4, 1.2, 1),
        ("long", 90, 20, 1.4, 1.2, 1.5), ("long", 75, 10, 1.4, 1.05, 1), ("long", 90, 10, 1.4, 1.05, 1.5),
        ("short", 30, 15, 1.4, 1.2, 1)]
reqs = {}
for f in ("near/stage1.jsonl", "near/stage2.jsonl"):
    for line in open(f, encoding="utf-8"):
        d = json.loads(line); p = d["request"]["params"]
        k = (p["position"], p["price_overlap"], p["orders"], p["price_factor"], p["volume_factor"], p["profit"])
        e = d["request"]["entry"]
        tpl = e.get("groups", [{}])[0].get("filters", [{}])[0].get("tpl") if e["kind"] != "none" else "none"
        if k in WANT and tpl == ("ma" if k[0] == "short" else "rsi"):
            reqs[k] = (tpl, d["request"], d["result"]["by_trades"])
s = gbt.load_session(); data = gbt.get_data(s)
all_m = data["pairs"]["NEARUSDT"]
prev = [m for m in all_m if "2024-10" <= m <= "2025-09"]
print("%-6s %-24s | %-22s | %-22s" % ("", "сетка", "2025-10…2026-09 (подбор)", "2024-10…2025-09 (проверка)"))
for k in WANT:
    tpl, req, bt = reqs[k]
    r2 = copy.deepcopy(req); r2.update(months=prev, **{"from": "2024-10-01", "to": "2025-09-30"})
    out, err = gbt.run_one(s, r2)
    b2 = (out or {}).get("by_trades", {})
    f = lambda b: ("ЛИКВИДАЦИЯ" if b.get("liquidated") else "%+7.1f%% dd %4.1f%%" % (b["net_pct"], b["max_drawdown"])) if b else err
    print("%-5s %-3s ov%-3s ord%-3s vf%-4s tp%-4s | %-22s | %s" % (k[0], tpl, k[1], k[2], k[4], k[5], f(bt), f(b2)))
