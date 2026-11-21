"""Render the captured terminal session to an MP4 (PIL frames encoded with OpenCV). Idle gaps are shortened."""
import json, subprocess, sys, textwrap
from PIL import Image, ImageDraw, ImageFont
W, H, FPS = 1600, 900, 8
COLS, ROWS = 150, 38
font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 17)
bold = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 22, index=1)
small = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 15)
LH, X0, Y0 = 21, 22, 84
BG, FG, DIM, GREEN, CAPBG, AMBER = (16, 18, 24), (214, 218, 226), (120, 128, 142), (126, 211, 138), (34, 40, 54), (240, 190, 100)
MAX_IDLE, STEP_CAP = 1.0, float(sys.argv[3]) if len(sys.argv) > 3 else 80.0

steps = json.load(open(sys.argv[1]))
import cv2, numpy as np
vw = cv2.VideoWriter(sys.argv[2], cv2.VideoWriter_fourcc(*"avc1"), FPS, (W, H))
assert vw.isOpened(), "video writer could not be opened"
total = 0
def frame(caption, lines, note="", n=1):
    global total
    im = Image.new("RGB", (W, H), BG); d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, 62], fill=CAPBG)
    d.text((X0, 16), caption, font=bold, fill=(255, 255, 255))
    if note:
        d.text((W - 22 - d.textlength(note, font=small), 24), note, font=small, fill=AMBER)
    for i, ln in enumerate(lines[-ROWS:]):
        col = GREEN if ln.startswith("$ ") else FG
        d.text((X0, Y0 + i * LH), ln, font=font, fill=col)
    b = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
    for _ in range(n):
        vw.write(b); total += 1

def wrap(text):
    return [ln if len(ln) <= COLS else ln[:COLS - 1] + "…" for ln in text.replace("\r", "").split("\n")]

def expand(events):
    """Output that arrives in one burst is revealed line by line so it can be read."""
    res, shift = [], 0.0
    for t, data in events:
        parts = data.splitlines(keepends=True)
        if len(parts) <= 3:
            res.append([t + shift, data]); continue
        for p in parts:
            res.append([t + shift, p]); shift += 0.1
    return res

# title card
im_lines = ["", "", "  Incident Investigator", "", "  A multi-agent production incident investigation system",
            "  Orchestrator, Observability, Dependency, Reasoning, Critic, Reporting  +  single-agent baseline", "",
            "  Everything below is an unedited recording of real commands; waiting time is shortened."]
frame("Demo", im_lines, n=FPS * 4)

for i, st in enumerate(steps, 1):
    cap = f"{i}/{len(steps)}  {st['caption']}"
    lines = []
    for k in range(0, len(st["cmd"]) + 1, 3):                     # typed command
        frame(cap, ["$ " + st["cmd"][:k] + "_"], n=1)
    lines = ["$ " + st["cmd"]]
    frame(cap, lines, n=FPS // 2)
    # compress the timeline: idle gaps capped, whole step capped by uniform speed-up
    ev, gaps, prev = expand(st["events"]), [], 0.0
    for t, _ in ev:
        gaps.append(min(t - prev, MAX_IDLE)); prev = t
    speed = max(1.0, sum(gaps) / STEP_CAP)
    shown = sum(gaps) / speed
    real = st["duration"]
    note = f"real duration {real:.0f} s, shown in {shown:.0f} s" if real > shown + 2 else ""
    buf, acc = "", 0.0
    for (t, data), g in zip(ev, gaps):
        acc += g / speed
        n = int(acc * FPS)
        if n:
            frame(cap, lines, note, n=n); acc -= n / FPS
        buf += data
        lines = ["$ " + st["cmd"]] + wrap(buf.rstrip("\n"))
    frame(cap, lines, note, n=int(FPS * (4 if len(lines) > 12 else 2.5)))
frame("End", ["", "", "  Repository: README.md (setup, results), docs/ARCHITECTURE.md, docs/TECHNICAL_NOTE.md",
              "  Evaluation: eval/results/v2/EVAL_RESULTS.md      Transcript: transcript/TRANSCRIPT.md"], n=FPS * 4)
vw.release()
print("frames", total, "seconds", total / FPS)
