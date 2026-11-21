"""Convert a Claude Code session log (.jsonl) into a readable, redacted Markdown transcript.
Keeps: user messages, assistant replies, tool calls (inputs) and truncated tool results.
Drops: harness-injected reminder blocks (they carry unrelated private notes), internal bookkeeping records.
Redacts: API keys, e-mail addresses."""
import json, re, sys

src, dst = sys.argv[1], sys.argv[2]
# optional 3rd argument: a .env file whose values are removed wherever they appear
SECRETS = []
if len(sys.argv) > 3:
    for ln in open(sys.argv[3]):
        k, _, v = ln.strip().partition("=")
        if "KEY" in k and len(v) >= 8:
            SECRETS.append(v.strip("'\""))
PRIVATE_WORDS = [w for w in (sys.argv[4].split(",") if len(sys.argv) > 4 else []) if w]
private_ids = set()      # tool calls whose results are unrelated private material
mail_ids: set = set()
MAX_RESULT, MAX_INPUT = 1500, 4000

def redact(s: str) -> str:
    for sec in SECRETS:
        s = s.replace(sec, "[REDACTED]")
    for w in PRIVATE_WORDS:
        s = re.sub(re.escape(w), "[private]", s, flags=re.I)
    s = re.sub(r"<system-reminder>.*?</system-reminder>", "", s, flags=re.S)
    s = re.sub(r'(API_KEY"?\s*[:=]\s*"?)[A-Za-z0-9_\-]{16,}', r"\1[REDACTED]", s)
    s = re.sub(r"\b(sk-[A-Za-z0-9_\-]{8,})", "[REDACTED]", s)
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email redacted]", s)
    return s.strip()

def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"\n... [{len(s) - n} more characters omitted]"

def fence(s: str) -> str:
    return "````text\n" + s.replace("````", "` ` ` `") + "\n````"

out = ["# Coding-assistant transcript", "",
       "Tool: Claude Code (Anthropic CLI). One session; the assistant's context was compacted part-way, so a "
       "summary record appears in the middle. Generated from the raw session log by a script: user messages and "
       "assistant replies are verbatim; tool inputs and results are truncated; API keys, e-mail addresses and "
       "harness-injected reminder blocks (unrelated private notes) are removed.", ""]
turn = 0
for line in open(src):
    o = json.loads(line)
    t = o.get("type")
    if t not in ("user", "assistant") or o.get("isSidechain") or o.get("isMeta"):
        continue
    content = (o.get("message") or {}).get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
    for b in blocks:
        bt = b.get("type")
        if bt == "text":
            txt = redact(b.get("text", ""))
            if not txt:
                continue
            if t == "user" and (txt.startswith("<task-notification>") or txt.startswith("[SYSTEM NOTIFICATION")):
                out += ["### System notification (a background command finished; not typed by the user)", "", fence(clip(txt, 600)), ""]
            elif t == "user":
                turn += 1
                label = "Context summary (automatic compaction)" if o.get("isCompactSummary") else f"User ({turn})"
                out += [f"## {label}", "", txt, ""]
            else:
                out += ["### Assistant", "", txt, ""]
        elif bt == "tool_use":
            if "/.claude/" in json.dumps(b.get("input", {})) and "/memory/" in json.dumps(b.get("input", {})):
                private_ids.add(b.get("id"))
            if str(b.get("name", "")).startswith("mcp__claude_ai_Gmail"):
                mail_ids.add(b.get("id"))
            inp = redact(json.dumps(b.get("input", {}), indent=1, ensure_ascii=False).replace("\\n", "\n"))
            out += [f"**Tool call: {b.get('name')}**", "", fence(clip(inp, MAX_INPUT)), ""]
        elif bt == "tool_result":
            c = b.get("content")
            if isinstance(c, list):
                c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
            c = str(c or "")
            if b.get("tool_use_id") in private_ids:
                c = "[omitted: the assistant's private memory note, unrelated to this assignment]"
            elif b.get("tool_use_id") in mail_ids:
                c = "[omitted: contents of the developer's mailbox (the assignment email and unrelated mail)]"
            elif sum(1 for ln in c.splitlines() if ln.strip().endswith("/")) > 15:
                c = "[omitted: listing of unrelated directories on the developer's machine]"
            out += ["Tool result:", "", fence(clip(redact(c), MAX_RESULT)), ""]
open(dst, "w").write("\n".join(out))
print("written", dst, "turns", turn)
