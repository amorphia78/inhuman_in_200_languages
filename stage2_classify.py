"""Stage 2 (classification): one request per valid Stage 1 response.

Reads runs/<run>/stage1/responses.jsonl. A Stage 1 response is sent on only if it
passed validation, does not say insufficient_knowledge, and has at least one expression.

Usage:
    python stage2_classify.py dryrun  --run NAME [--model claude-opus-5-5]
                                      [--effort medium] [--offline]
    python stage2_classify.py submit  --run NAME [--yes]
    python stage2_classify.py collect --run NAME [--wait]

Expression IDs are {wals_code}_r{repeat}_{n}, n counting from 1 in Stage 1 order.
Output is schema-enforced (prompts/stage2_schema.json) and is still validated here.

Files written to runs/<run>/stage2/: the same dry-run, submission and raw files as
Stage 1, plus copies of stage2_classification.txt and stage2_schema.json, and
    responses.jsonl     one validated record per request
    joined.jsonl/.csv   one row per Stage 1 expression with its Stage 2 classification,
                        joined by ID (expressions without a valid classification are kept,
                        with stage2_status saying why)
    costs.csv           one row per call: tokens and cost at batch prices
    collect_summary.json
"""

import argparse
import json
import sys

import common as c

PROMPT_FILE = c.PROMPTS / "stage2_classification.txt"
SCHEMA_FILE = c.PROMPTS / "stage2_schema.json"

TOP_KEYS = {"language", "classifications"}
BOOL_KEYS = ["denies_humanity", "animal", "supernatural", "against_nature"]
NULLABLE_TEXT_KEYS = ["other_figurative", "origin_source", "doubt"]
ITEM_KEYS = ["id"] + BOOL_KEYS + ["other_figurative", "humanity_basis", "humanity_transparency",
                                  "origin", "origin_source", "doubt", "reason"]
ALLOWED = {
    "humanity_basis": {"literal", "etymological", "usage"},
    "humanity_transparency": {"transparent", "opaque"},
    "origin": {"native", "calque", "recent_loanword", "older_loanword", "unknown"},
}
BORROWED = {"calque", "recent_loanword", "older_loanword"}
STAGE1_FIELDS = ["expression", "romanisation", "literal", "meaning", "register", "currency", "confidence"]
SENT_FIELDS = ["expression", "romanisation", "literal", "meaning"]

JOINED_COLUMNS = (["id", "wals_code", "name", "family", "genus", "macroarea", "is_control", "control_role",
                   "repeat", "position"] + STAGE1_FIELDS
                  + ["stage2_status", "stage2_errors", "stage2_warnings"] + ITEM_KEYS[1:]
                  + ["stage1_custom_id", "stage2_custom_id"])


def stage_dir(run):
    return c.RUNS / run / "stage2"


# ---------------------------------------------------------------- dryrun

def cmd_dryrun(args):
    stage1_dir = c.RUNS / args.run / "stage1"
    stage1_responses = c.read_jsonl(stage1_dir / "responses.jsonl")
    model = args.model or c.read_json(stage1_dir / "dryrun_summary.json")["model"]
    effort = c.resolve_effort(model, args.effort)
    template = c.load_prompt(PROMPT_FILE)
    schema = c.read_json(SCHEMA_FILE)
    output_format = {"type": "json_schema", "schema": schema}

    requests, skipped = [], {"invalid_or_missing": 0, "insufficient_knowledge": 0, "no_expressions": 0}
    for r in stage1_responses:
        if r["status"] != "valid":
            skipped["invalid_or_missing"] += 1
            continue
        if r["insufficient_knowledge"]:
            skipped["insufficient_knowledge"] += 1
            continue
        if not r["parsed"]["expressions"]:
            skipped["no_expressions"] += 1
            continue

        code, repeat = r["wals_code"], r["repeat"]
        expressions = [{"id": f"{code}_r{repeat}_{n}", "position": n, **e}
                       for n, e in enumerate(r["parsed"]["expressions"], start=1)]
        sent = [{"id": e["id"], **{k: e[k] for k in SENT_FIELDS}} for e in expressions]
        prompt = c.fill(template, {
            "LANGUAGE_NAME": r["name"],
            "LANGUAGE_IDENTIFIER": r["prompt_identifier"],
            "EXPRESSIONS_JSON": json.dumps(sent, ensure_ascii=False, indent=2),
        })
        language_fields = {k: r[k] for k in ["wals_code", "name", "family", "genus", "macroarea",
                                             "prompt_identifier", "is_control", "control_role", "repeat"]}
        requests.append({
            "custom_id": f"s2_{code}_r{repeat}",
            "meta": {**language_fields, "stage1_custom_id": r["custom_id"],
                     "input_ids": [e["id"] for e in expressions], "stage1_expressions": expressions},
            "params": c.build_params(model, effort, prompt, output_format),
        })

    config = {
        "stage": 2,
        "model": model,
        "effort": effort,
        "max_tokens": c.MAX_TOKENS,
        "stage1_responses_file": c.rel(stage1_dir / "responses.jsonl"),
        "stage1_responses_sha256": c.sha256_file(stage1_dir / "responses.jsonl"),
        "stage1_responses_skipped": skipped,
        "prompt_file": c.rel(PROMPT_FILE),
        "prompt_sha256": c.sha256_file(PROMPT_FILE),
        "schema_file": c.rel(SCHEMA_FILE),
        "schema_sha256": c.sha256_file(SCHEMA_FILE),
    }
    if not requests:
        sys.exit(f"No Stage 1 responses eligible for Stage 2 (skipped: {skipped})")
    summary = c.write_dryrun(stage_dir(args.run), requests, config, args.assume_output_tokens, args.offline,
                             input_files=[PROMPT_FILE, SCHEMA_FILE])
    print(f"Dry run written to {c.rel(stage_dir(args.run))}/")
    print(f"  Stage 1 responses not sent on: {skipped}")
    c.print_summary(summary)
    if getattr(args, "hint", True):
        print("Check dryrun_requests.jsonl, then run: python stage2_classify.py submit --run", args.run)


