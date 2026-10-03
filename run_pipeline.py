"""Run both stages end to end for one run: dry run, submit, wait, collect.

Usage:
    python run_pipeline.py [--run replication] [--model claude-opus-5-5]
                           [--languages data/languages_wals200.csv] [--repeats 1]
                           [--effort medium] [--yes]

The defaults reproduce the settings of the main run (runs/wals200_opus). Then run
    python analyze.py --run replication

Before each stage is submitted, the dry-run estimate is shown and you are asked to
type 'yes' (unless --yes is given). While a batch is processing, one progress line
is printed whenever its counts change.

Resuming: a submitted batch keeps running on the API side if this script is stopped
(e.g. Ctrl+C). Run the same command again: stages already collected are skipped, and
a stage already submitted goes straight to waiting for its results.
"""

import argparse
from argparse import Namespace

import common as c
import stage1_collect as s1
import stage2_classify as s2


def run_stage(number, stage_dir, dryrun, collect, yes):
    print(f"\n=== Stage {number} ===")
    if (stage_dir / "collect_summary.json").exists():
        print(f"Already collected ({c.rel(stage_dir)}); skipping.")
        return
    if (stage_dir / "submission.json").exists():
        print("Already submitted; waiting for results.")
    else:
        dryrun()
        c.submit_batch(stage_dir, yes, show_summary=False)
    collect()


def main():
    c.utf8_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", default="replication", help="name of this run; files go to runs/<run>/")
    parser.add_argument("--model", default=c.DEFAULT_MODEL, choices=sorted(c.MODEL_SETTINGS))
    parser.add_argument("--languages", type=c.Path, default=c.DEFAULT_LANGUAGES)
    parser.add_argument("--repeats", type=int, default=c.DEFAULT_REPEATS)
    parser.add_argument("--effort", default=None, help="default: the model's own default")
    parser.add_argument("--yes", action="store_true", help="submit without asking for confirmation")
    args = parser.parse_args()

    print(f"Run '{args.run}': model {args.model}, languages {c.rel(args.languages)}, {args.repeats} repeats")
    try:
        run_stage(
            1, s1.stage_dir(args.run),
            dryrun=lambda: s1.cmd_dryrun(Namespace(
                run=args.run, languages=args.languages, repeats=args.repeats, model=args.model,
                effort=args.effort, assume_output_tokens=None, offline=False, hint=False)),
            collect=lambda: s1.cmd_collect(Namespace(run=args.run, wait=True)),
            yes=args.yes,
        )
        run_stage(
            2, s2.stage_dir(args.run),
            dryrun=lambda: s2.cmd_dryrun(Namespace(
                run=args.run, model=args.model, effort=args.effort,
                assume_output_tokens=None, offline=False, hint=False)),
            collect=lambda: s2.cmd_collect(Namespace(run=args.run, wait=True)),
            yes=args.yes,
        )
    except KeyboardInterrupt:
        print("\nStopped. Any submitted batch keeps running; run the same command again to resume.")
        return

    total = sum(c.read_json(d / "collect_summary.json")["actual_cost"]["cost_usd"]
                for d in (s1.stage_dir(args.run), s2.stage_dir(args.run)))
    print(f"\n=== Done: total cost ${total:.4f} ===")
    print(f"Stage 1 expressions: {c.rel(s1.stage_dir(args.run) / 'expressions.csv')}")
    print(f"Joined table:        {c.rel(s2.stage_dir(args.run) / 'joined.csv')}")
    print(f"Next: python analyze.py --run {args.run}")


if __name__ == "__main__":
    main()
