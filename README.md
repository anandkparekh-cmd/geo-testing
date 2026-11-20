# Geo incrementality agent system

Phase 1, step 1: the synthetic data generator. The spec is in the Claude doc
"Geo Incrementality Agent System: Spec".

## Setup

```bash
python -m pip install -r requirements.txt
```

## Generate data

```bash
# Two years of business-as-usual history (2024-11-18 to 2026-11-15)
python -m geo_engine.synthetic.cli history

# Inject a test: 10 random non-London areas go dark for 4 weeks, plus a 2-week cooldown
python -m geo_engine.synthetic.cli test --name demo --random 10 --exclude-region London --start 2027-01-11

# Or name the areas yourself, or run a heavy-up
python -m geo_engine.synthetic.cli test --name hu --areas B,LS,NG --start 2027-01-11 --kind heavy_up --multiplier 1.5
```

`--seed` (default 42) changes the whole synthetic world. Same seed, same data.

## Outputs

| File | What it is | Who can read it |
|---|---|---|
| `data/synthetic/history.csv` | date, postcode, kpi, spend: one row per district per day | Agents |
| `data/synthetic/test_<name>.csv` | Same format, history plus the test and cooldown | Agents |
| `data/synthetic/postcode_areas.csv` | Postcode area to region lookup | Agents |
| `data/_ground_truth/test_<name>.json` | True effect: lift, incremental conversions, cost per incremental conversion | Never agents |
| `data/_ground_truth/test_<name>_daily.csv` | Daily treated-area totals with and without the test | Never agents |
| `data/_ground_truth/world_events.csv` | Regional promos and outages built into the data | Never agents |

Your real data uses the same CSV format, so swapping it in later means pointing
`ingest` at your file instead of `history.csv`.

## What the synthetic world contains

- 121 real UK postcode areas, split into about 700 districts (district numbers are illustrative).
- About 4,000 conversions and GBP 25,000 of test-channel spend per day nationally.
- National trend, weekly cycle, winter peak, Black Friday and Christmas effects.
- Shared regional movements, so matching has real structure to find.
- Regional promos and local outages, to exercise the data quality checks.
- Test-channel spend drives about 10% of conversions, with carryover and diminishing returns.

## Check a data file (synthetic or real)

```bash
python -m geo_engine.cli quality data/synthetic/history.csv

# Your own export: name any column auto-detection can't work out
python -m geo_engine.cli quality my_export.csv --col postcode="Billing Postcode" --col kpi="New Customers"

# Other geo units: region, postcode_district, or a custom lookup CSV (e.g. ITV regions)
python -m geo_engine.cli quality my_export.csv --unit custom --lookup itv_regions.csv
```

This runs three engine functions in turn:

1. `ingest`: reads CSV or Excel, maps columns, reads UK or ISO dates, parses postcodes at
   any level down to districts, sums duplicates, fills missing postcode-days with zero,
   and logs every fix it makes.
2. `map_geo`: aggregates to postcode area (default), region, district or a custom lookup.
3. `quality_report`: checks history length, missing dates, national tracking drops,
   zero runs, low-volume geos, unusual spikes and dips (per region and per geo), level
   shifts, dominant geos and spend coverage. It recommends each geo as keep, avoid as a
   test geo, or review.

Outputs: `reports/<file>_quality.md` (readable), `reports/<file>_quality.json` (for the
agents) and `data/clean/<file>_<unit>.csv` (the area-level data used from here on).

## Design a test

```bash
python -m geo_engine.cli design data/synthetic/history.csv --expected-lift -0.08 --exclude-region London
```

Runs the quality checks, then:

1. `select_markets`: builds candidate test groups (40 per size by default, sizes 5/10/15/20,
   each mixing large and small geos), leaving out geos the quality report marked *review*
   and keeping *avoid as test geo* geos as controls only.
2. `simulate_power`: replays each candidate over about 40 historical 21/28/42-day windows
   with no test running (peak trading 20 Nov to 5 Jan skipped), using the same synthetic
   control estimator the analysis will use. The error spread gives the MDE.
3. Ranks the options and, given an expected lift, recommends the cheapest one that keeps
   80% power on all of history and on its most recent half alone. The noise behind every
   MDE is the historical error widened by 15% and read with t critical values (see
   "Are the intervals honest?" below), so the design and the readout use the same yardstick.

Outputs: `reports/<file>_design.md`, `reports/<file>_design.json` and every candidate
in `reports/<file>_design_candidates.csv`.

### Is the MDE honest?

`python scripts/validate_power.py` builds 10 fresh synthetic worlds, picks a design in
each, then runs real 28-day holdouts after the history ends and compares the estimation
error with the prediction. Result (`reports/power_validation.json`), 20 tests per row:

| Design | Predicted error (RMSE) | Actual error (RMSE) |
|---|---|---|
| Chosen by select_markets | 1.74% | 1.80% |
| Random 10 geos | 2.13% | 2.38% |

Raw predictions land within about 10% of reality, and the chosen designs beat random
ones by about a quarter.

## Read a test

```bash
python -m geo_engine.cli analyse data/synthetic/test_demo.csv --test-geos AB,CF,CW,G,HD,KW,LE,ME,PL,S \
    --start 2027-01-11 --days 28 --cooldown 14 --truth data/_ground_truth/test_demo.json
```

(`--truth` is for synthetic data only: it adds a comparison with the injected effect.)

1. `estimate_lift`: fits the counterfactual on the 365 days before the test, compares it
   with what the test geos did, and gives lift, incremental KPI, cost per incremental
   conversion and their 90% intervals, for the test and for test plus cooldown.