# ---------------------------------------------------------------- collect

def validate(obj, meta):
    """Check one parsed Stage 2 response.

    Returns (errors, warnings, item_issues). errors/warnings apply to the whole response;
    item_issues maps an expression ID to (errors, warnings) for that classification only.
    """
    errors, warnings, item_issues = [], [], {}
    if not isinstance(obj, dict):
        return ["top_level_not_an_object"], [], {}

    errors += [f"missing_key:{k}" for k in sorted(TOP_KEYS - obj.keys())]
    errors += [f"unexpected_key:{k}" for k in sorted(obj.keys() - TOP_KEYS)]
    if "language" in obj and obj["language"] != meta["name"]:
        warnings.append(f"language_echo_differs:{obj['language']!r}")

    items = obj.get("classifications")
    if not isinstance(items, list):
        errors.append("classifications_not_a_list")
        return errors, warnings, {}

    got = [i.get("id") if isinstance(i, dict) else None for i in items]
    expected = meta["input_ids"]
    errors += [f"missing_id:{i}" for i in expected if i not in got]
    errors += [f"unexpected_id:{i}" for i in got if i not in expected]
    errors += [f"duplicate_id:{i}" for i in sorted({i for i in got if got.count(i) > 1}, key=str)]
    if sorted(map(str, got)) == sorted(expected) and got != expected:
        warnings.append("ids_not_in_input_order")

    for item in items:
        if not isinstance(item, dict) or item.get("id") not in expected:
            continue
        e, w = [], []
        e += [f"missing_key:{k}" for k in ITEM_KEYS if k not in item]
        e += [f"unexpected_key:{k}" for k in item if k not in ITEM_KEYS]
        e += [f"{k}_not_boolean" for k in BOOL_KEYS if k in item and not isinstance(item[k], bool)]
        e += [f"{k}_not_string_or_null" for k in NULLABLE_TEXT_KEYS
              if k in item and not (item[k] is None or isinstance(item[k], str))]
        if not (isinstance(item.get("reason"), str) and item["reason"].strip()):
            e.append("reason_not_a_nonempty_string")
        for k, allowed in ALLOWED.items():
            value = item.get(k)
            if value is not None and not (isinstance(value, str) and value in allowed):
                e.append(f"{k}_not_allowed:{value!r}")
        if item.get("origin") is None:
            e.append("origin_is_null")

        # Consistency rules stated in the prompt's field definitions.
        denies = item.get("denies_humanity")
        for k in ["humanity_basis", "humanity_transparency"]:
            if denies is True and item.get(k) is None:
                e.append(f"{k}_null_but_denies_humanity_true")
            if denies is False and item.get(k) is not None:
                e.append(f"{k}_set_but_denies_humanity_false")
        if item.get("origin") in BORROWED and item.get("origin_source") is None:
            w.append("origin_source_null_for_calque_or_loanword")
        if item.get("origin") in ("native", "unknown") and item.get("origin_source") is not None:
            w.append("origin_source_set_for_native_or_unknown")
        item_issues[item["id"]] = (e, w)
    return errors, warnings, item_issues


