# Running AFL++ campaigns on AWS EC2

Scripts in this folder:

| Script | Run where | What it does |
|---|---|---|
| `setup_machine.sh` | on the VM, once | Installs packages, builds AFL++ (pinned version), clones the FYP repo |
| `build_libxml2.sh` | on the VM, once | Builds `xmllint-afl` (ASan) and `xmllint-cmplog` |
| `run_campaign.sh` | on the VM, per run | Starts the fuzzers + passive loggers, records metadata, auto-finishes |
| `finish_campaign.sh` | started automatically | Waits for the end, packs results, shuts the VM down |
| `status.sh` | on the VM, any time | Progress / health of every run on that machine |

Tested end-to-end on Ubuntu 24.04 with AFL++ v5.03c and libxml2 v2.15.4.

---

## 0. Before you start (one-time, in the AWS console)

1. **Account plan.** If your account is on the *Free* plan, EC2 only lets you launch a few
   small instance types. Upgrade to the *Paid* plan (Billing → Account plan); your
   sign-up credits carry over.
2. **Budget alert.** Billing → Budgets → Create budget → e.g. US$150, email alerts at 50/80/100%.
   Alerts only warn you; they don't stop anything.
3. **Region.** Pick one (top-right of the console) and do everything in it.
   US East (N. Virginia) is ~25% cheaper than Sydney; latency doesn't matter for fuzzing.
4. **Quota.** Service Quotas → EC2 → "Running On-Demand Standard (A, C, D, H, I, M, R, T, Z)
   instances" → request 64. Until approved you're limited to 5 vCPUs, which is enough for
   the small test machine in step 1 but **not** for 5 parallel `c7i.xlarge` (20 vCPUs).

## Which machine size

**FROZEN as of the libxml2 baseline batch: `c7i.xlarge` (4 vCPUs, 8 GiB) running 3 fuzzers**
(main + sec1 + sec2), in **ap-southeast-2 (Sydney)**. That is what the 5 committed baseline
runs in `baseline/run1..5/` actually used - see any of their `meta.txt`. Every later
condition and target must match it or the comparison is void. 3 fuzzers leaves 1 vCPU for
the loggers, Min Yee's analyzer and AgentAFL.

Sydney is ~25% dearer than N. Virginia but keeps the already-approved EC2 quota, so don't
move. Don't use `t2`/`t3`/`t4g` types (they throttle under sustained load) or Lightsail.

| Per 12-hour run | Sydney (what we use) |
|---|---|
| `c7i.xlarge` (4 vCPU) | ~US$2.80 |
| 5 runs (one target, one condition) | ~US$14 |
| All 45 runs | ~US$125 |

Always launch with the explicit layout so a default can never drift:

```bash
./run_campaign.sh libxml2 baseline 1 3 43200
```

Plus disk: 30 GiB costs about US$0.10/day while the VM exists (running or stopped).

---

## 1. Launch a test VM

EC2 → Launch instance:

