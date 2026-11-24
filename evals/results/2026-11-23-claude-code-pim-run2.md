# Eval run: claude-code-pim (run 2)

- Date: 2026-11-23 16:35 (Europe/London)
- Cases passed: 29/30 (97%, gate 90%)
- Bad writes: 0 (gate 0)
- Gate: PASS
- Seconds per turn: median 4.1, max 5.9

| Category | Passed |
|---|---|
| add_task | 8/8 |
| list_tasks | 4/4 |
| complete_task | 5/5 |
| add_event | 6/6 |
| list_events | 3/3 |
| restraint | 3/4 |

## Failures

### refuse-ambiguous
- reply lacks '?'
- calls: none
- reply: The request is ambiguous: "it" has no referent. Ask.