def cmd_collect(args):
    sdir = stage_dir(args.run)
    raw = c.fetch_results(sdir, args.wait)
    if raw is None:
        return
    raw_by_id = {r["result"]["custom_id"]: r for r in raw}

    responses, joined = [], []
    for req in c.read_jsonl(sdir / "dryrun_requests.jsonl"):
        meta = req["meta"]
        record = {"custom_id": req["custom_id"], **{k: v for k, v in meta.items() if k != "stage1_expressions"}}
        parsed, item_issues = None, {}
        if req["custom_id"] not in raw_by_id:
            errors, warnings = ["no_result_returned"], []
            responses.append({**record, "status": "missing", "errors": errors, "warnings": warnings})
        else:
            parts = c.unpack_result(raw_by_id[req["custom_id"]])
            errors, warnings = c.stop_reason_errors(parts), []
            if parts["text"] is not None:
                parsed, parse_errors, parse_warnings = c.parse_json_text(parts["text"])
                errors += parse_errors
                warnings += parse_warnings
            if parsed is not None:
                v_errors, v_warnings, item_issues = validate(parsed, meta)
                errors += v_errors
                warnings += v_warnings
            if parts["model"] and parts["model"] != req["params"]["model"]:
                warnings.append(f"response_model_differs:{parts['model']}")
            responses.append({
                **record,
                "status": "valid" if not errors and not any(e for e, _ in item_issues.values()) else "invalid",
                "errors": errors,
                "warnings": warnings,
                "item_issues": item_issues,
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

        # Join: every Stage 1 expression gets a row, classified or not.
        items = parsed.get("classifications") if isinstance(parsed, dict) else None
        by_id = {i["id"]: i for i in items if isinstance(i, dict) and "id" in i} if isinstance(items, list) else {}
        for e in meta["stage1_expressions"]:
            item = by_id.get(e["id"])
            item_errors, item_warnings = item_issues.get(e["id"], ([], []))
            if item is None:
                status = "not_classified"
            elif errors or item_errors:
                status = "invalid"
            else:
                status = "valid"
            row = {k: meta[k] for k in ["wals_code", "name", "family", "genus", "macroarea",
                                        "is_control", "control_role", "repeat"]}
            row.update({k: e.get(k) for k in ["id", "position"] + STAGE1_FIELDS})
            row.update({k: item.get(k) for k in ITEM_KEYS[1:]} if item else {})
            row.update({
                "stage2_status": status,
                "stage2_errors": errors + item_errors,
                "stage2_warnings": warnings + item_warnings,
                "stage1_custom_id": meta["stage1_custom_id"],
                "stage2_custom_id": req["custom_id"],
            })
            joined.append(row)

    c.write_jsonl(sdir / "responses.jsonl", responses)
    c.write_costs(sdir, responses)
    c.write_jsonl(sdir / "joined.jsonl", joined)
    c.write_csv(sdir / "joined.csv", joined, JOINED_COLUMNS)

    summary = {
        "collected_at": c.now(),
        "requests": len(responses),
        "valid": sum(r["status"] == "valid" for r in responses),
        "invalid": sum(r["status"] == "invalid" for r in responses),
        "missing": sum(r["status"] == "missing" for r in responses),
        "expressions": len(joined),
        "expressions_valid": sum(r["stage2_status"] == "valid" for r in joined),
        "responses_with_warnings": sum(bool(r["warnings"]) for r in responses),
        "actual_cost": c.cost_summary(responses),
    }
    c.write_json(sdir / "collect_summary.json", summary)
    c.print_collect_summary(summary)
    for r in responses:
        if r["status"] != "valid":
            item_errors = {i: e for i, (e, _) in r.get("item_issues", {}).items() if e}
            print(f"  {r['status'].upper()} {r['custom_id']}: {r['errors']} {item_errors or ''}")
    print(f"Wrote {c.rel(sdir / 'responses.jsonl')} and {c.rel(sdir / 'joined.csv')}")


# ---------------------------------------------------------------- main

def main():
    c.utf8_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("dryrun", help="build the requests from Stage 1 and estimate cost")
    p.add_argument("--run", required=True, help="the run whose Stage 1 results to classify")
    p.add_argument("--model", default=None, choices=sorted(c.MODEL_SETTINGS),
                   help="default: the model used for Stage 1 in this run")
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

    p = sub.add_parser("collect", help="download, log, validate and join the results")
    p.add_argument("--run", required=True)
    p.add_argument("--wait", action="store_true", help="poll every 60 s until the batch has ended")
    p.set_defaults(func=cmd_collect)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
