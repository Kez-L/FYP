# Local test of the `combined` arm (1 main + 2 secondaries)

A functional test of the escalation loop on your own machine. **Not a campaign** — the
timers and LLM budget are shortened so a trigger fires in minutes instead of hours. Nothing
produced here is comparable to a cloud run; use `run_campaign.sh` for that.

Every terminal starts with the same two lines, then runs one command. `local_test_env.sh`
sets the paths and the shortened timers and creates the directories.

```bash
cd ~/Documents/1git-folder
source cloud/local_test_env.sh
```

Everything lands in `~/fyp-test/` (outside the repo, so a test can never be mistaken for
real results).

---

## Terminal 0 — one-time setup

`monitor.py` needs `psutil`, which is the only missing dependency:

```bash
sudo apt-get install -y python3-psutil
```

No sudo? Install it anywhere and point `PYLIB` at it — `local_test_env.sh` adds it to the
monitor's `PYTHONPATH` only:

```bash
~/Documents/1git-folder/AgentAFL/.venv/bin/pip install --target ~/fyp-test/pylibs psutil
export PYLIB=~/fyp-test/pylibs      # then re-source local_test_env.sh in each terminal
```

Check everything else is ready:

```bash
cd ~/Documents/1git-folder && ./cloud/preflight_agentafl.sh
```

Expect `15 passed, 0 failed`.

---

## Terminals 1-3 — the fuzzers

Same layout as the cloud run (`-M main` + two secondaries with different power schedules),
minus CMPLOG since there is no local cmplog binary.

**`-M main` matters.** Started without `-M`/`-S`, afl-fuzz auto-configures as `-S default`,
which is a *secondary*, and secondaries skip any sync directory lacking an `is_main_node`
marker — so `afl-addseeds` would import nothing, silently.

### Terminal 1 — main

```bash
cd ~/Documents/1git-folder && source cloud/local_test_env.sh
env AFL_SKIP_CPUFREQ=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 AFL_NO_UI=1 \
    AFL_IMPORT_FIRST=1 AFL_SYNC_TIME=1 \
    AFL_FUZZER_STATS_UPDATE_INTERVAL="$AFL_FUZZER_STATS_UPDATE_INTERVAL" \
    AFL_PYTHON_MODULE=adaptive_mutator PYTHONPATH="$MINYEE" \
  afl-fuzz -M main -i "$SEEDS" -o "$OUT" -V 1800 -m none -t 1000 \
    -- "$TARGET" --noout @@
```

### Terminal 2 — sec1

```bash
cd ~/Documents/1git-folder && source cloud/local_test_env.sh
env AFL_SKIP_CPUFREQ=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 AFL_NO_UI=1 \
    AFL_IMPORT_FIRST=1 AFL_SYNC_TIME=1 \
    AFL_FUZZER_STATS_UPDATE_INTERVAL="$AFL_FUZZER_STATS_UPDATE_INTERVAL" \
    AFL_PYTHON_MODULE=adaptive_mutator PYTHONPATH="$MINYEE" \
  afl-fuzz -S sec1 -p explore -i "$SEEDS" -o "$OUT" -V 1800 -m none -t 1000 \
    -- "$TARGET" --noout @@
```

### Terminal 3 — sec2

```bash
cd ~/Documents/1git-folder && source cloud/local_test_env.sh
env AFL_SKIP_CPUFREQ=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 AFL_NO_UI=1 \
    AFL_IMPORT_FIRST=1 AFL_SYNC_TIME=1 \
    AFL_FUZZER_STATS_UPDATE_INTERVAL="$AFL_FUZZER_STATS_UPDATE_INTERVAL" \
    AFL_PYTHON_MODULE=adaptive_mutator PYTHONPATH="$MINYEE" \
  afl-fuzz -S sec2 -p exploit -i "$SEEDS" -o "$OUT" -V 1800 -m none -t 1000 \
    -- "$TARGET" --noout @@
```

Each should print `Python mutator 'adaptive_mutator' installed successfully.` If it does
not, her adaptation layer is inert and the test is meaningless.

**Why `AFL_FUZZER_STATS_UPDATE_INTERVAL`?** afl-fuzz rewrites `fuzzer_stats` only every 60s
by default (`STATS_UPDATE_SEC`), and `run_analyzer.py` **discards** any window whose
`execs_done` has not changed (`Skipping stale AFL++ stats`). Measured here without it, at a
10s window: **2 windows evaluated, 17 skipped** — no trigger would ever fire. With it: 35
evaluated, 0 skipped.

