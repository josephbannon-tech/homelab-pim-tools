# Eval run: claude-code-pim-v0.8.0

- Date: 2026-11-26 09:02 (Europe/London)
- Cases passed: 54/55 (98%, gate 90%)
- Bad writes: 0 (gate 0)
- Gate: PASS
- Seconds per turn: median 4.7, max 18.9

| Category | Passed |
|---|---|
| add_task | 8/8 |
| list_tasks | 4/4 |
| complete_task | 5/5 |
| add_event | 6/6 |
| list_events | 5/5 |
| change_event | 5/5 |
| notes | 3/3 |
| repeating | 3/3 |
| restraint | 9/9 |
| recipes | 6/7 |

## Failures

### recipe-search
- reply mentions 'roast chicken'
- calls: list_recipes, list_recipes
- reply: Only one of your recipes is tagged as quick (under 30 minutes): - **Cod Curry**: Indian, fish, about 520 kcal per serving, serves 2 Your other two recipes take longer: - **Lamb Keema with Rice** is standard (45–60 minutes). - **Sunday Roast Chicken** is a weekend recipe (more than an hour).
