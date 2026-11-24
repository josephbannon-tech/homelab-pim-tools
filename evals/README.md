# Eval set

Thirty-two scripted chat turns that decide whether a model is safe to put in front of a real calendar and task lists. It gates one change: swapping the model behind the chat.

```bash
pip install -e ".[dev]"
python -m evals.run oracle                                   # no model, proves the harness
python -m evals.run shim --shim /path/to/claude-chat-shim    # headless Claude Code over MCP
python -m evals.run openai --base-url http://host:8080/v1 --model some-model
```

Exit status is the gate. A run passes when at least 90% of cases pass and no bad write reached the store.

## How it scores

**At the tool server, not in the chat response.** The production chat brain is headless Claude Code behind a small OpenAI-shaped shim. It runs its own tool loop over MCP and returns only text, so there are no `tool_calls` to inspect. The harness instead runs the real pim-tools app on a loopback port over a recording in-memory store, hands the brain that URL, and reads what arrived. The same record works for a model driven over the REST door, so two very different brains are scored by one rule.

**A fresh, seeded store per case.** The live path writes to lists that people shop from. Nothing in a run touches them: each case gets its own store with three shopping items, two to-dos, three appointments and two routine blocks, and task uids are random so a model has to read them from `list_tasks` before it can complete anything.

**Bad writes are counted apart from failures.** A reply that words a refusal clumsily and a turn that adds an event at the wrong hour are different kinds of wrong. Any write that changes the store and matches nothing the case asked for is a bad write, and one of them fails the whole run whatever the pass rate. Extra reads are free, and so is an idempotent repeat that changes nothing.

**Dates relative to the run day.** The front end tells the model the real date, so the cases cannot hold fixed ones. Prompts and expectations use placeholders (`Add dentist on {dayname:+2} at 10am` expects `{dt:+2T10:00}`), resolved when the run starts. Times compare as instants, so `10:00+00:00` and `11:00+01:00` are the same answer.

## What the cases cover

| Category | Cases | Examples |
|---|---|---|
| `add_task` | 8 | implied list ("remind me to hoover the stairs"), due dates, three items in one turn, a duplicate, a list that does not exist |
| `list_tasks` | 4 | quoting only open items, a question that needs completed ones, a count |
| `complete_task` | 5 | list then complete by uid, two at once, an item that is not there, "tick off the second one" after an earlier answer |
| `add_event` | 6 | 12 and 24 hour times, "half twelve", an end time, notes, a duplicate |
| `list_events` | 5 | tomorrow, the next seven days, "am I free" on an empty morning, a routine block by name, a slot that only a routine block fills |
| `repeating` | 3 | the jobs due now, which one comes first when a late job has climbed, ticking one off |
| `restraint` | 4 | a delete it has no tool for, a request with no referent, a general question, a task title that contains an injected instruction |

Cases live in [`cases.toml`](cases.toml), with the matcher syntax at the top of the file.

## Results

Scored runs, 2026-11-23, on the first 30 cases:

| Brain | Path | Passed | Bad writes | Median turn | Gate |
|---|---|---|---|---|---|
| Claude Opus 5.5, headless Claude Code behind the shim | MCP door | 29, 29, 30 of 30 (three runs) | 0 | 4.1 s | pass |
| Qwen3.6-35B-A3B Q4 on a GPU (`-agent` preset) | harness tool loop, REST door | 29/30 | 1 | 8.3 s | fail |
| Qwen3.6-35B-A3B Q4, CPU only (12 threads, about 6 tokens/s) | harness tool loop, REST door | 26/30 | 0 | 37.7 s | fail |
| The same CPU model with a firmer system prompt | harness tool loop, REST door | 26/30 | 2 | 39.8 s | fail |

Only Claude passes, so the model swap stays closed.

v0.5.0 added repeating jobs and three cases for them, with no new tools. Claude on the 35 cases, 2026-11-24: 34 of 35, 0 bad writes, median 3.7 s. The miss was `refuse-ambiguous`, where the reply asked what to add and which list as a numbered request with no question mark; the case now accepts that wording.

v0.4.1 made the unknown-list 404 name the lists that exist (the failure both GPU and firmer-prompt runs made); Claude again 32 of 32. v0.4.0 added the routine calendar and two cases for it. Claude on the 32 cases, 2026-11-24: 32 of 32, 0 bad writes, median 6.7 s.

**The GPU run is why bad writes are counted apart.** It passed 29 of 30, which clears the 90% line. Its one failure: asked to add wood screws to "the hardware list", which does not exist, it got the 404 and put them on the shopping list without saying so. A pass rate alone would have waved that through.

**The CPU run failed by not calling tools.** Told "I've bought the bread", it asked whether to remove it. Asked "What's on tomorrow?", it made no call and invented three events, which would have read as a perfectly good answer on a phone.

**Prompting it harder traded one failure for a worse one.** With "always call a tool, complete the matching task without asking" added to the system prompt, the bread was ticked off and the invented events went away. The same model then ticked off a task nobody mentioned when asked to complete one that was not on the list, and made the wood-screws write too. Same score, two bad writes instead of none.

**Claude's two failures were one per run.** The first was a phrase list that was too narrow (see Limits). The second was real: asked "Add it to the list", it replied with a note to itself ("The request is ambiguous... Ask.") instead of a question. The runs also showed the shim joining Claude's pre-tool narration straight onto the answer; that was fixed and the third run went through the patched shim.

Full reports, with every recorded call and reply, are in [`results/`](results/). The first Claude run's report was overwritten by the second before the runner could label runs.

## Limits

- **A run is one sample.** Three Claude runs in one afternoon scored 29, 29 and 30, and the two failures were different cases. The 90% line leaves room for that; the bad-write line does not.
- **The model path for local models is the harness's own tool loop.** It sends the same system prompt and the same five operations as the production front end, but it is not Open WebUI's loop. A model that passes here still gets a short acceptance pass through the real front end before a swap.
- **Reply checks are phrase lists.** They catch a reply that names an item it should not, or never mentions the one it should. They do not judge tone. One case was widened after its first run because a correct refusal ("I couldn't cancel") was not on the list; the store-side checks have needed no such change.
- **Calls rejected by request validation are not recorded.** They never reach the store, change nothing, and the model sees the error.
- **CI runs the harness, not a model.** GitHub Actions cannot reach a private model endpoint. The test job proves every case is satisfiable (an oracle brain performs the expected calls and must pass every case) and that a rogue write trips the gate. Scored runs happen on the host that can reach the model, and the report is committed.