---

## Terminal 4 — Min Yee's monitor

Both the monitor and the analyzer must run from the **same** directory, because `monitor.py`
writes `raw_metrics.json` to its CWD and `run_analyzer.py` reads that filename from its CWD.

```bash
cd ~/Documents/1git-folder && source cloud/local_test_env.sh
cd "$WORKDIR"
env PYTHONPATH="$PYLIB" python3 -u -c "
import sys; sys.path.insert(0, '$MINYEE')
from monitor import AFLRuntimeMonitor
AFLRuntimeMonitor(fuzzer_out_dir='$OUT/main', window_sec=$WINDOW_SEC,
                  output_log='raw_metrics.json', experiment_csv='experiment_metrics.csv',
                  calibration_windows=$CALIB).start_monitoring()
"
```

This is her `monitor.py` unmodified — the only reason for `-c` instead of
`python3 monitor.py "$OUT/main"` is that her `__main__` hardcodes `window_sec=30`, and a
10s window gets a trigger ~3x sooner. For production-identical behaviour,
`export WINDOW_SEC=30 CALIB=8` first and the command is unchanged.

Start this **after** `$OUT/main/fuzzer_stats` exists — her `monitor.py` resolves that path
once in `__init__` and logs nothing for the whole run if it is missing at that instant.

---

## Terminal 5 — Min Yee's analyzer

```bash
cd ~/Documents/1git-folder && source cloud/local_test_env.sh
cd "$WORKDIR"
env PYTHONIOENCODING=utf-8 LC_ALL=C.UTF-8 \
  python3 -u "$MINYEE/run_analyzer.py" --calibration "$CALIB" --persistence "$PERSIST"
```

It waits politely for `raw_metrics.json`, so order relative to the monitor does not matter.
The `PYTHONIOENCODING`/`LC_ALL` are not optional: she prints a literal `Δ` inside a `try`
whose `except` swallows everything, so under a non-UTF-8 locale every window would raise and
no trigger would ever fire.

---

## Terminal 6 — the AgentAFL escalation daemon

```bash
cd ~/Documents/1git-folder && source cloud/local_test_env.sh
python3 -u "$MINYEE/agentafl_escalation.py" \
  --campaign-root "$OUT" --work-dir "$AGENTWORK" --request-file "$REQUEST" \
  --target "$TARGET" --target-args "--noout @@" \
  --instance main --instances main,sec1,sec2 \
  --calls "$AF_CALLS" --seeds-per-call "$AF_SEEDS" --cooldown-s "$AF_COOLDOWN"
```

At startup it should print, in order:

```
stripped inherited mutator env vars so child afl-* tools do not abort: AFL_PYTHON_MODULE
afl-showmap self-test OK: 833 edges on id:000000,... (status=ok)
watching .../adaptation/.agentafl_request
campaign start <epoch> (already T+...); elapsed_s ... same clock as plot_data relative_time
seed memory at start: 0 positive / 0 negative, next cycle_id 1
```

**This costs real API calls** — `$AF_CALLS x $AF_SEEDS` per escalation (2x3 by default, a
few tenths of a cent on Haiku). To exercise the whole loop with **zero** API calls, add
`--calls 0`: it still freezes the baseline, records the escalation and writes the timeline,
just generates no seeds.

---

## Terminal 7 — watching it work

```bash
cd ~/Documents/1git-folder && source cloud/local_test_env.sh
watch -n5 '
echo "--- windows: evaluated / skipped ---"
grep -c "^\[Window" '"$TEST"'/logs/analyzer.log 2>/dev/null
grep -c "Skipping stale" '"$TEST"'/logs/analyzer.log 2>/dev/null
echo "--- adapt signal ---"; cat '"$SIGNAL"' 2>/dev/null
echo "--- escalations ---"; cat '"$AGENTWORK"'/agentafl_timeline.csv 2>/dev/null
echo "--- imported by main ---"; od -An -tu4 -N4 '"$OUT"'/main/.synced/addseeds 2>/dev/null
'
```

If you ran the fuzzers in their own terminals rather than redirecting to logs, use this
instead for the analyzer part — it reads the same thing from her stdout in Terminal 5.

