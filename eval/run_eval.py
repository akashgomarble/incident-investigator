"""Evaluation harness: run both systems on every incident, score them, write a report.

    python eval/run_eval.py --name main                 # real LLM, both systems, 1 trial
    python eval/run_eval.py --llm fake --name smoke     # offline plumbing check (scores are meaningless)
    python eval/run_eval.py --trials 3 --chaos 0.1      # repeat trials, add random transient tool faults

Runs are resumable: a (incident, system, trial) whose report.json already exists is re-scored, not re-run.
A results folder is tied to the model and fault rates it was made with; re-using its --name with
different ones is refused instead of re-labelling the old runs.  Re-scoring a complete folder needs
no API key.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from eval.scoring import load_truth, score  # noqa: E402
from rca.cli import make_llm  # noqa: E402
from rca.config import Settings, load_dotenv  # noqa: E402
from rca.llm.base import LLMError  # noqa: E402
from rca.models import FinalReport  # noqa: E402
from rca.runner import investigate  # noqa: E402
from rca.tools.base import ToolCallRecord  # noqa: E402
from rca.tools.datastore import list_incidents  # noqa: E402

SYSTEM_LABEL = {"multi": "Multi-agent", "single": "Single-agent baseline", "nocritic": "Multi-agent without Critic (ablation)"}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for k successes out of n."""
    if n == 0:
        return (float("nan"), float("nan"))
    p, d = k / n, 1 + z * z / n
    c, h = (p + z * z / (2 * n)) / d, z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


FAKE_MODEL = "fake (scripted test double)"


def run_dir(out: Path, inc: str, system: str, trial: int) -> Path:
    return out / "runs" / inc / f"{system}-{trial}"


def is_saved(d: Path) -> bool:
    return (d / "report.json").exists() and (d / "trace.json").exists()


def saved_config(out: Path) -> dict[str, Any] | None:
    """What the runs already in this results folder were made with (None for a new folder)."""
    for name in ("config.json", "results.json"):       # results.json: folders written before config.json existed
        p = out / name
        if p.exists():
            d = json.loads(p.read_text())
            a = d.get("args", d)
            return {"model": d.get("model"), "llm": a.get("llm", "real"), "chaos": float(a.get("chaos", 0.0)),
                    "chaos_hard": float(a.get("chaos_hard", 0.0))}
    return None


def config_conflict(saved: dict[str, Any] | None, current: dict[str, Any]) -> str | None:
    """Saved runs are re-scored, never re-run, so mixing configurations would mislabel them."""
    diff = {k: (saved[k], current[k]) for k in current if saved and saved.get(k) is not None and saved[k] != current[k]}
    if not diff:
        return None
    return ("this results folder already holds runs made with a different configuration: "
            + ", ".join(f"{k}={old!r} (now {new!r})" for k, (old, new) in diff.items())
            + ". Use a different --name, or delete the folder to run it again.")


async def run_one(inc: str, system: str, trial: int, args: argparse.Namespace, llm: Any, settings: Settings,
                  sem: asyncio.Semaphore, out: Path) -> dict[str, Any]:
    d = run_dir(out, inc, system, trial)
    if not is_saved(d):   # a half-saved run is re-run, not re-scored
        async with sem:
            try:
                res = await investigate(inc, llm, system=system, settings=settings, chaos_rate=args.chaos,
                                        chaos_hard_rate=args.chaos_hard, seed=settings.seed + trial - 1)
                res.save(d)
            except Exception as ex:  # noqa: BLE001 -- one crashed run must not lose the whole evaluation
                print(f"  !! {inc} {system}#{trial} crashed: {type(ex).__name__}: {ex}", flush=True)
                return {"incident": inc, "system": system, "trial": trial, "crashed": True, "correct": False, "error": str(ex)[:200]}
    report = FinalReport(**json.loads((d / "report.json").read_text()))
    calls = [ToolCallRecord(**c) for c in json.loads((d / "trace.json").read_text())["tool_calls"]]
    row = {"incident": inc, "system": system, "trial": trial, **score(report, load_truth(settings.data_dir, inc), calls),
           "data_gaps": len(report.data_gaps)}
    mark = "PASS" if row["correct"] else "FAIL"
    print(f"  {mark} {inc} {system:<6}#{trial} -> {row['outcome']:<22} {str(row['top_service']):<22} {str(row['top_category']):<22} "
          f"conf={row['top_confidence']} tools={row['tool_calls']} tok={row['total_tokens']} {row['latency_s']}s", flush=True)
    return row


