"""Shared helpers for the Stage 1 and Stage 2 scripts.

Everything that talks to the Claude API is in the section marked "Claude API" at the
bottom. The language list, prompt files and validation are provider-neutral, so a
second provider would need its own versions of those functions only.
"""

import csv
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROMPTS = ROOT / "prompts"
RUNS = ROOT / "runs"
KEY_FILE = ROOT / "key.txt"

# Defaults are the settings of the main run (runs/wals200_opus).
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_LANGUAGES = ROOT / "data" / "languages_wals200.csv"
DEFAULT_REPEATS = 1
MAX_TOKENS = 16000  # Includes any thinking tokens. A response cut off here is flagged.

# Thinking and effort per model. Each default effort is that model's own default, set
# explicitly so that it is logged.
# - Sonnet 5.5: "between_tools" is its lowest thinking setting. With no tools in the
#   request it means no extended thinking at all, so the request is a bare prompt.
#   It is only accepted at effort "high" or below, with no other field in "thinking".
# - Opus 5.5: thinking cannot be switched off; adaptive is the only mode.
#   display="summarized" keeps a readable summary of the thinking in the log.
# assumed_output_tokens (per stage) is only for the dry-run cost estimate. Opus figures are
# the wals200_opus means (736 and 2,410) rounded up; Sonnet figures are rough guesses.
MODEL_SETTINGS = {
    "claude-sonnet-5-5": {"thinking": {"type": "between_tools"}, "default_effort": "high",
                          "efforts": ["low", "medium", "high"],
                          "assumed_output_tokens": {1: 1000, 2: 3000}},
    "claude-opus-5-5": {"thinking": {"type": "adaptive", "display": "summarized"}, "default_effort": "medium",
                        "efforts": ["low", "medium", "high", "xhigh", "max"],
                        "assumed_output_tokens": {1: 800, 2: 2500}},
}

# Batch prices in USD per million tokens (input, output) = 50% of standard prices.
# Standard prices checked 2 Oct 2026: Opus 5.5 $4/$20, Sonnet 5.5 $2/$10.
BATCH_PRICES = {
    "claude-opus-5-5": (2.00, 10.00),
    "claude-sonnet-5-5": (1.00, 5.00),
}

# Brief, section 4, "Prompt hygiene": none of these may appear in the Stage 1 prompt.
# Matched as word stems, so "humanity" and "monster" are caught too.
HYGIENE_STEMS = ["inhuman", "monst", "beast", "bestial", "savage", "unnatural",
                 "demon", "person", "human"]

PLACEHOLDER = re.compile(r"\{([A-Z][A-Z_]*)\}")


def utf8_console():
    """Windows consoles default to cp1252, which cannot print most language names."""
    sys.stdout.reconfigure(encoding="utf-8")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rel(path):
    """Path relative to the project folder where possible, for readable logs."""
    path = Path(path).resolve()
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------- files

