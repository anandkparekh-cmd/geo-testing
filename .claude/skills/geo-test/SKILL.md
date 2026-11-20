---
name: geo-test
description: "Start or continue a geo incrementality test, one step at a time, stopping at the user's gates. Use when the user wants to set up, run or move forward a geo test."
argument-hint: "<test_id> [data file or notes]"
allowed-tools: Bash(python -m geo_engine.plan_cli *) Bash(python3 -m geo_engine.plan_cli *)
---

Run the next step of the geo test named in: $ARGUMENTS

1. Take the first word of the arguments as the test id; anything after it (a data file path, a start date, notes) is input for the step.
2. If `geotests/<test_id>/test_plan.yaml` does not exist, create it with `python -m geo_engine.plan_cli new <test_id>`; the next step is the brief.
3. Run `python -m geo_engine.plan_cli status geotests/<test_id>` and read who the next step belongs to.
   - An agent: hand the step to it (`geo-brief`, `geo-data`, `geo-design`, `geo-launch`, `geo-monitoring`, `geo-analysis`, `geo-readout`), passing `geotests/<test_id>` and any input from the arguments. If the step needs a file or a date the user has not given, ask for it first.
   - The user (a choice or a gate): summarise what they need to review in a few lines, give the report's path, and stop. Approvals go through `/geo-approve <test_id> <gate>`.
4. Finish by running `status` again and saying, in one line, what comes next and who does it.

Follow the rules in CLAUDE.md: gates are the user's, the plan changes only through `plan_cli`, no peeking at a live test, numbers only from the engine's reports.