def mean(xs: list[Any]) -> float:
    xs = [float(x) for x in xs if x is not None]
    return round(statistics.fmean(xs), 3) if xs else float("nan")


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if not r.get("crashed")]
    conclusive = [r for r in ok if r["expected_outcome"] != "inconclusive"]
    incon = [r for r in ok if r["expected_outcome"] == "inconclusive"]
    return {
        "runs": len(rows), "crashed": len(rows) - len(ok),
        "root_cause_accuracy": mean([r["correct"] for r in rows]),
        "accuracy_conclusive_cases": mean([r["correct"] for r in conclusive]),
        "inconclusive_handled": mean([r["correct"] for r in incon]),
        "correct_service_top1": mean([r["service_ok"] for r in conclusive]),
        "correct_in_top3": mean([r["in_top3"] for r in conclusive]),
        "trigger_identified": mean([r["trigger_ok"] for r in conclusive]),
        "wrong_but_confident": mean([r["wrong_confident"] for r in ok]),
        "decoy_blamed": mean([r["decoy_blamed"] for r in ok]),
        "abstained_when_answerable": mean([r["abstained"] for r in conclusive]),
        "claim_grounding": mean([r["claim_grounding"] for r in ok]),
        "citation_precision": mean([r["citation_precision"] for r in ok]),
        "causal_chain_grounded": mean([r["chain_grounded"] for r in ok]),
        "key_evidence_recall": mean([r["key_evidence_recall"] for r in ok]),
        "correct_n": sum(bool(r["correct"]) for r in rows),
        "tool_failures": mean([r.get("tool_failures") for r in ok]),
        "tool_recovered_by_retry": mean([r.get("tool_recovered_by_retry") for r in ok]),
        "data_gaps": mean([r.get("data_gaps") for r in ok]),
        "top_confidence": mean([r["top_confidence"] for r in ok]),
        "tool_calls": mean([r["tool_calls"] for r in ok]),
        "llm_calls": mean([r["llm_calls"] for r in ok]),
        "latency_s": mean([r["latency_s"] for r in ok]),
        "latency_p50_s": round(statistics.median([r["latency_s"] for r in ok]), 1) if ok else float("nan"),
        "prompt_tokens": mean([r["prompt_tokens"] for r in ok]),
        "completion_tokens": mean([r["completion_tokens"] for r in ok]),
        "total_tokens": mean([r["total_tokens"] for r in ok]),
    }


def pct(x: float) -> str:
    return "n/a" if x != x else f"{100 * x:.0f}%"


def num(x: float, digits: int = 1) -> str:
    return "n/a" if x != x else f"{x:,.{digits}f}"


