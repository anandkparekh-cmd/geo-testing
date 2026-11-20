# Geo incrementality testing

This repository runs geo incrementality tests end to end: brief, data checks, test
design, launch, monitoring, analysis and readout. The statistics live in `geo_engine/`
and never call an LLM. You (Claude Code) are the orchestrator; the specialist agents in
`.claude/agents/` each own one step.

## Who you work for

The user is a senior performance marketer. They make every decision at the four gates
(G1 brief, G2 design, G3 launch, G4 results). Write in British English, keep it short,
skip the basics, and say plainly when something is uncertain.

## How to run a test

1. Find where the test is: `python -m geo_engine.plan_cli status geotests/<test_id>`
   (or `list` for every test). If `python` isn't found, use `python3`.
2. Hand the step it names to that agent, passing the test folder:
   `geo-brief`, `geo-data`, `geo-design`, `geo-launch`, `geo-monitoring`, `geo-analysis`,
   `geo-readout`.
3. When the next step belongs to the user (a gate or a choice), stop, summarise what
   they need to review in a few lines with the report's path, and wait.
4. After each step, run `status` again and tell the user what is next.

`/geo-test <test_id>` does this loop for one step at a time; `/geo-status` lists tests;
`/geo-approve <test_id> <gate>` records an approval.

## Rules that never bend

- **Gates are the user's.** Run `plan_cli approve` only when the user's latest message
  approves that specific gate, with `--by` set to their name. Claude Code asks them to
  confirm the command as well.
- **The test plan is changed only through `plan_cli`.** Never edit `test_plan.yaml`
  directly (it is blocked). After G3 the brief, data and design are locked; a change
  needs `plan_cli deviation` with the user's reason.
- **No peeking.** While a test is `locked` or `live`, do not open its KPI data, results
  or any file that would show how it is going. Monitoring uses `plan_cli monitor`, which
  reads spend only.
- **Numbers come from the engine.** Quote figures only from the reports the commands
  write (`quality_report.md`, `design_shortlist.md`, `results.md`, ...). Never compute,
  re-round or extrapolate statistics yourself.
- **Synthetic ground truth is off limits.** Never read `data/_ground_truth/` or use the
  `--truth` flag while working on a test; they exist only for the engine's own tests.

## Engine commands (for reference)

`python -m geo_engine.plan_cli new|brief|approve|data|design|choose|launch|monitor|analyse|readout|deviation|status|list`.
Each prints what it did and what comes next. Run `python -m geo_engine.plan_cli <cmd> --help`
for options. `python -m pytest -q` runs the engine's tests.
