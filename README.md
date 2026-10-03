# "Inhuman" in 200 languages

A first-pass cross-linguistic survey, using a large language model (Claude) as the research instrument, of how languages condemn extreme cruelty, and in particular how often they do it by denying that the act or the one who did it is human (as English *inhuman* or Swedish *omänsklig* do).

**All expressions in this repository were produced by an LLM. They are hypotheses to be checked against dictionaries or native speakers, not verified linguistic data.**

## What was done

The sample is the 200-language sample of the *World Atlas of Language Structures* (WALS), designed to be spread across language families and geographic areas. The list is built from the WALS CLDF dataset (release v2020.6). Each language is named to the model with a short identifier taken from WALS, e.g. *Slave (genus: Athapaskan; family: Na-Dene; spoken in: Canada)*.

Each language went through two stages, each a separate, fresh API request:

1. **Collection** ([prompts/stage1_collection.txt](prompts/stage1_collection.txt)). The model is asked for up to fifteen words and fixed expressions that ordinary speakers use to condemn an act, or the one who did it, as extraordinarily cruel or wicked. The prompt contains no hint of the hypothesis: no examples, and none of the words *inhuman, monstrous, beastly, savage, unnatural, demonic, person, human*. The model is told that "insufficient knowledge" is an acceptable answer.
2. **Classification** ([prompts/stage2_classification.txt](prompts/stage2_classification.txt)). For each language that produced expressions, a second request classifies each expression as:
   - **denies_humanity**: requires an explicit negation, privation or exclusion of a word for "human", "person" or "humanity", now or in the word's etymology. A comparison with an animal or demon does not count by itself.
   - **animal**: compares the act or person to an animal.
   - **supernatural**: compares the act or person to a demon, monster, ghost or other supernatural being.
   - **against_nature**: presents the act or person as unnatural.
   - **other_figurative**: any other figurative basis.

   It also codes origin (native, calque, recent or older loanword). Output is enforced against [prompts/stage2_schema.json](prompts/stage2_schema.json).

Every request is a single user message, with no system prompt and no tools, sent through the Message Batches API. Every response is validated, and problems are flagged rather than silently repaired.

**Main run** ([runs/wals200_opus](runs/wals200_opus)):

| Setting | Value |
|---|---|
| Model | `claude-opus-5-5` |
| Effort | `medium` |
| Thinking | Adaptive. It cannot be switched off on this model; summaries are logged. |
| Requests | One per language (no repeats) |
| Date | 3 October 2026 |
| Cost | $3.57 at batch prices ($1.72 for Stage 1, $1.86 for Stage 2) |

**Development.** The prompts were settled in an 11-language pilot. It contained English, Swedish, eight languages chosen by stepping through the WALS 200, and an invented language as a control. Both models correctly declined the invented language.

The pilot compared Claude Sonnet 5.5 (thinking off) with Claude Opus 5.5. Opus gave answers for one language more, and it alone surfaced *inhuman* and *omänsklig*. That led to the choice of Opus, a longer expression limit, and an explicit-negation rule for `denies_humanity`. The pilot data are not included here.

## Results

Stage 1, all 200 languages:

| Outcome | Languages | % |
|---|---|---|
| Produced expressions | 61 | 30.5% |
| Insufficient knowledge | 139 | 69.5% |
| Invalid or missing | 0 | 0% |

Of the 61 languages with expressions, the share with at least one expression coded true in each category:

| Category | Languages | % |
|---|---|---|
| denies_humanity | 25 | 41.0% |
| animal | 33 | 54.1% |
| supernatural | 54 | 88.5% |
| against_nature | 4 | 6.6% |
| any of those four | 58 | 95.1% |
| other_figurative | 55 | 90.2% |
| any of all five | 60 | 98.4% |

## Reproducing the tables

Requires Python 3.10 or later.

### From the archived results (no API key needed)

This needs no packages and no key:

```
python analyze.py
```

It prints both tables and writes `runs/wals200_opus/analysis/summary.csv` and `languages.csv` (one row per language).

### Redoing the API calls (needs an Anthropic API key)

```
pip install -r requirements.txt
python run_pipeline.py
python analyze.py --run replication
```

**Setting the key.** Before running, set the `ANTHROPIC_API_KEY` environment variable:
- Windows PowerShell: `$env:ANTHROPIC_API_KEY = "..."`
- macOS or Linux: `export ANTHROPIC_API_KEY=...`

Alternatively, put the key in a file named `key.txt` in this folder; it is git-ignored.

**What `run_pipeline.py` does.** It runs both stages into `runs/replication/`, using the main run's settings. It sends exactly the same Stage 1 requests.
- It shows a cost estimate and asks you to type `yes` before each batch; add `--yes` to skip this.
- Expect about $4 at October 2026 prices.
- Batches can take from minutes to several hours.
- If the script is stopped, run the same command again to resume.

**Results will not be identical.** Model output varies from run to run, and models are eventually retired. That is why the raw responses of the main run are archived here.

## Files

| Path | Contents |
|---|---|
| `data/raw/` | WALS v2020.6 `languages.csv` and `countries.csv`, unchanged |
| `data/languages_wals200.csv` | The language list, built by `build_language_list.py` |
| `prompts/` | The two prompts and the Stage 2 output schema, exactly as sent (placeholders in `{CURLY_CAPS}`) |
| `run_pipeline.py` | Runs both stages end to end |
| `stage1_collect.py`, `stage2_classify.py` | The two stages, each with `dryrun`, `submit` and `collect` commands |
| `common.py` | Shared code: request building, validation, logging, cost |
| `analyze.py` | Produces the tables above |
| `runs/wals200_opus/stage1/` | The Stage 1 batch: `dryrun_requests.jsonl` (every request exactly as sent), `submission.json` (batch ID, time), `raw_results.jsonl` (raw API responses), `responses.jsonl` (validated), `expressions.csv`, `costs.csv` (per call), and a copy of the prompt used |
| `runs/wals200_opus/stage2/` | The same for Stage 2, plus `joined.csv`: one row per expression with all Stage 1 and Stage 2 fields |
| `runs/wals200_opus/analysis/` | Output of `analyze.py` |

## References

Comrie, B., Dryer, M. S., Gil, D., & Haspelmath, M. (2013). Introduction. In M. S. Dryer & M. Haspelmath (Eds.), *The world atlas of language structures online*. Max Planck Institute for Evolutionary Anthropology. https://wals.info/chapter/s1

Dryer, M. S., & Haspelmath, M. (Eds.). (2013). *The world atlas of language structures online*. Max Planck Institute for Evolutionary Anthropology. https://wals.info

The WALS data in `data/raw/` come from https://github.com/cldf-datasets/wals (release v2020.6) and are licensed under CC BY 4.0.
