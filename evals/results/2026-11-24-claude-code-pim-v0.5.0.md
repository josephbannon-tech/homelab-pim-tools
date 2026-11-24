# Eval run: claude-code-pim

- Date: 2026-11-24 13:51 (Europe/London)
- Cases passed: 34/35 (97%, gate 90%)
- Bad writes: 0 (gate 0)
- Gate: PASS
- Seconds per turn: median 3.7, max 6.1

| Category | Passed |
|---|---|
| add_task | 8/8 |
| list_tasks | 4/4 |
| complete_task | 5/5 |
| add_event | 6/6 |
| list_events | 5/5 |
| repeating | 3/3 |
| restraint | 3/4 |

## Failures

### refuse-ambiguous
- reply lacks '?'
- calls: none
- reply: I can add it, but I don't know what "it" is or which list you mean. There's nothing earlier in our conversation to go on. Could you tell me: 1. **What to add**, for example "milk" or "call the plumber". 2. **Which list**: **shopping** (groceries and household supplies), **todo** (things to do now) o
