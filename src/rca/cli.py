"""CLI:  rca investigate INC-003 [--system multi|single] [--llm real|fake] [-v]
          rca list
          rca serve [--port 8000]     live streaming web UI"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .assemble import render_markdown
from .config import Settings, load_dotenv
from .llm.base import LLMError
from .runner import investigate
from .tools.datastore import DataStore, list_incidents


def make_llm(kind: str):  # type: ignore[no-untyped-def]
    if kind == "fake":
        from .llm.fake import FakeLLM
        return FakeLLM()
    from .llm.openai_compat import OpenAICompatClient
    return OpenAICompatClient()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()                                              # the directory the command is run from
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")  # the project root, when run from elsewhere
    ap = argparse.ArgumentParser(prog="rca", description="Multi-agent production incident investigator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list incidents in the dataset")
    inv = sub.add_parser("investigate", help="investigate one incident")
    inv.add_argument("incident_id")
    inv.add_argument("--system", choices=["multi", "single"], default="multi")
    inv.add_argument("--llm", choices=["real", "fake"], default="real")
    inv.add_argument("--chaos", type=float, default=0.0, help="extra random transient tool-fault rate (0-1)")
    inv.add_argument("--agent-fault", action="append", default=[], metavar="ROLE:N", help="crash ROLE's first N invocations")
    inv.add_argument("--out", type=Path, default=None, help="directory for report.md / report.json / trace.json")
    inv.add_argument("-v", "--verbose", action="store_true", help="stream the investigation trace")
    srv = sub.add_parser("serve", help="web UI that streams an investigation live (or replays a saved run)")
    srv.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)

    settings = Settings(verbose=getattr(args, "verbose", False))
    if args.cmd == "list":
        for i in list_incidents(settings.data_dir):
            inc = DataStore(settings.data_dir, i).incident
            print(f"{i}  [{inc.severity}] {inc.title}  (reported on {inc.affected_service})")
        return 0

    if args.cmd == "serve":
        from .ui.server import serve
        serve(args.port)
        return 0

    try:
        faults = {r: int(n) for r, n in (x.split(":") for x in args.agent_fault)}
    except ValueError:
        print(f"error: --agent-fault expects ROLE:N (for example critic:1), got {args.agent_fault}", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(investigate(args.incident_id, make_llm(args.llm), system=args.system, settings=settings,
                                         chaos_rate=args.chaos, agent_faults=faults))
    except (LLMError, FileNotFoundError) as ex:     # no API key, or an unknown incident id: one line, not a traceback
        print(f"error: {ex}", file=sys.stderr)
        return 2
    out = args.out or Path("runs") / f"{args.incident_id}-{args.system}"
    result.save(out)
    print(render_markdown(result.report))
    print(f"(saved to {out}/)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
