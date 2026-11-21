# Coding-assistant transcript

This project was built with Claude Code (Anthropic's CLI coding assistant) in two sessions.

- [`TRANSCRIPT.md`](TRANSCRIPT.md) is the main session: every user message and assistant reply
  verbatim, every tool call (commands run, files written) and its result. It runs from the first
  reading of the assignment to the push of the third code review's changes.
- [`TRANSCRIPT_REVIEW_SESSION.md`](TRANSCRIPT_REVIEW_SESSION.md) is a second, shorter session that
  ran in parallel on 21 Nov: the third code review and three small changes to the web UI.
- Not included: the turns after that push in the main session. They changed no code (only a README screenshot and the demo). They cover
  recording the demo video, a recording guide, and preparing the submission email, and they contain
  mailbox and chat-workspace lookups. Also not included: the final packaging turns that committed
  the review session's two UI changes, exported these transcripts, and squashed the history.
  The review session ends with those UI changes made but not yet committed; they are in this
  repository's commit.
- [`export_transcript.py`](export_transcript.py) is the script that produced it from the raw session
  log, so the conversion is reproducible:
  `python export_transcript.py <session>.jsonl TRANSCRIPT.md [.env] [word,word]`.
- The repository's history was squashed to a single commit before submission (an early export of the
  transcript had a redaction slip). Commit ids quoted inside the transcript are from before that
  squash, so they no longer resolve in this repository.

What the script changes, and nothing else:

- Tool inputs longer than 4,000 characters and tool results longer than 1,500 characters are
  truncated, with a marker saying how much was omitted.
- API keys and e-mail addresses are replaced with a placeholder.
- Text injected by the tool itself rather than typed by a person (reminder blocks carrying the
  assistant's persistent notes about unrelated work) is removed, as are one read of such a note and
  one listing of unrelated directories. The employer's and developer's names and a few words
  naming unrelated personal matters are replaced with `[private]`.
- Results of mailbox lookups (the assistant read the assignment email to check the submission
  instructions) are replaced with a one-line marker; the calls themselves are kept.
- The assistant's context was compacted part-way through the session; the automatic summary that
  replaced the earlier context appears in the transcript where it happened.
- The lists of private words passed to the export script, where they appear inside the transcript
  itself, are replaced with a marker, and the local machine's user name is replaced with `[private]`.

## Where the direction came from

The assistant wrote the code and the documents; the author directed the work and decided what
shipped. The pattern across both sessions: scope the task and require research before any design
(Main 1); demand an independent code review before calling anything done — three times (Main 7,
Main 38, Review 1); walk through the entire system part by part rather than submit anything
unexamined (Main 9, 16–26); hold the evaluation to the standard of the author's earlier project,
which is what forced the ablation, the fault stress run and the confidence intervals (Main 27–30);
and cut the UI back to the assignment's actual scope when it drifted (Review 2–7). The turns below
are where those requests set or changed the direction of the work. Turn numbers are the
`## User (n)` headings.

| Turn | Request | What followed |
|---|---|---|
| Main 1 | Complete the assignment, with research first | Design, implementation, dataset, tests, first evaluation round |
| Main 3 | Submit the AI transcript as a deliverable; check everything | This export and its script |
| Main 7 | Run a code review and fix what it finds | First review; fixes; evaluation re-run as `v3` |
| Main 9, 16 to 26 | Explain the problem statement and the whole system, part by part | Ten-part walkthrough; a side question on how confidence is computed (turn 20) |
| Main 12 | Build a streaming web UI, modelled on the author's earlier project | `rca serve`: live event stream and replay of saved runs |
| Main 27, 29 | Question the evaluation; compare it against the earlier project's evaluation | Gap list: no ablation, no fault stress run, no error bars |
| Main 30 | Close those gaps on both systems | No-Critic ablation, fault stress run, intervals, confidence table, `docs/EVAL.md`, round `v4` |
| Main 38, 39 | Check the second session's review report, then fix and push | Claims verified by independent replay; one slip fixed |
| Review 1 | A further code review of the submission | Third review: outcome rule, scorer and harness fixes |
| Review 2, 5 | The replay picker lists too many runs | Picker narrowed to run set, incident and recording |
| Review 6, 7 | Remove the run limits and the offline-script option from the UI, as neither is in the assignment | Both removed from the page |
