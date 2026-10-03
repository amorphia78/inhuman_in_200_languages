"""Stage 1 (collection): one bare request per language per repeat.

Usage:
    python stage1_collect.py dryrun  --run NAME [--languages data/languages_wals200.csv]
                                     [--repeats 1] [--model claude-opus-5-5]
                                     [--effort medium] [--offline]
    python stage1_collect.py submit  --run NAME [--yes]
    python stage1_collect.py collect --run NAME [--wait]

run_pipeline.py runs all of this (and Stage 2) in one command.

Files written to runs/<run>/stage1/:
    dryrun_requests.jsonl  every request exactly as it will be sent, with metadata
    dryrun_summary.json    model, parameters, input file hashes, token and cost estimate
    stage1_collection.txt  copy of the prompt template used
    submission.json        batch ID, submission time, SDK version, hash of the dry-run file
    raw_results.jsonl      every raw result as returned by the API (never edited)
    batch_final.json       the batch object after it ended
    responses.jsonl        one validated record per request (status, errors, warnings, parsed JSON)
    expressions.csv        one row per expression, for reading
    costs.csv              one row per call: tokens and cost at batch prices
    collect_summary.json   counts, actual cost in total and per call
"""

import argparse

import common as c

PROMPT_FILE = c.PROMPTS / "stage1_collection.txt"

TOP_KEYS = {"language", "insufficient_knowledge", "expressions"}
EXPR_KEYS = ["expression", "romanisation", "literal", "meaning", "register", "currency", "confidence"]
ALLOWED = {
    "register": {"everyday", "slang_or_insult", "formal_or_literary"},
    "currency": {"current", "archaic", "regional"},
    "confidence": {"high", "medium", "low"},
}
MAX_EXPRESSIONS = 15

EXPRESSION_COLUMNS = ["custom_id", "wals_code", "name", "family", "macroarea", "is_control",
                      "control_role", "repeat", "position", "status"] + EXPR_KEYS


def stage_dir(run):
    return c.RUNS / run / "stage1"


# ---------------------------------------------------------------- dryrun

def cmd_dryrun(args):
    languages = c.load_languages(args.languages)
    effort = c.resolve_effort(args.model, args.effort)
    template = c.load_prompt(PROMPT_FILE)

    hits = c.hygiene_hits(template)
    if hits:
        print(f"WARNING: prompt-hygiene words in the Stage 1 prompt: {hits}")

    requests = []
    for lang in languages:
        prompt = c.fill(template, {"LANGUAGE_NAME": lang["name"],
                                   "LANGUAGE_IDENTIFIER": lang["prompt_identifier"]})
        for repeat in range(1, args.repeats + 1):
            requests.append({
                "custom_id": f"s1_{lang['wals_code']}_r{repeat}",
                "meta": {**lang, "repeat": repeat},
                "params": c.build_params(args.model, effort, prompt),
            })

    config = {
        "stage": 1,
        "model": args.model,
        "effort": effort,
        "max_tokens": c.MAX_TOKENS,
        "repeats": args.repeats,
        "languages_file": c.rel(args.languages),
        "languages_sha256": c.sha256_file(args.languages),
        "language_count": len(languages),
        "prompt_file": c.rel(PROMPT_FILE),
        "prompt_sha256": c.sha256_file(PROMPT_FILE),
        "prompt_hygiene_hits": hits,
    }
    summary = c.write_dryrun(stage_dir(args.run), requests, config, args.assume_output_tokens, args.offline,
                             input_files=[PROMPT_FILE])
    print(f"Dry run written to {c.rel(stage_dir(args.run))}/")
    c.print_summary(summary)
    if getattr(args, "hint", True):
        print("Check dryrun_requests.jsonl, then run: python stage1_collect.py submit --run", args.run)


# ---------------------------------------------------------------- collect

def validate(obj, language_name):
    """Check one parsed Stage 1 response. Returns (errors, warnings)."""
    errors, warnings = [], []
    if not isinstance(obj, dict):
        return ["top_level_not_an_object"], []

    missing, extra = TOP_KEYS - obj.keys(), obj.keys() - TOP_KEYS
    errors += [f"missing_key:{k}" for k in sorted(missing)]
    errors += [f"unexpected_key:{k}" for k in sorted(extra)]

    if "language" in obj and obj["language"] != language_name:
        warnings.append(f"language_echo_differs:{obj['language']!r}")

    insufficient = obj.get("insufficient_knowledge")
    if not isinstance(insufficient, bool):
        errors.append("insufficient_knowledge_not_boolean")

    expressions = obj.get("expressions")
    if not isinstance(expressions, list):
        errors.append("expressions_not_a_list")
        return errors, warnings

    if insufficient is True and expressions:
        errors.append("insufficient_knowledge_true_but_expressions_given")
    if insufficient is False and not expressions:
        warnings.append("no_expressions_but_insufficient_knowledge_false")
    if len(expressions) > MAX_EXPRESSIONS:
        warnings.append(f"more_than_{MAX_EXPRESSIONS}_expressions:{len(expressions)}")

    for n, e in enumerate(expressions, start=1):
        if not isinstance(e, dict):
            errors.append(f"expr{n}:not_an_object")
            continue
        errors += [f"expr{n}:missing_key:{k}" for k in EXPR_KEYS if k not in e]
        errors += [f"expr{n}:unexpected_key:{k}" for k in e if k not in EXPR_KEYS]
        for k in ["expression", "literal", "meaning"]:
            if k in e and not (isinstance(e[k], str) and e[k].strip()):
                errors.append(f"expr{n}:{k}_not_a_nonempty_string")
        if "romanisation" in e and not (e["romanisation"] is None or isinstance(e["romanisation"], str)):
            errors.append(f"expr{n}:romanisation_not_string_or_null")
        for k, allowed in ALLOWED.items():
            if k in e and not (isinstance(e[k], str) and e[k] in allowed):
                errors.append(f"expr{n}:{k}_not_allowed:{e[k]!r}")
    return errors, warnings