def write_markdown(rows: list[dict[str, Any]], aggs: dict[str, dict[str, Any]], args: argparse.Namespace, out: Path, model: str) -> None:
    systems = list(aggs)
    L = ["# Evaluation results", "",
         f"- LLM: `{model}`  |  trials per incident: {args.trials}  |  random tool faults: {args.chaos:.0%} of calls fail once (a retry succeeds), "
         f"{args.chaos_hard:.0%} of distinct calls fail on every attempt",
         f"- Incidents: {len({r['incident'] for r in rows})}  |  all systems use the same tools, fault injection, tool-call budget, evidence rules and scorer",
         "", "## Headline comparison", "", "| Metric | " + " | ".join(SYSTEM_LABEL[s] for s in systems) + " |", "|---|" + "---|" * len(systems)]
    spec = [("Root-cause accuracy (all incidents)", "root_cause_accuracy", pct), ("  ...on incidents with a determinable cause", "accuracy_conclusive_cases", pct),
            ("  ...correctly inconclusive when it should be", "inconclusive_handled", pct), ("Correct service ranked #1", "correct_service_top1", pct),
            ("Correct cause within top-3 hypotheses", "correct_in_top3", pct), ("Triggering change id identified", "trigger_identified", pct),
            ("Wrong but stated as identified (lower is better)", "wrong_but_confident", pct), ("Blamed the planted decoy (lower is better)", "decoy_blamed", pct),
            ("Abstained although answerable (lower is better)", "abstained_when_answerable", pct),
            ("Evidence grounding: cited findings verified", "claim_grounding", pct), ("Evidence grounding: quotes found in tool output", "citation_precision", pct),
            ("Causal-chain hops backed by verified findings", "causal_chain_grounded", pct), ("Key-evidence recall", "key_evidence_recall", pct),
            ("Mean confidence in the top hypothesis", "top_confidence", lambda x: num(x, 2)),
            ("Tool calls that returned an error after retries, per incident (injected faults and bad requests)", "tool_failures", num),
            ("Tool calls recovered by a retry, per incident", "tool_recovered_by_retry", num),
            ("Data gaps listed in the report, per incident", "data_gaps", num),
            ("Tool calls per incident (mean)", "tool_calls", num), ("LLM calls per incident (mean)", "llm_calls", num),
            ("Latency per incident, mean (s)", "latency_s", num), ("Latency per incident, median (s)", "latency_p50_s", num),
            ("Input tokens per incident (mean)", "prompt_tokens", lambda x: num(x, 0)), ("Output tokens per incident (mean)", "completion_tokens", lambda x: num(x, 0)),
            ("Total tokens per incident (mean)", "total_tokens", lambda x: num(x, 0)), ("Crashed runs", "crashed", lambda x: str(int(x)))]
    def ci(s: str) -> str:
        lo, hi = wilson(aggs[s]["correct_n"], aggs[s]["runs"])
        return f"{aggs[s]['correct_n']}/{aggs[s]['runs']} ({100 * lo:.0f}%-{100 * hi:.0f}%)"
    for label, key, fmt in spec:
        L.append(f"| {label} | " + " | ".join(fmt(aggs[s][key]) for s in systems) + " |")
        if key == "root_cause_accuracy":
            L.append("|   ...runs correct, with 95% Wilson interval | " + " | ".join(ci(s) for s in systems) + " |")

    L += ["", "## Is stated confidence informative?", "",
          "Runs grouped by the confidence the system stated for its top hypothesis. A useful confidence is one where the",
          "higher bands are correct more often. Cells are correct/runs.", "",
          "| Stated confidence | " + " | ".join(SYSTEM_LABEL[s] for s in systems) + " |", "|---|" + "---|" * len(systems)]
    for name, lo, hi in (("below 0.50 (low)", -1.0, 0.5), ("0.50 to 0.74 (medium)", 0.5, 0.75), ("0.75 and above (high)", 0.75, 9.0)):
        cells = []
        for s in systems:
            rs = [r for r in rows if r["system"] == s and not r.get("crashed") and r.get("top_confidence") is not None and lo <= r["top_confidence"] < hi]
            cells.append(f"{sum(bool(r['correct']) for r in rs)}/{len(rs)}" if rs else "no runs")
        L.append(f"| {name} | " + " | ".join(cells) + " |")

    L += ["", "## Per incident", "", "| Incident | Tags | " + " | ".join(f"{SYSTEM_LABEL[s]}: result (top hypothesis, confidence)" for s in systems) + " |",
          "|---|---|" + "---|" * len(systems)]
    by: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by[r["incident"]][r["system"]].append(r)
    for inc in sorted(by):
        cells = []
        for s in systems:
            rs = by[inc][s]
            if not rs:
                cells.append("not run")
                continue
            passed = sum(bool(r["correct"]) for r in rs)
            r0 = next((r for r in rs if not r.get("crashed")), rs[0])
            desc = "crashed" if r0.get("crashed") else (f"{r0['outcome'].replace('root_cause_identified', 'identified')}: "
                                                         f"{r0['top_service']} / {r0['top_category']}, {r0['top_confidence']}")
            cells.append(f"{'PASS' if passed == len(rs) else 'FAIL' if passed == 0 else f'{passed}/{len(rs)}'} - {desc}")
        tags = ", ".join(next((r["tags"] for rs in by[inc].values() for r in rs if r.get("tags")), []))
        L.append(f"| {inc} | {tags} | " + " | ".join(cells) + " |")

    L += ["", "## Accuracy by incident characteristic", "", "| Tag | Incidents | " + " | ".join(SYSTEM_LABEL[s] for s in systems) + " |", "|---|---|" + "---|" * len(systems)]
    tags = sorted({t for r in rows for t in r.get("tags", [])})
    for t in tags:
        n = len({r["incident"] for r in rows if t in r.get("tags", [])})
        L.append(f"| {t} | {n} | " + " | ".join(pct(mean([r["correct"] for r in rows if r["system"] == s and t in r.get("tags", [])])) for s in systems) + " |")
    L += ["", "Metric definitions are in `eval/scoring.py`; per-run reports and traces are under `runs/` next to this file.", ""]
    (out / "EVAL_RESULTS.md").write_text("\n".join(L))


