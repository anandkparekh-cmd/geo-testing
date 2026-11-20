---
name: geo-status
description: "List every geo test in this repository with its status and what comes next."
allowed-tools: Bash(python -m geo_engine.plan_cli *) Bash(python3 -m geo_engine.plan_cli *)
---

Run `python -m geo_engine.plan_cli list` (use `python3` if `python` isn't found). Show the result as a short table: test, status, next step, who. If $ARGUMENTS names a test, run `python -m geo_engine.plan_cli status geotests/$ARGUMENTS` instead and add the gates that are done.
