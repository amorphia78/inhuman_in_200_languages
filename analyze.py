"""Simple first-look analysis of one run.

Usage:
    python analyze.py                  # the archived main run, runs/wals200_opus
    python analyze.py --run NAME       # any other run, e.g. a replication

Reports:
  1. Of all languages, the % for which Stage 1 produced expressions (rather than
     insufficient_knowledge, or an invalid/missing response).
  2. Of the languages with expressions, the % with at least one expression coded true
     in each Stage 2 category, and in any of the top three, four and five categories
     (in the order: denies_humanity, animal, supernatural, against_nature,
     other_figurative).

A language counts as having a category if any of its validly classified expressions
has it (in any repeat, if the run has repeats). other_figurative counts when it is
not null.

Files written to runs/<run>/analysis/:
    summary.csv     the figures printed to the console
    languages.csv   one row per language: Stage 1 outcome and a flag per category
"""

import argparse
from collections import defaultdict

import common as c

CORE = ["denies_humanity", "animal", "supernatural", "against_nature"]
CATEGORIES = CORE + ["other_figurative"]
ANY_TOP = {f"any_top{n}": CATEGORIES[:n] for n in (3, 4, 5)}
MEASURES = CATEGORIES + list(ANY_TOP)
LANGUAGE_FIELDS = ["wals_code", "name", "family", "genus", "macroarea", "is_control", "control_role"]


def pct(n, d):
    return round(100 * n / d, 1) if d else None


def main():
    c.utf8_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", default="wals200_opus")
    args = parser.parse_args()
    run_dir = c.RUNS / args.run

    # Stage 1: outcome per language.
    languages = {}
    for r in c.read_jsonl(run_dir / "stage1" / "responses.jsonl"):
        lang = languages.setdefault(r["wals_code"], {**{k: r[k] for k in LANGUAGE_FIELDS},
                                                     "responses": 0, "with_expressions": 0,
                                                     "insufficient_knowledge": 0, "invalid_or_missing": 0})
        lang["responses"] += 1
        if r["status"] != "valid":
            lang["invalid_or_missing"] += 1
        elif r["insufficient_knowledge"]:
            lang["insufficient_knowledge"] += 1
        elif r["parsed"]["expressions"]:
            lang["with_expressions"] += 1

    # Stage 2: category flags per language, from validly classified expressions only.
    flags = defaultdict(lambda: {k: False for k in CATEGORIES})
    classified = set()
    for row in c.read_jsonl(run_dir / "stage2" / "joined.jsonl"):
        if row["stage2_status"] != "valid":
            continue
        classified.add(row["wals_code"])
        f = flags[row["wals_code"]]
        for k in CORE:
            f[k] = f[k] or row[k] is True
        f["other_figurative"] = f["other_figurative"] or row["other_figurative"] is not None

    rows = []
    for code, lang in languages.items():
        lang["stage1_outcome"] = ("expressions" if lang["with_expressions"]
                                  else "insufficient_knowledge" if lang["insufficient_knowledge"]
                                  else "invalid_or_missing")
        f = flags[code] if code in classified else {}
        rows.append({**lang, **f,
                     **{name: (any(f[k] for k in ks) if f else None) for name, ks in ANY_TOP.items()}})

    n_all = len(rows)
    with_expr = [r for r in rows if r["stage1_outcome"] == "expressions"]
    n_expr = len(with_expr)
    n_ik = sum(r["stage1_outcome"] == "insufficient_knowledge" for r in rows)
    n_bad = sum(r["stage1_outcome"] == "invalid_or_missing" for r in rows)
    n_unclassified = sum(r["wals_code"] not in classified for r in with_expr)

    summary = [
        {"measure": "languages", "count": n_all, "denominator": n_all, "percent": 100.0},
        {"measure": "stage1_produced_expressions", "count": n_expr, "denominator": n_all, "percent": pct(n_expr, n_all)},
        {"measure": "stage1_insufficient_knowledge", "count": n_ik, "denominator": n_all, "percent": pct(n_ik, n_all)},
        {"measure": "stage1_invalid_or_missing", "count": n_bad, "denominator": n_all, "percent": pct(n_bad, n_all)},
    ]
    for k in MEASURES:
        n = sum(bool(r.get(k)) for r in with_expr)
        summary.append({"measure": k, "count": n, "denominator": n_expr, "percent": pct(n, n_expr)})

    out = run_dir / "analysis"
    c.write_csv(out / "summary.csv", summary, ["measure", "count", "denominator", "percent"])
    c.write_csv(out / "languages.csv", rows,
                LANGUAGE_FIELDS + ["stage1_outcome", "responses", "with_expressions", "insufficient_knowledge",
                                   "invalid_or_missing"] + MEASURES)

    labels = {**{k: k for k in CATEGORIES},
              "any_top3": "any of top three", "any_top4": "any of top four", "any_top5": "any of top five"}
    print(f"Run '{args.run}': {n_all} languages")
    print(f"  Stage 1 produced expressions:  {n_expr:4d}  ({pct(n_expr, n_all)}%)")
    print(f"  Insufficient knowledge:        {n_ik:4d}  ({pct(n_ik, n_all)}%)")
    print(f"  Invalid or missing:            {n_bad:4d}  ({pct(n_bad, n_all)}%)")
    print(f"\nOf the {n_expr} languages with expressions, % with at least one expression coded:")
    for s in summary[4:]:
        print(f"  {labels[s['measure']]:28} {s['count']:4d}  ({s['percent']}%)")
    if n_unclassified:
        print(f"\nNote: {n_unclassified} languages with expressions have no valid Stage 2 classification "
              "and count as false above.")
    print(f"\nWrote {c.rel(out / 'summary.csv')} and {c.rel(out / 'languages.csv')}")


if __name__ == "__main__":
    main()
