# Demo video

[`demo.mp4`](demo.mp4) is a silent screen recording of the system being run from the terminal. The
caption bar at the top says what each part shows.

It is a rendering of real command output: `capture.py` runs the commands and records what they print
with timestamps, and `render.py` draws that output as video frames. The text is not edited; lines wider than the screen are cut off with an ellipsis. Waiting time
is shortened (the real duration of each command is shown top right), because a real multi-agent
investigation takes two to four minutes. The video is three minutes long and has no narration.

| Part | Command | What to look for |
|---|---|---|
| 1 | `pytest -q` | The offline test suite |
| 2 | `rca list` | The 12 synthetic incidents |
| 3 | `rca investigate INC-009 -v` | A real multi-agent run: plan, parallel specialist tasks, findings verified against tool output, hypotheses, Critic review, feedback round, outcome |
| 4 | `head -n 70 runs/INC-009-multi/report.md` | The report: ranked hypotheses, confidence, facts and assumptions, actions |
| 5 | `rca investigate INC-009 --system single` | The single-agent baseline on the same incident |
| 6 | `rca investigate INC-011 -v` | A config-history source returns incomplete data (`partial=True`); the Critic asks for more evidence and a second round runs before it accepts |
| 7 | `rca investigate INC-001 --llm fake --agent-fault observability:1 -v` | An agent crash (scripted offline model): the task is retried and the run continues |
| 8 | `head -n 30 eval/results/v3/EVAL_RESULTS.md` | Multi-agent against the baseline |

Parts 3, 5 and 6 call the real model (`deepseek-ai/DeepSeek-V4.1-Flash`). Part 7 uses the scripted
test double, so its hypotheses are canned; it is there to show the recovery path only.

To record again: `python demo/capture.py demo/capture.json && python demo/render.py demo/capture.json demo/demo.mp4`
(needs Pillow and OpenCV: `pip install pillow opencv-python`). `capture.json` is the recording this video was rendered from.

## Web UI recording

[`ui-demo.mp4`](ui-demo.mp4) is a 68-second silent recording of the web UI (`rca serve`) in headless Chrome.
It shows a replay of a recorded real-model run of INC-011 (tasks, tool calls, evidence ledger, hypothesis
board, cross-links, final report), then a live run of INC-003 on the offline script with 30% tool faults
and the Observability Agent crashed once.