### Force a trigger immediately

Rather than waiting for a natural plateau, write the request her hook writes:

```bash
cd ~/Documents/1git-folder && source cloud/local_test_env.sh
printf '{"ts": %s, "window_id": 1, "trigger_reason": "COVERAGE_SLOWDOWN", "improved": false, "total_recovery_edges": 0, "ces": 0.0, "cpu": 25.0, "memory": 48.0, "delta_new_edges": 0}\n' "$(date +%s)" > "$REQUEST"
```

The daemon should consume it within ~2s. Use this to test the AgentAFL arm on its own; use
the natural path (below) to test that the whole loop closes.

---

## What a working run looks like

The natural chain, all from real code, in her analyzer's output then the daemon's:

```
[Feedback] Trigger = COVERAGE_SLOWDOWN                                  <- plateau detected
MODE=EXPLORE_HEAVY;TIMESTAMP=...                                        <- Level-1 adaptation
[Coverage Evaluation] Productive Windows: 1/3 | ... | Improved: False   <- it did not recover
  -> .agentafl_request written                                          <- the hook
  -> froze baseline: 2183 edges from 4136 queue files in 10s            <- daemon
  -> escalation 1 at T+0h05m44s                                         <- on plot_data's clock
  -> request dropped: cooldown                                          <- rate limit working
```

Checks, in order of what they prove:

| check | command | meaning |
|---|---|---|
| mutator live | `grep -c "installed successfully"` in each fuzzer's output | her layer can act at all |
| windows used | `evaluated > 0` and `skipped == 0` in Terminal 5 | the stats interval is right |
| trigger fired | `Improved: False` on a `[Coverage Evaluation]` line | Level 1 tried and failed |
| hook fired | a row appears in `agentafl_timeline.csv` | her loop reached AgentAFL |
| seeds generated | `agentafl/escalations/esc_001/seeds/` non-empty | the LLM path works |
| **seeds imported** | `od -An -tu4 -N4 $OUT/main/.synced/addseeds` advances | **the loop actually closed** |

That last one is the one that matters. Seeds written but never imported is the single
quietest failure available here, and it means nothing you measure afterwards is attributable
to AgentAFL.

---

## Teardown

```bash
pkill -x afl-fuzz
pkill -f 'run_analyzer.py|AFLRuntimeMonitor|agentafl_escalation.py'
rm -f ~/Documents/1git-folder/ResourceAwareAFL/adaptation/.adapt_signal \
      ~/Documents/1git-folder/ResourceAwareAFL/adaptation/.agentafl_request*
rm -rf ~/fyp-test          # only once you have looked at the results
```

`.adapt_signal` and `.agentafl_request` live in the repo checkout and are shared by every
run on the machine, so clear them between tests — a stale request would fire a spurious
escalation at the next daemon start.

---

## Knobs

`local_test_env.sh` defaults, override by exporting before sourcing:

| var | test default | production |
|---|---|---|
| `WINDOW_SEC` | 10 | 30 |
| `CALIB` | 3 | 8 |
| `PERSIST` | 2 | 2 |
| `AFL_FUZZER_STATS_UPDATE_INTERVAL` | 5 | 60 (AFL default) |
| `AF_CALLS` x `AF_SEEDS` | 2 x 3 | 5 x 5 |
| `AF_COOLDOWN` | 60 | 1500 |
| `AGENTAFL_HOOK` | 1 | 1 for `combined`, 0 for `minyee`/`baseline` |

### Testing Min Yee's layer on its own

The hook in `run_analyzer.py` is opt-in, so to reproduce a `minyee` run locally:

```bash
export AGENTAFL_HOOK=0
source cloud/local_test_env.sh        # prints "(minyee-only: her layer as before)"
```

Then run Terminals 1-5 as normal and **skip Terminal 6**. Her analyzer will detect plateaus
and adapt exactly as it did before the integration existed: nothing is written to
`adaptation/.agentafl_request` and no LLM call is possible.

Both the analyzer terminal (Terminal 5) and the daemon terminal (Terminal 6) read this var,
which is why it lives in `local_test_env.sh` rather than being typed per-terminal — sourcing
the same file in each keeps them in step. If you start the daemon with the gate off it will
refuse, since nothing would ever write a request; pass `--allow-hook-off` if you intend to
drive escalations by hand with the manual-trigger command above.