def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_csv(path, rows, columns):
    """utf-8-sig so that Excel shows non-Latin scripts correctly."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: cell(row.get(k)) for k in columns})


def cell(value):
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def load_languages(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    codes = [r["wals_code"] for r in rows]
    if len(codes) != len(set(codes)):
        sys.exit(f"Duplicate wals_code in {path}")
    for code in codes:
        if not re.fullmatch(r"[a-z0-9]+", code):
            sys.exit(f"wals_code {code!r} is not usable in a batch custom_id")
    return rows


# ---------------------------------------------------------------- prompts

def load_prompt(path):
    """The prompt file exactly as stored, minus the final newline."""
    return Path(path).read_text(encoding="utf-8").rstrip("\n")


def fill(template, values):
    """Fill {CURLY_CAPS} placeholders in one pass.

    One pass matters: the filled-in text (e.g. Stage 1 expressions) is never re-scanned.
    Every placeholder in the template must have a value, and every value must be used.
    """
    names = set(PLACEHOLDER.findall(template))
    if names != set(values):
        sys.exit(f"Placeholder mismatch: template has {sorted(names)}, values given for {sorted(values)}")
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


def hygiene_hits(text):
    hits = []
    for stem in HYGIENE_STEMS:
        hits += [m.group(0) for m in re.finditer(rf"\b{stem}\w*", text, re.IGNORECASE)]
    return hits


# ---------------------------------------------------------------- responses

def parse_json_text(text):
    """Parse a response that should be JSON only.

    Returns (obj, errors, warnings). Nothing is repaired: the only tolerance is a
    response wrapped whole in a Markdown code fence, which is parsed but flagged.
    """
    try:
        return json.loads(text), [], []
    except json.JSONDecodeError:
        pass
    m = re.fullmatch(r"\s*```(?:json)?\s*\n(.*?)\n?\s*```\s*", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1)), [], ["json_wrapped_in_code_fence"]
        except json.JSONDecodeError:
            pass
    return None, ["response_is_not_valid_json"], []


def unpack_result(record):
    """Pull the useful parts out of one raw batch result record."""
    result = record["result"]["result"]
    out = {"result_type": result["type"], "stop_reason": None, "model": None, "usage": None,
           "text": None, "thinking_summary": None, "error": None}
    if result["type"] == "succeeded":
        msg = result["message"]
        out["stop_reason"] = msg.get("stop_reason")
        out["model"] = msg.get("model")
        out["usage"] = msg.get("usage")
        out["text"] = "".join(b.get("text", "") for b in msg["content"] if b["type"] == "text")
        thinking = [b.get("thinking", "") for b in msg["content"] if b["type"] == "thinking"]
        out["thinking_summary"] = "\n\n".join(t for t in thinking if t) or None
    elif result["type"] == "errored":
        out["error"] = result.get("error")
    return out


def stop_reason_errors(parts):
    if parts["result_type"] != "succeeded":
        return [f"batch_result_{parts['result_type']}"]
    if parts["stop_reason"] != "end_turn":
        return [f"stop_reason_{parts['stop_reason']}"]
    return []


# ---------------------------------------------------------------- cost

def cost_usd(model, input_tokens, output_tokens):
    if model not in BATCH_PRICES:
        return None
    p_in, p_out = BATCH_PRICES[model]
    return input_tokens / 1e6 * p_in + output_tokens / 1e6 * p_out


def call_cost(parts, requested_model):
    """Tokens and cost of one call, from its usage block.

    Results that did not succeed carry no usage and are not billed, so they cost 0.
    Prompt caching is not used; any cache tokens are counted at the full input price,
    which can only overstate the cost.
    """
    usage = parts["usage"] or {}
    t_in = (usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0) \
        + (usage.get("cache_read_input_tokens") or 0)
    t_out = usage.get("output_tokens") or 0
    cost = cost_usd(parts["model"] or requested_model, t_in, t_out) if usage else 0.0
    return {"input_tokens": t_in, "output_tokens": t_out,
            "cost_usd": None if cost is None else round(cost, 6)}


COST_COLUMNS = ["custom_id", "wals_code", "name", "repeat", "model", "result_type", "stop_reason",
                "status", "input_tokens", "output_tokens", "cost_usd"]


def cost_summary(responses):
    """Totals and per-call figures over the calls that returned a result."""
    costs = [r["cost"] for r in responses if r.get("cost")]
    billed = [x["cost_usd"] for x in costs if x["cost_usd"]]
    return {
        "calls": len(costs),
        "input_tokens": sum(x["input_tokens"] for x in costs),
        "output_tokens": sum(x["output_tokens"] for x in costs),
        "cost_usd": round(sum(billed), 4),
        "mean_cost_per_call_usd": round(sum(billed) / len(billed), 6) if billed else None,
        "min_cost_per_call_usd": min(billed) if billed else None,
        "max_cost_per_call_usd": max(billed) if billed else None,
        "mean_input_tokens_per_call": round(sum(x["input_tokens"] for x in costs) / len(costs)) if costs else None,
        "mean_output_tokens_per_call": round(sum(x["output_tokens"] for x in costs) / len(costs)) if costs else None,
    }


def print_collect_summary(summary):
    """Counts on one line each; the cost block as a single readable line."""
    for k, v in summary.items():
        if k == "actual_cost":
            print(f"  cost: ${v['cost_usd']:.4f} for {v['calls']} calls "
                  f"(${v['mean_cost_per_call_usd'] or 0:.4f}/call, "
                  f"mean {v['mean_output_tokens_per_call']} output tokens/call)")
        elif k != "collected_at":
            print(f"  {k}: {v}")


def write_costs(stage_dir, responses):
    """costs.csv: one row per call, with its tokens and cost at batch prices."""
    rows = [{**r, **r["cost"]} for r in responses if r.get("cost")]
    write_csv(stage_dir / "costs.csv", rows, COST_COLUMNS)


def confirm(question):
    answer = input(f"{question} Type 'yes' to continue: ")
    return answer.strip().lower() == "yes"


# ================================================================ Claude API

def resolve_effort(model, effort):
    settings = MODEL_SETTINGS[model]
    effort = effort or settings["default_effort"]
    if effort not in settings["efforts"]:
        sys.exit(f"Effort {effort!r} is not allowed for {model}; choose from {settings['efforts']}")
    return effort


def build_params(model, effort, prompt, output_format=None):
    """One bare request: a single user message, no system prompt, no tools.

    Thinking is set per model (see MODEL_SETTINGS). No sampling parameters: current
    models reject non-default temperature/top_p/top_k.
    """
    params = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "thinking": dict(MODEL_SETTINGS[model]["thinking"]),
        "output_config": {"effort": effort},
        "messages": [{"role": "user", "content": prompt}],
    }
    if output_format is not None:
        params["output_config"]["format"] = output_format
    return params


def make_client(max_retries=2):
    """API key from ANTHROPIC_API_KEY, else from key.txt. The key is never logged."""
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        sys.exit("No API key: set ANTHROPIC_API_KEY or put the key in key.txt")
    return anthropic.Anthropic(api_key=key, max_retries=max_retries)


def sdk_version():
    import anthropic
    return anthropic.__version__


def count_input_tokens(client, params):
    """Exact input token count from the free count_tokens endpoint."""
    import anthropic
    args = {k: v for k, v in params.items() if k != "max_tokens"}
    try:
        return client.messages.count_tokens(**args).input_tokens
    except anthropic.BadRequestError:
        # Fall back to the message alone if the endpoint rejects a parameter.
        return client.messages.count_tokens(model=params["model"], messages=params["messages"]).input_tokens


def rough_input_tokens(params):
    """Offline estimate: about 3.5 characters per token for English prompt text."""
    text = params["messages"][0]["content"]
    if "format" in params["output_config"]:
        text += json.dumps(params["output_config"]["format"])
    return round(len(text) / 3.5)


def write_dryrun(stage_dir, requests, config, assume_output_tokens, offline, input_files):
    """Write exactly what will be sent, plus a token and cost estimate.

    input_files (prompt and schema files) are copied into stage_dir, so each run
    folder keeps the exact prompt versions it used.
    """
    if (stage_dir / "submission.json").exists():
        sys.exit(f"{rel(stage_dir)} has already been submitted; its dry run is the record of what was "
                 "sent and will not be overwritten. Use a new --run name.")
    stage_dir.mkdir(parents=True, exist_ok=True)
    for path in input_files:
        shutil.copyfile(path, stage_dir / Path(path).name)
    if assume_output_tokens is None:
        assume_output_tokens = MODEL_SETTINGS[config["model"]]["assumed_output_tokens"][config["stage"]]
    # Repeats share a prompt, so each distinct request is counted once. Extra retries
    # (with the SDK's backoff) ride out rate limits on the count_tokens endpoint.
    client = None if offline else make_client(max_retries=8)
    model = config["model"]
    counted = {}
    distinct = len({json.dumps(r["params"], sort_keys=True) for r in requests})
    if not offline:
        print(f"Counting input tokens for {distinct} distinct requests...", flush=True)
    for r in requests:
        key = json.dumps(r["params"], sort_keys=True)
        if key not in counted:
            counted[key] = rough_input_tokens(r["params"]) if offline else count_input_tokens(client, r["params"])
            if not offline and len(counted) % 50 == 0:
                print(f"  {len(counted)}/{distinct}", flush=True)
        r["input_tokens_estimate"] = counted[key]
        r["cost_estimate_usd"] = round(cost_usd(model, r["input_tokens_estimate"], assume_output_tokens), 6)
    total_in = sum(r["input_tokens_estimate"] for r in requests)
    total_out = assume_output_tokens * len(requests)
    worst_out = MAX_TOKENS * len(requests)
    summary = {
        "created_at": now(),
        **config,
        "request_count": len(requests),
        "token_count_method": "rough (3.5 chars/token)" if offline else "count_tokens endpoint",
        "input_tokens": total_in,
        "assumed_output_tokens_per_request": assume_output_tokens,
        "output_tokens_assumed": total_out,
        "estimated_cost_usd": round(cost_usd(model, total_in, total_out) or 0, 4),
        "estimated_cost_per_call_usd": round((cost_usd(model, total_in, total_out) or 0) / len(requests), 6),
        "worst_case_cost_usd": round(cost_usd(model, total_in, worst_out) or 0, 4),
    }
    write_jsonl(stage_dir / "dryrun_requests.jsonl", requests)
    write_json(stage_dir / "dryrun_summary.json", summary)
    return summary


def print_summary(summary):
    print(f"  requests:        {summary['request_count']}  (model {summary['model']}, effort {summary['effort']})")
    print(f"  input tokens:    {summary['input_tokens']:,}  ({summary['token_count_method']})")
    print(f"  output tokens:   {summary['output_tokens_assumed']:,}  (assumed "
          f"{summary['assumed_output_tokens_per_request']:,}/request, including any thinking)")
    print(f"  estimated cost:  ${summary['estimated_cost_usd']:.2f} at batch prices "
          f"(${summary['estimated_cost_per_call_usd']:.4f}/call)")
    print(f"  worst case:      ${summary['worst_case_cost_usd']:.2f} (every request hits max_tokens={MAX_TOKENS:,})")


def submit_batch(stage_dir, yes, show_summary=True):
    """Send dryrun_requests.jsonl unchanged as one Message Batch."""
    submission_path = stage_dir / "submission.json"
    if submission_path.exists():
        sys.exit(f"Already submitted: {rel(submission_path)}. Use a new --run name to resubmit.")
    dryrun_path = stage_dir / "dryrun_requests.jsonl"
    if not dryrun_path.exists():
        sys.exit("No dry run found. Run the dryrun command first.")
    requests = read_jsonl(dryrun_path)
    if show_summary:
        print(f"About to submit {rel(dryrun_path)}:")
        print_summary(read_json(stage_dir / "dryrun_summary.json"))
    if not yes and not confirm("Submit this batch? This spends money."):
        sys.exit("Not submitted.")

    client = make_client()
    batch = client.messages.batches.create(
        requests=[{"custom_id": r["custom_id"], "params": r["params"]} for r in requests]
    )
    write_json(submission_path, {
        "submitted_at": now(),
        "batch_id": batch.id,
        "request_count": len(requests),
        "dryrun_file": rel(dryrun_path),
        "dryrun_sha256": sha256_file(dryrun_path),
        "anthropic_sdk_version": sdk_version(),
        "batch": batch.model_dump(mode="json"),
    })
    print(f"Submitted batch {batch.id}. Saved {rel(submission_path)}.")


def fetch_results(stage_dir, wait):
    """Download all results once the batch has ended. Returns raw records, or None."""
    submission = read_json(stage_dir / "submission.json")
    client = make_client()
    started, last = time.time(), None
    print(f"Batch {submission['batch_id']} ({submission['request_count']} requests)")
    while True:
        batch = client.messages.batches.retrieve(submission["batch_id"])
        n = batch.request_counts
        state = (batch.processing_status, n.processing, n.succeeded, n.errored, n.canceled, n.expired)
        if state != last:  # one line per change, not per poll
            minutes, seconds = divmod(int(time.time() - started), 60)
            failed = f", {n.errored} errored, {n.canceled} canceled, {n.expired} expired" \
                if n.errored or n.canceled or n.expired else ""
            print(f"  [{minutes:3d}:{seconds:02d}] {batch.processing_status}: "
                  f"{n.succeeded} succeeded, {n.processing} processing{failed}", flush=True)
            last = state
        if batch.processing_status == "ended":
            break
        if not wait:
            print("Not finished yet. Run collect again later, or use --wait.")
            return None
        time.sleep(30)

    retrieved_at = now()
    records = [
        {"batch_id": batch.id, "retrieved_at": retrieved_at, "result": r.model_dump(mode="json")}
        for r in client.messages.batches.results(batch.id)
    ]
    write_jsonl(stage_dir / "raw_results.jsonl", records)
    write_json(stage_dir / "batch_final.json", batch.model_dump(mode="json"))

    sent = {r["custom_id"] for r in read_jsonl(stage_dir / "dryrun_requests.jsonl")}
    got = {r["result"]["custom_id"] for r in records}
    if sent - got:
        print(f"WARNING: no result returned for {sorted(sent - got)}")
    print(f"Saved {len(records)} raw results to {rel(stage_dir / 'raw_results.jsonl')}")
    return records