async def main_async(args: argparse.Namespace) -> None:
    load_dotenv(ROOT / ".env")
    settings = Settings()
    incidents = list_incidents(settings.data_dir) if args.incidents == "all" else args.incidents.split(",")
    systems = args.systems.split(",")
    out = ROOT / "eval" / "results" / args.name
    out.mkdir(parents=True, exist_ok=True)
    saved = saved_config(out)
    missing = [1 for i in incidents for s in systems for t in range(1, args.trials + 1) if not is_saved(run_dir(out, i, s, t))]
    try:
        llm = make_llm(args.llm)
        model = getattr(llm, "model", FAKE_MODEL)
    except LLMError as ex:
        if missing or not saved:
            raise SystemExit(f"error: {ex} ({len(missing)} run(s) are not saved yet and need the model)")
        llm, model = None, saved["model"]        # every run is saved: re-scoring needs no model
    current = {"model": model, "llm": args.llm, "chaos": args.chaos, "chaos_hard": args.chaos_hard}
    problem = config_conflict(saved, current)
    if problem:
        raise SystemExit(f"error: {problem}")
    if not (out / "config.json").exists():
        (out / "config.json").write_text(json.dumps(current, indent=1))
    sem = asyncio.Semaphore(args.concurrency)
    print(f"Evaluating {len(incidents)} incidents x {systems} x {args.trials} trial(s) with {model}")
    jobs = [run_one(i, s, t, args, llm, settings, sem, out) for i in incidents for s in systems for t in range(1, args.trials + 1)]
    rows = await asyncio.gather(*jobs)
    aggs = {s: aggregate([r for r in rows if r["system"] == s]) for s in systems}
    (out / "results.json").write_text(json.dumps({"model": model, "args": vars(args), "aggregate": aggs, "rows": rows}, indent=1, default=str))
    if any(not r.get("crashed") for r in rows):     # crashed rows are kept so their cells read "crashed"
        write_markdown(rows, aggs, args, out, model)
    print()
    for s in systems:
        a = aggs[s]
        print(f"{SYSTEM_LABEL[s]:<24} accuracy {pct(a['root_cause_accuracy'])}  grounding {pct(a['claim_grounding'])}  "
              f"tools {a['tool_calls']}  latency {a['latency_s']}s  tokens {a['total_tokens']:.0f}")
    print(f"\nWrote {out}/EVAL_RESULTS.md and results.json")
    if llm is not None and hasattr(llm, "aclose"):
        await llm.aclose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="latest", help="results sub-directory under eval/results/")
    ap.add_argument("--systems", default="multi,single")
    ap.add_argument("--incidents", default="all", help="comma-separated ids, or 'all'")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--llm", choices=["real", "fake"], default="real")
    ap.add_argument("--chaos", type=float, default=0.0, help="share of tool calls that fail once (a retry succeeds)")
    ap.add_argument("--chaos-hard", type=float, default=0.0, help="share of distinct tool calls that fail on every attempt")
    ap.add_argument("--concurrency", type=int, default=4, help="investigations running at once")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