- **Name:** `afl-test`
- **AMI:** Ubuntu Server 24.04 LTS, 64-bit (x86)
- **Instance type:** `c7i.large` while you wait for the quota, `c7i.xlarge` once approved
- **Key pair:** Create new → `.pem` → save it somewhere safe (you can't re-download it)
- **Network:** Allow SSH from **My IP**
- **Storage:** 30 GiB, gp3
- **Advanced details → Shutdown behavior:** leave as **Stop** (NOT Terminate - terminate
  deletes the disk, and your results with it)

## 2. Connect

On your laptop:

```bash
chmod 400 ~/Downloads/afl-key.pem
ssh -i ~/Downloads/afl-key.pem ubuntu@<Public IPv4 address from the EC2 console>
```

The public IP changes every time the VM is stopped and started.

## 3. Get the scripts and set up

Easiest: commit this `cloud/` folder to the FYP repo, then on the VM:

```bash
mkdir -p ~/fyp && git clone --depth 1 https://github.com/Kez-L/FYP.git ~/fyp/FYP
cd ~/fyp/FYP/cloud
./setup_machine.sh          # ~5-15 min
./build_libxml2.sh          # ~2-5 min
```

(Or copy the folder from your laptop: `scp -i afl-key.pem -r cloud ubuntu@<ip>:~/`.)

**Match your local setup before building** (these are recorded in every run's `meta.txt`):

```bash
AFLPP_REF=v5.03c ./setup_machine.sh            # your local `afl-fuzz -h | head -1`
LIBXML2_REF=v2.15.4 USE_ASAN=1 ./build_libxml2.sh
```

## 4. Smoke test (10 minutes, 2 fuzzers)

```bash
AUTO_SHUTDOWN=0 ./run_campaign.sh libxml2 baseline 0 2 600
./status.sh                  # run again after a few minutes
```

Check that execs/s is in the same ballpark as your laptop and that `edges` keeps rising.
After ~11 minutes `status.sh` should say FINISHED and `~/fyp/results/` should contain
`libxml2_baseline_run0.tar.gz`.

## 5. Make a reusable image (so 5 VMs are identical)

1. On the VM: `rm -rf ~/fyp/runs/* ~/fyp/results/*` (don't bake test runs into the image)
2. Console: select the instance → Actions → Image and templates → **Create image**
3. Name it e.g. `afl-libxml2-aflpp503c`. Takes a few minutes.
4. Terminate the test VM.

The image costs roughly US$1.50/month to keep. Delete it (and its snapshot) at the end of
the project.

## 6. The real runs

1. EC2 → AMIs → select your image → **Launch instance from AMI**.
   Type `c7i.xlarge`, same key pair and security group, **Number of instances: 5**.
2. SSH into each VM and start one run, with a different run id on each:

   ```bash
   cd ~/fyp/FYP/cloud
   ./run_campaign.sh libxml2 baseline 1 3 43200    # VM 1
   ./run_campaign.sh libxml2 baseline 2 3 43200    # VM 2  ... up to 5
   ```

   For the `minyee` condition, run `./preflight_resource_aware.sh` once on the VM first, then:

   ```bash
   ./run_campaign.sh libxml2 minyee 1 3 43200      # VM 1 ... up to 5
   ```

   For the `combined` condition (minyee + AgentAFL), run both preflights once on the VM,
   then:

   ```bash
   ./preflight_agentafl.sh
   ./run_campaign.sh libxml2 combined 1 3 43200    # VM 1 ... up to 5
   ```

3. Close your laptop. Each VM fuzzes for 12h, packs its results, and powers itself off.
   (A failsafe also powers it off 13h after start, in case anything hangs.)

## 7. Collect results, then terminate

The next day the VMs will be in the *Stopped* state (no compute charge, disk still kept).
For each one:

1. Instance state → **Start**, wait for "Running", copy the new public IP
2. On your laptop:
   ```bash
   mkdir -p results
   scp -i afl-key.pem 'ubuntu@<ip>:~/fyp/results/*.tar.gz' results/
   tar -tzf results/libxml2_baseline_run1.tar.gz | head    # check it opens
   ```
3. Instance state → **Terminate**

## What's in each results tarball

```
libxml2_baseline_run1/
  meta.txt                 everything needed to describe the run in your write-up:
                           versions, commits, instance type, seeds hash, timings
  summary.txt              final afl-whatsup + key fuzzer_stats per instance
  afl_env.txt              AFL_* environment used
  afl-out/<instance>/      AFL++ output: plot_data (coverage over time), fuzzer_stats,
                           queue/, crashes/, hangs/
  plateau_log.csv          plateau_watch.py, polled every 60s across all instances
  monitor/raw_metrics.json Min Yee's monitor.py, every 30s (window_sec, hardcoded)
  monitor/experiment_metrics.csv  same windows as a 15-column CSV
  logs/                    per-process logs
```

For `condition=minyee` (and `combined`) there are also:

```
  adapt_signal_log.csv     wall-clock timeline of .adapt_signal: mode, signal_unix, event
  logs/minyee_analyzer.log run_analyzer.py stdout: CES, triggers, modes, recovery verdicts
  logs/main.log            contains "[Adaptive Mutator] Mode changed: X -> Y" per instance
```

For `condition=combined` there are also:

```
  agentafl/agentafl_timeline.csv     WHEN AgentAFL ran: one row per event, with elapsed_s
                                     on the same clock as afl-out/*/plot_data relative_time
  agentafl/escalations.jsonl         one record per escalation (and per dropped request):
                                     calls, seeds, new edges, injection result, seed-memory size
  agentafl/history/cycles.jsonl      the campaign-wide seed memory, append-only
  agentafl/history/candidates/       every generated seed, kept because the memory
                                     references it by path - do not prune
  agentafl/escalations/esc_NNN/      per-escalation prompts/, responses/, seeds/,
                                     calls.jsonl, baseline_edges.json, summary.json
  logs/agentafl_escalation.log       daemon stdout: triggers, drops, per-call yield
  afl-out/addseeds/queue/            what afl-addseeds handed to the campaign
  afl-out/main/.synced/addseeds      4-byte LE count of entries main actually imported
```

`afl-out/*/plot_data` is the primary coverage-over-time data. Note that in parallel mode
each instance's `edges_found` is its own view; campaign-wide coverage is best computed
afterwards by running `afl-showmap` over the union of all instances' queues.

---

## Cost safety checklist

- Budget alert set up
- `AUTO_SHUTDOWN` left on for real runs (it's on by default)
- Shutdown behavior = Stop, never Terminate, until results are downloaded
- After downloading: **Terminate** the VMs (stopped VMs still cost a little for disk)
- End of project: delete AMIs + snapshots (EC2 → AMIs → Deregister; EC2 → Snapshots → Delete)

## Frozen experiment configuration

Settled by the committed libxml2 baseline batch. Anything here that changes invalidates
the comparison across conditions.

| | value |
|---|---|
| instance type | `c7i.xlarge` (4 vCPU, 8 GiB), ap-southeast-2 |
| instances | 3: `main` (`-M main -c xmllint-cmplog`, `AFL_CYCLE_SCHEDULES=1`), `sec1 -p explore`, `sec2 -p exploit` |
| duration | `43200` (12h), via `-V` |
| common flags | `-m none -t 1000` |
| seeds | `AgentAFL/seeds_min`, 111 files, `seeds_sha256_16=52974a6a2190a399` |
| AFL++ | `v5.03c` (`afl-fuzz++5.03c`) |
| libxml2 | `v2.15.4` (`96498992`), ASan + CMPLOG |

`run_campaign.sh` uses the `run_tidy.sh` / `run_libical.sh` layout (main with CMPLOG,
secondaries with explore/exploit/...). The older local XML command in
`AgentAFL/common_commands.md` used a different one (CMPLOG on sec1, no `-p`) - that layout
is **not** what the baselines used, so don't copy from there. Seeds can be overridden with
`SEEDS=/path`, but then the hash no longer matches the baselines.

## The `minyee` condition

Nothing in `ResourceAwareAFL/` ever starts or stops `afl-fuzz`. The chain is:

```
afl-fuzz  -- writes fuzzer_stats
  -> monitor.py <outdir>      passive; psutil on the pid read from fuzzer_stats
                              -> monitor/raw_metrics.json (JSONL, 30s windows)
  -> run_analyzer.py          tails raw_metrics.json from its CWD, 1s poll
                              CES scoring + CPU / memory / coverage triggers
  -> controller.py            -> ResourceAwareAFL/adaptation/.adapt_signal
                                 ("MODE=<mode>;TIMESTAMP=<unix>")
  -> adaptive_mutator.py      AFL++ Python custom mutator, loaded IN-PROCESS in each
                              afl-fuzz via AFL_PYTHON_MODULE; re-reads .adapt_signal on
                              every fuzz_count()/fuzz() and swaps mutation strategy
```

`adaptive_mutator.py` is the only link that touches the fuzzing, so `run_campaign.sh`
exports `AFL_PYTHON_MODULE=adaptive_mutator` and passes `PYTHONPATH=<repo>/ResourceAwareAFL`
to every instance. Without that the analyzer would log decisions nothing ever acts on, and
the arm would be statistically identical to baseline. `fuzz_count()` returns 0 while the
mode is `DEFAULT`, so the custom stage is dormant until a trigger fires.

Run `./preflight_resource_aware.sh` once per machine first. It checks that this AFL++ was built
with Python mutator support, that `psutil` is importable, that `cea`/`controller` import,
and calls `fuzz()`/`fuzz_count()` across all 7 modes and a range of buffers and `max_size`
values - a raise inside `fuzz()` makes afl-fuzz FATAL mid-campaign.

`MINYEE_PIN_MODE=<MODE>` pins the signal to one mode and skips the analyzer, for smoke
testing one mode against real afl-fuzz. It is recorded as `pinned_mode=` in `meta.txt`, so
a smoke run can't be mistaken for a campaign.

### What to expect from the results

`mutate_throttle_power` and `mutate_skip_timeouts` in `adaptive_mutator.py` are currently
identical (`random.choice([bit_flip, arithmetic_mutation])`), nothing in the chain changes
`-p`, `-t`, `-m` or AFL++ scheduling, and `fuzz_count()` returns 1, so the ceiling on any
adaptation is one extra mutation exec per queue entry. Expect `libxml2_minyee` coverage
around `libxml2_baseline`, within noise. The reportable output of this arm is the
control-loop timeline (trigger types, mode dwell times, recovery verdicts, CES) plus
overhead - not a coverage win. Per the approved architecture that is the point: Min Yee's
layer is the conventional Level-1 adjustment, and AgentAFL exists for when it doesn't
recover the plateau.

### Notes / known issues in Min Yee's code

Her files are deliberately left unmodified so results stay attributable to her component
as committed. Worked around from this side, or detected:

1. `monitor.py` resolves its `fuzzer_stats` path **once**, in `__init__`: it prefers
   `<out>/default/fuzzer_stats` and only falls back to `<out>/fuzzer_stats` if the fallback
   already exists at that instant. `run_campaign.sh` therefore waits for
   `afl-out/main/fuzzer_stats` (up to `STATS_WAIT`, default 300s) before starting it, and
   for `minyee` **aborts the campaign** if it never appears - otherwise the monitor logs
   nothing for 12h and the adaptive arm is silently dead.
2. `run_analyzer.py` prints a literal `U+0394` inside its per-record `try`, whose
   `except Exception` only logs and continues, with the adaptation logic *after* those
   prints. Under a non-UTF-8 locale every window would raise and no trigger would ever
   fire, while the pid file and `status.sh` looked healthy. Worked around by launching it
   with `PYTHONIOENCODING=utf-8 LC_ALL=C.UTF-8`.
3. `run_analyzer.py` dereferences `controller.last_adaptation["trigger_reason"]` in the
   recovery branch, but `controller.evaluate_adaptation()` sets `last_adaptation = None`.
   On that path the `TypeError` is swallowed, `last_adaptation_record` is never cleared and
   `awaiting_recovery` stays `True`, so the branch re-raises every window and the signal
   **pins at the last non-DEFAULT mode for the rest of the run**. Only fixable in her file.
   `status.sh` surfaces it as a climbing `errors=` count plus a mode that hasn't changed in
   over 30 minutes; `summary.txt` records `analyzer_errors=`.
4. `.adapt_signal` lives in the repo checkout, so it is shared by every campaign on a
   machine. `run_campaign.sh` refuses to start a `minyee` run if another `run_analyzer.py`
   or any `afl-fuzz` is already running. One campaign per VM.
5. `monitor.py` watches `afl-out/main` only, so the control signal is derived from one
   instance's process tree and applied to all three. Recorded as `control_scope=` in
   `meta.txt`.
6. `monitor.py`'s `window_sec=30` and `run_analyzer.py`'s input filename are hardcoded,
   hence the `cd "$RUN/monitor"` before launching both.
7. Coverage comes from `bitmap_cvg` (2-dp percentage), which moves much less finely than
   `edges_found`. Window 1's `cpu_percent` is always 0.0 (monitor initialisation) and her
   own analysis scripts discard it.
8. `main.py` is the superseded predecessor of `run_analyzer.py` - `run_analyzer.py` is the
   entry point this pipeline uses.

## The `combined` condition (AgentAFL Level 2)

`combined` is `minyee` plus the AgentAFL escalation: when Min Yee's Level-1 layer adapts and
still fails to recover a coverage plateau, AgentAFL generates seeds and injects them into the
live campaign. It is the arm the two components exist to produce together.

```bash
./preflight_resource_aware.sh     # once per machine
./preflight_agentafl.sh           # once per machine, checks the Level-2 pieces
./run_campaign.sh libxml2 combined 1 3 43200
```

The chain adds one process and one file to the `minyee` chain:

```
run_analyzer.py
  -> (coverage adaptation observed, improved == False)
  -> ResourceAwareAFL/adaptation/.agentafl_request        one JSON line, atomic rename
       -> agentafl_escalation.py          separate process, polls every 2s
            -> re-freeze the baseline (afl-showmap -C over every instance queue)
            -> AgentAFL Stage 4, 5 calls x 5 seeds, positive + negative feedback
            -> afl-addseeds -o afl-out    lands in afl-out/addseeds/queue/id:%06d,...
                 -> afl-out/main imports it on its next sync pass (~30s)
                      -> secondaries inherit it from main
```

The hook in `run_analyzer.py` is ~10 additive lines that only write that file, wrapped in
`try/except`. Everything expensive happens in the separate process, because `run_analyzer.py`
advances on *lines* of `raw_metrics.json` rather than wall clock: a ~105s LLM call inside her
loop would let the 2-3 windows describing the call itself be consumed as the post-injection
recovery evidence, i.e. it would measure the plateau it was trying to escape.

### Seed memory across escalations

`<run>/agentafl/history/cycles.jsonl` is append-only for the whole campaign, with
`cycle_id` strictly increasing. Each escalation's prompts are built from **every** earlier
escalation's seeds: coverage-increasing ones as "Reached new coverage:" examples, redundant
ones clustered at Jaccard 0.9 as "Tried before - avoid repeating:". Only the very first call
of the very first escalation is cold, and it bootstraps from the AFL++ queue instead.

**Never delete `agentafl/history/candidates/`.** The scan drops any record whose seed file is
missing, so pruning it silently shrinks the memory rather than erroring. It is ~700 small
files per 12h run.

### Running Min Yee's layer without AgentAFL

AgentAFL is opt-in at two independent levels, so `minyee` is a clean control:

| condition | `AGENTAFL_HOOK` | daemon | effect |
|---|---|---|---|
| `baseline` | `0` | not launched | no mutator either |
| `minyee` | `0` | not launched | **her layer exactly as before** |
| `combined` | `1` | launched | full two-level escalation |

```bash
# minyee control for the three-arm batch (current code, no AgentAFL at all)
./run_campaign.sh libxml2 minyee 1 3 43200

# ...and also reproduce the committed Oct-13 fuzzer config (stock 20min sync)
AFL_SYNC_TIME=20 ./run_campaign.sh libxml2 minyee 1 3 43200
```

With `AGENTAFL_HOOK` unset or `0`, the hook in `run_analyzer.py` is dead code — the `try`
is never entered and no file is written. Verified by replaying her committed metrics through
three variants: her file as committed at `HEAD`, the patched file with the gate off, and the
patched file with the gate on. The first two produce **byte-identical stdout and neither
creates `.agentafl_request`**; only the third writes it. So a `minyee` run is
indistinguishable from one against her unpatched code.

`AFL_SYNC_TIME=20` is byte-identical in behaviour to leaving it unset: `SYNC_TIME` is
`20 * 60 * 1000` in `config.h` and the env parser computes `time * 60 * 1000`. Both
`agentafl_hook=` and `afl_sync_time=` are recorded in `meta.txt` for every condition —
`afl_env.txt` only captures `^AFL_|^ASAN_`, so the hook state would otherwise be invisible.

Matching the committed Oct-13 tarballs *exactly* would additionally need her
`controller.py` and `run_analyzer.py` at `ae015802`, i.e. before the Coverage Retry change.
That is deliberately not wired up here — you would be comparing against her superseded
behaviour.

Because the gate defaults to off, a `combined` run could in principle be launched with the
hook disabled and silently never escalate. Three things prevent that: `run_campaign.sh` sets
the gate explicitly for every condition rather than relying on the default,
`agentafl_escalation.py` refuses to start when it is not `1` (override with
`--allow-hook-off` only for hand-written requests), and `preflight_agentafl.sh` checks both
the gate and its export are present.

### When did AgentAFL run?

Everything is timestamped against **afl-fuzz's own `start_time`** (from
`afl-out/main/fuzzer_stats`), which is the same origin as the `relative_time` column in
`afl-out/*/plot_data`. So an escalation's `elapsed_s` drops straight onto the coverage curve
with no offset arithmetic.

`agentafl/agentafl_timeline.csv` is the flat, joinable version, in the same spirit as
`plateau_log.csv` and `adapt_signal_log.csv`:

```
unix_time,elapsed_s,elapsed_hms,event,escalation_id,calls,seeds,new_edges,injected,synced_main,recovery_edges,detail
1791889032.4,1830.0,0h30m30s,escalation,1,5,25,70,25,25,0,
1791889302.1,2100.0,0h35m00s,dropped,,,,,,25,0,"cooldown (1230s left)"
```

`summary.txt` reports the same thing at a glance:

```
escalation_times_T+=0h30m 0h57m 1h24m ...
escalation_gaps_s=[1590, 1620, ...]  (min=1590 should be >= cooldown)
first_escalation_T+=0h30m
```

Per-escalation detail is in `agentafl/escalations/esc_NNN/calls.jsonl`, which carries
`elapsed_s` per individual LLM call alongside its latency and marginal new edges.

### Budget, and why there is a cooldown

5 calls x 5 seeds per escalation: replayed over every `stage4_both` run in
`AgentAFL/prompt_tests/results3/`, the first 5 calls captured 90% (xml) / 81% (ical) of a
15-call run's new edges at the best edges-per-call of any budget retaining ~90% of the yield,
and the single biggest-yield call fell within the first 5 in every xml and ical run.

`AGENTAFL_COOLDOWN_S` (default 1500) is load-bearing, not a safety net. `run_analyzer.py`'s
Coverage Retry clears the stagnation latch whenever recovery yields zero edges, so
`COVERAGE_SLOWDOWN` re-fires roughly every 5 windows (150s). Replayed over
`ResourceAwareAFL/six_hour_results/` that is 49-106 opportunities per 6h run (98-212 per 12h).
Uncapped that would be ~4.5h of LLM wall time per run; 1500s throttles it to ~28 escalations,
~140 calls, ~49min. Tune with `AGENTAFL_CALLS`, `AGENTAFL_SEEDS_PER_CALL`,
`AGENTAFL_COOLDOWN_S`, `AGENTAFL_MAX_ESCALATIONS`, `AGENTAFL_PROVIDER`, `AGENTAFL_MODEL`;
all are recorded in `meta.txt`.

### `AFL_SYNC_TIME=1` is set on every condition

AFL++ defaults to a 20-minute sync period, and a main node syncs at half that, so an injected
seed would idle up to 10 minutes. `run_campaign.sh` therefore sets `AFL_SYNC_TIME=1` for
**baseline, minyee and combined alike**, so the only difference between arms is whether seeds
are injected at all. A `combined` run compared against baselines fuzzed at the default sync
period would not be a clean comparison.

### Failure modes to check, in order

All three are silent - the campaign fuzzes happily and the arm just equals `minyee`:

| symptom in `summary.txt` / `status.sh` | meaning |
|---|---|
| `escalations=0`, `requests_dropped=0` | her hook never fired; check the hook is present and that coverage adaptations are happening at all |
| `escalations>0`, `seeds_injected=0` | `afl-addseeds` refused - usually no `*/fuzzer_stats` yet |
| `seeds_injected>0`, `synced_addseeds_main=0` | seeds written but no fuzzer imported them |

That last one is the trap worth knowing: a campaign started **without** `-M`/`-S`
auto-configures as `-S default`, which is a *secondary*, and secondaries skip any sync
directory lacking an `is_main_node` marker - so `afl-addseeds` would import nothing, with no
error. `run_campaign.sh` always uses `-M main`, which syncs from every sibling directory, so
this only bites hand-rolled local runs (including `ResourceAwareAFL/run_six_experiments.sh`).
`agentafl_cycle.ensure_syncable()` writes the marker when it detects no main node.

### Tests

```bash
cd ResourceAwareAFL && python3 test_agentafl_cycle.py      # 13 tests, no API key needed
```

The one that matters is `test_second_escalation_sees_first_escalations_seeds`.
