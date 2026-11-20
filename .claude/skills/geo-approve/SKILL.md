---
name: geo-approve
description: "Record the user's approval of a gate (G1 brief, G2 design, G3 launch, G4 results) for a geo test."
argument-hint: "<test_id> <G1|G2|G3|G4> [note]"
disable-model-invocation: true
---

The user is approving a gate: $ARGUMENTS

1. Read the test id, the gate and any note from the arguments. If the gate is missing or not one of G1, G2, G3, G4, ask.
2. Run `python -m geo_engine.plan_cli status geotests/<test_id>` and confirm the plan is at that gate. If not, say what has to happen first and stop.
3. Remind the user in two or three lines what they are signing off (from `test_plan.yaml` and the step's report). For G3, say that it locks the brief, data and design.
4. Run `python -m geo_engine.plan_cli approve geotests/<test_id> <gate> --by <user's name> --note "<note>"`. Use the name the user goes by; ask if you do not know it. Claude Code will ask them to confirm the command.
5. Run `status` and say what comes next.