2. `placebo_test`: checks the interval in time (no-test windows before the test) and in
   space (fake test groups drawn from the controls over the real test dates).
3. `robustness_suite`: re-estimates with time-based regression, a 182-day fitting period,
   the top control removed, and each test geo left out, and checks the direction holds.

Outputs: `reports/<file>_lift.md`, a chart `reports/<file>_lift.png`, `_lift.json` and
the daily series `_lift_daily.csv`.

Demo result: true effect -8.52% (-1,102 conversions); estimate -8.40% (-1,097), 90%
interval -12.9% to -3.9%. Cost per incremental conversion: true £70.98, estimate £73.85.

### Are the intervals honest?

`python scripts/validate_lift.py` (and `... fresh` for new worlds) runs 80 estimates in
10 synthetic worlds: real 28-day holdouts and periods with no test, on random designs.

| Run | 90% interval held the truth | No-test periods called significant | Holdouts detected |
|---|---|---|---|
| Worlds 1-10 (used to set the 15% widening) | 92.5% | 10.0% | 97.5% |
| Worlds 11-20 (fresh check) | 97.5% | 2.5% | 90.0% |

Without the widening and t values, intervals held the truth only 82.5% of the time and
no-test periods were called significant 20% of the time. With them, the intervals are
honest to slightly cautious.

## Launch a test

```bash
python -m geo_engine.cli launch data/synthetic/history.csv --name jan_holdout \
    --test-geos BD,CA,DA,FY,HP,LE,RG,SP,WR,WS --start 2027-01-11 --days 21 --cooldown 14
```

`export_targeting` writes into `targeting/`:
- `<name>_google_ads_exclude.txt` and `<name>_meta_exclude.txt`: the test areas' postcode
  districts, one per line (both platforms target UK postcodes at district level, e.g. LS1;
  Google takes 1,000 per bulk paste, so longer lists are split). A heavy-up writes
  `_include` lists for a separate campaign instead.
- `<name>_geo_assignment.csv`: every area as test, control or left out.
- `<name>_launch_checklist.md`: dated steps, including cutting campaign budgets by the
  test areas' share so the paused budget does not flow into the controls.

Lists only hold districts that appear in your data: check them against a full UK
postcode district list before launch.

## Monitor a live test

```bash
python -m geo_engine.cli monitor platform_location_report.csv --name jan_holdout \
    --test-geos BD,CA,DA,FY,HP,LE,RG,SP,WR,WS --start 2027-01-11 --days 21
```

The file needs date, postcode and spend (a platform location report works); any KPI
column is ignored, so nobody sees results mid-test.
- `pacing_report`: test spend against plan, control spend against the 28-day pre-test
  average and against the week just before launch. A step up in control spend at
  launch means the platform moved the paused budget into the controls.
- `leakage_check`: spend still reaching test areas in a holdout (or extra spend landing
  outside them in a heavy-up), by area.

Try it on a deliberately flawed synthetic test:
`python -m geo_engine.synthetic.cli test --name leaky --areas BD,CA,DA,FY,HP,LE,RG,SP,WR,WS --start 2027-01-11 --days 21 --cooldown 0 --leakage 0.08 --reallocation 0.6`,
then run `monitor` on `data/synthetic/test_leaky.csv`.

## Run a test from its plan (phase 2)

Each test lives in `geotests/<test_id>/` with one `test_plan.yaml`: the brief, data,
design, launch, monitoring log, deviations, results and readout, plus the four gate
approvals. Every step writes its reports into the same folder. The orchestrator's rules
(`geo_engine/workflow.py`) decide what is allowed next; the agents will call the same
commands.

| Status | Next step | Who |
|---|---|---|
| draft | complete the brief, then approve **G1** | Brief agent, then you |
| brief_approved | `data`: ingest and quality checks | Data agent |
| data_ready | `design`, then `choose`, then approve **G2** | Design agent, then you |
| design_approved | `launch`: lists and checklist, then approve **G3** (locks the plan) | Launch agent, then you |
| locked / live | `monitor` weekly (spend only); `analyse` after the test | Monitoring, Analysis agents |
| analysed | approve **G4** | you |
| results_approved | readout | Readout agent |

```bash
P="python -m geo_engine.plan_cli"; G=geotests/jan_holdout
$P new jan_holdout
$P brief $G --question "Does Meta prospecting drive incremental new customers?" \
    --channel meta_prospecting --test-type holdout --kpi new_customers --kpi-source "orders database" \
    --success-metric cost_per_incremental --decision-rule "Keep if under £90 per incremental customer" \
    --expected-lift -0.08
$P approve $G G1 --by Anand
$P data $G data/synthetic/history.csv
$P design $G --exclude-region London
$P choose $G                      # the recommended option, or --days 28 --geos 10
$P approve $G G2 --by Anand
$P launch $G --start 2027-02-15
$P approve $G G3 --by Anand       # brief, data and design are now locked
$P monitor $G spend_report.csv    # weekly
$P analyse $G full_data.csv       # after the test
$P approve $G G4 --by Anand
$P status $G                      # any time: where the test is and what comes next
```

After G3 the brief, data and design are locked: a hash of them is stored, and any
change, even a hand edit of the YAML, is refused unless logged with
`$P deviation $G --section design --field cooldown_days --value 21 --reason "..." --by Anand`.

The readout also lists unusual regional movements among the control areas during the
test (`outside_events`), as context. They are common and already part of the noise the
interval allows for, so they are not used to discount the result.

## Tests

```bash
python -m pytest -q
```

Checks reproducibility, the CSV format, that untreated areas are identical with
and without a test, and that the ground truth is internally consistent.
