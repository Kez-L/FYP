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

For `condition=minyee` there are also:

```
  adapt_signal_log.csv     wall-clock timeline of .adapt_signal: mode, signal_unix, event
  logs/minyee_analyzer.log run_analyzer.py stdout: CES, triggers, modes, recovery verdicts
  logs/main.log            contains "[Adaptive Mutator] Mode changed: X -> Y" per instance
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
