"""Mechanical grounding: a finding counts as *verified* only if a quote it cites literally
appears in the output of the tool call it cites.  No LLM is involved, so a model cannot talk
its way into a 'fact'."""
from __future__ import annotations

import re
from typing import Callable, Optional

from .models import EvidenceRef, Finding, FindingIn
from .tools.base import ToolCallRecord

_ALNUM = re.compile(r"[^a-z0-9]+")
MIN_QUOTE_CHARS = 4


def normalize(text: str) -> str:
    """Lower-case and drop everything but letters/digits, so JSON escaping, whitespace and
    punctuation differences cannot cause false negatives."""
    return _ALNUM.sub("", text.lower())


def check_ref(ref: EvidenceRef, lookup: Callable[[str], Optional[ToolCallRecord]]) -> tuple[bool, str]:
    rec = lookup(ref.call_id.strip())
    if rec is None:
        return False, f"{ref.call_id}: no such tool call"
    q = normalize(ref.quote)
    if len(q) < MIN_QUOTE_CHARS:
        return False, f"{ref.call_id}: quote too short to verify"
    if q in normalize(rec.output):
        return True, f"{ref.call_id}: ok"
    return False, f"{ref.call_id}: quote not found in tool output"


def verify_finding(fid: str, agent: str, task_id: str | None, f: FindingIn,
                   lookup: Callable[[str], Optional[ToolCallRecord]]) -> Finding:
    results = [check_ref(r, lookup) for r in f.refs]
    ok = sum(1 for good, _ in results if good)
    note = "no evidence refs" if not results else f"{ok}/{len(results)} refs verified" + (
        "" if ok == len(results) else " (" + "; ".join(n for good, n in results if not good) + ")")
    return Finding(**f.model_dump(), id=fid, agent=agent, task_id=task_id, verified=ok > 0, verification_note=note)


def ref_stats(findings: list[Finding], lookup: Callable[[str], Optional[ToolCallRecord]]) -> tuple[int, int]:
    """(verified refs, total refs) across findings -- the citation-precision numerator/denominator."""
    good = total = 0
    for f in findings:
        for r in f.refs:
            total += 1
            good += check_ref(r, lookup)[0]
    return good, total
