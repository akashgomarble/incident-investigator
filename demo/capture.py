"""Run the demo commands for real and record their output with timestamps."""
import json, os, subprocess, sys, time, selectors
STEPS = [
 ("Offline test suite: 66 tests, no network, no API key", "pytest -q"),
 ("The synthetic dataset: 12 incidents", "rca list"),
 ("Multi-agent investigation on a real model (noisy-neighbour incident, symptom is not at the cause)", "rca investigate INC-009 -v"),
 ("The report: ranked hypotheses, facts vs assumptions, confidence, actions", "head -n 70 runs/INC-009-multi/report.md"),
 ("Single-agent baseline: same incident, same tools", "rca investigate INC-009 --system single"),
 ("Incomplete tool results and a Critic feedback round (data sources return partial data)", "rca investigate INC-011 -v"),
 ("Agent crash drill (scripted offline model): the task is retried and the run continues", "rca investigate INC-001 --llm fake --agent-fault observability:1 -v"),
 ("Evaluation: multi-agent vs single-agent baseline, 12 incidents x 3 trials", "head -n 30 eval/results/v3/EVAL_RESULTS.md"),
]
env = dict(os.environ, PYTHONUNBUFFERED="1", PATH=os.path.abspath(".venv/bin") + ":" + os.environ["PATH"], NO_COLOR="1", COLUMNS="150")
out = []
for caption, cmd in STEPS:
    t0 = time.time()
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    ev = []
    os.set_blocking(p.stdout.fileno(), False)
    sel = selectors.DefaultSelector(); sel.register(p.stdout, selectors.EVENT_READ)
    while True:
        if sel.select(timeout=0.2):
            data = os.read(p.stdout.fileno(), 65536)
            if not data:
                break
            ev.append([round(time.time() - t0, 3), data.decode("utf-8", "replace")])
        elif p.poll() is not None:
            rest = p.stdout.read()
            if rest: ev.append([round(time.time() - t0, 3), rest.decode("utf-8", "replace")])
            break
    p.wait()
    out.append({"caption": caption, "cmd": cmd, "events": ev, "duration": round(time.time() - t0, 2), "exit": p.returncode})
    json.dump(out, open(sys.argv[1], "w"))
    print(cmd, "->", p.returncode, round(time.time() - t0, 1), "s", sum(len(e[1]) for e in ev), "chars", flush=True)