def cmd_collect(args):
    sdir = stage_dir(args.run)
    raw = c.fetch_results(sdir, args.wait)
    if raw is None:
        return
    raw_by_id = {r["result"]["custom_id"]: r for r in raw}

    responses, expression_rows = [], []
    for req in c.read_jsonl(sdir / "dryrun_requests.jsonl"):
        meta = req["meta"]
        record = {"custom_id": req["custom_id"], **meta}
        if req["custom_id"] not in raw_by_id:
            responses.append({**record, "status": "missing", "errors": ["no_result_returned"], "warnings": []})
            continue

        parts = c.unpack_result(raw_by_id[req["custom_id"]])
        errors, warnings = c.stop_reason_errors(parts), []
        parsed = None
        if parts["text"] is not None:
            parsed, parse_errors, parse_warnings = c.parse_json_text(parts["text"])
            errors += parse_errors
            warnings += parse_warnings
        if parsed is not None:
            v_errors, v_warnings = validate(parsed, meta["name"])
            errors += v_errors
            warnings += v_warnings
        if parts["model"] and parts["model"] != req["params"]["model"]:
            warnings.append(f"response_model_differs:{parts['model']}")

        status = "valid" if not errors else "invalid"
        insufficient = parsed.get("insufficient_knowledge") if isinstance(parsed, dict) else None
        responses.append({
            **record,
            "status": status,
            "errors": errors,
            "warnings": warnings,
            "insufficient_knowledge": insufficient,
            "result_type": parts["result_type"],
            "stop_reason": parts["stop_reason"],
            "model": parts["model"],
            "usage": parts["usage"],
            "cost": c.call_cost(parts, req["params"]["model"]),
            "api_error": parts["error"],
            "response_text": parts["text"],
            "thinking_summary": parts["thinking_summary"],
            "parsed": parsed,
        })
        if isinstance(parsed, dict) and isinstance(parsed.get("expressions"), list):
            for n, e in enumerate(parsed["expressions"], start=1):
                if isinstance(e, dict):
                    expression_rows.append({"custom_id": req["custom_id"], **meta,
                                            "position": n, "status": status, **e})

    c.write_jsonl(sdir / "responses.jsonl", responses)
    c.write_costs(sdir, responses)
    c.write_csv(sdir / "expressions.csv", expression_rows, EXPRESSION_COLUMNS)

    def count(**kw):
        return sum(all(r.get(k) == v for k, v in kw.items()) for r in responses)

    summary = {
        "collected_at": c.now(),
        "requests": len(responses),
        "valid": count(status="valid"),
        "invalid": count(status="invalid"),
        "missing": count(status="missing"),
        "valid_insufficient_knowledge": count(status="valid", insufficient_knowledge=True),
        "valid_with_expressions": sum(r["status"] == "valid" and bool(r["parsed"]["expressions"])
                                      for r in responses),
        "expressions": len(expression_rows),
        "responses_with_warnings": sum(bool(r["warnings"]) for r in responses),
        "actual_cost": c.cost_summary(responses),
    }
    c.write_json(sdir / "collect_summary.json", summary)
    c.print_collect_summary(summary)
    for r in responses:
        if r["errors"]:
            print(f"  INVALID {r['custom_id']}: {r['errors']}")
    print(f"Wrote {c.rel(sdir / 'responses.jsonl')} and {c.rel(sdir / 'expressions.csv')}")


# ---------------------------------------------------------------- main

def main():
    c.utf8_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("dryrun", help="build the requests and estimate cost; sends nothing billable")
    p.add_argument("--run", required=True, help="name of this run; files go to runs/<run>/")
    p.add_argument("--languages", type=c.Path, default=c.DEFAULT_LANGUAGES)
    p.add_argument("--repeats", type=int, default=c.DEFAULT_REPEATS)
    p.add_argument("--model", default=c.DEFAULT_MODEL, choices=sorted(c.MODEL_SETTINGS))
    p.add_argument("--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"],
                   help="default: the model's own default (Sonnet 5.5: high; Opus 5.5: medium)")
    p.add_argument("--assume-output-tokens", type=int, default=None,
                   help="assumed output tokens per request, including any thinking, for the estimate "
                        "(default: set per model in common.MODEL_SETTINGS)")
    p.add_argument("--offline", action="store_true",
                   help="estimate input tokens from character counts instead of the free count_tokens endpoint")
    p.set_defaults(func=cmd_dryrun)

    p = sub.add_parser("submit", help="submit the dry-run requests as one batch (asks for confirmation)")
    p.add_argument("--run", required=True)
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(func=lambda a: c.submit_batch(stage_dir(a.run), a.yes))

    p = sub.add_parser("collect", help="download, log and validate the results")
    p.add_argument("--run", required=True)
    p.add_argument("--wait", action="store_true", help="poll every 60 s until the batch has ended")
    p.set_defaults(func=cmd_collect)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
