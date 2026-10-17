# Running AFL++ campaigns on AWS EC2

The experiment is a 3x3 matrix, 5 runs per cell, 45 runs in total:

| | `baseline` | `minyee` | `combined` |
|---|---|---|---|
| **`libxml2`** (XML) | stock AFL++ | + ResourceAwareAFL Level 1 | + AgentAFL Level 2 |
| **`libical`** (iCalendar) | stock AFL++ | + ResourceAwareAFL Level 1 | + AgentAFL Level 2 |
| **`tidy`** (HTML) | stock AFL++ | + ResourceAwareAFL Level 1 | + AgentAFL Level 2 |

`baseline` is plain AFL++ with only the passive loggers attached, so nothing in
either of our components can touch the fuzzing. Every cell uses the same instance
type, layout, duration and AFL++ settings; the only difference between the three
columns is which of our layers is switched on.

Scripts in this folder:

| Script | Run where | What it does |
|---|---|---|
| `setup_all.sh` | on the VM, **once** | The one setup command: runs everything below, then prints GO / NO-GO |
| `setup_machine.sh` | called by `setup_all.sh` | Installs packages, builds AFL++ (pinned version), clones the FYP repo |
| `build_libxml2.sh` | called by `setup_all.sh` | Builds `xmllint-afl` (ASan) and `xmllint-cmplog` |
| `build_libical.sh` | called by `setup_all.sh` | Builds `libical_fuzzer-afl` (ASan) and `-cmplog` |
| `build_tidy.sh` | called by `setup_all.sh` | Builds `tidy_parse_string_fuzzer-afl` (ASan) and `-cmplog` |
| `preflight_resource_aware.sh` | on the VM, once | Checks everything the `minyee` arm needs (Python mutator support, all 7 modes) |
| `preflight_agentafl.sh` | on the VM, per target | Checks everything the `combined` arm needs, including the API key and the `@@` trap |
| `run_campaign.sh` | on the VM, per run | Starts the fuzzers + loggers, records metadata, auto-finishes |
| `verify_run.sh` | on the VM, at **T+15min** | PASS/FAIL verdict on a live run. **This is the one to actually look at.** |
| `status.sh` | on the VM, any time | Detailed progress / health of every run on that machine |
| `targets.sh` | sourced, not run | The single definition of each target: binaries, args, seeds, env |
| `finish_campaign.sh` | started automatically | Waits for the end, packs results, shuts the VM down |
| `signal_log.sh` | started automatically | Polls `.adapt_signal` into a joinable wall-clock timeline |

Supporting data, all committed so a fresh clone is self-sufficient:
`harnesses/` (the libical and tidy fuzz harnesses), `seeds/ical` + `seeds/html`
(their corpora; xml's is `AgentAFL/seeds_min`), `sanity/` (one small valid input
per format, for `afl-showmap` self-tests).

For a functional test of the `combined` loop on your own machine rather than on
EC2, see `LOCAL_TEST.md` and the `local_test_env.sh` / `run_local_combined.sh` /
`watch_local_combined.sh` / `stop_local_combined.sh` helpers. Nothing produced
there is comparable to a cloud run.

Tested end-to-end on Ubuntu 24.04 with AFL++ v5.03c.

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

**FROZEN: `c7i.xlarge` (4 vCPUs, 8 GiB) running 3 fuzzers** (main + sec1 + sec2), in
**ap-southeast-2 (Sydney)**. Every cell of the matrix must match it or the comparison is
void. 3 fuzzers leaves 1 vCPU for the loggers, Min Yee's analyzer and AgentAFL.

Keep 3 instances even though the earlier local campaigns used 4 for ical and 6 for tidy.
Cross-cell comparability outranks matching those older layouts, and 4 of 6 instances on a
4-vCPU box is oversubscribed. `run_campaign.sh` now defaults to 3, but pass it explicitly
anyway so a default can never drift.

Sydney is ~25% dearer than N. Virginia but keeps the already-approved EC2 quota, so don't
move. Don't use `t2`/`t3`/`t4g` types (they throttle under sustained load) or Lightsail.

| Per 12-hour run | Sydney (what we use) |
|---|---|
| `c7i.xlarge` (4 vCPU) | ~US$2.80 |
| 5 runs (one cell) | ~US$14 |
| All 45 runs (9 cells x 5) | ~US$125 |

Plus disk: 30 GiB costs about US$0.10/day while the VM exists (running or stopped). That
is still enough with all three targets built (~650 MB of sources and builds); the ASan
crash directories dominate, not the toolchain.

**LLM spend** applies only to the 15 `combined` runs: ~28 escalations x 5 calls = ~140
Haiku calls per run, so ~2100 calls across the batch. Small next to the EC2 bill, but it
is a separate account and a separate way to run out of credits mid-batch - check the
balance before starting the `combined` column.

### Quota

9 cells x 5 VMs x 4 vCPU = **180 vCPUs** if you ran the whole matrix at once, against the
approved quota of 64. So run it in batches. One target at a time is 3 cells x 5 VMs = 15
VMs = 60 vCPUs, which fits inside 64 with room to spare:

```
batch 1: libxml2  baseline/minyee/combined, runs 1-5   (15 VMs)
batch 2: libical  baseline/minyee/combined, runs 1-5   (15 VMs)
batch 3: tidy     baseline/minyee/combined, runs 1-5   (15 VMs)
```

All three batches must be launched from the **same AMI**, so bake it once before batch 1
and do not re-bake between batches.

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

## 3. Get the repo and set up

On the VM:

```bash
mkdir -p ~/fyp && git clone --depth 1 https://github.com/Kez-L/FYP.git ~/fyp/FYP
cd ~/fyp/FYP/cloud
./setup_all.sh              # ~25-45 min: AFL++, all three targets, both preflights
```

`setup_all.sh` ends with a single **GO** or **NO-GO** line. Don't image the machine until
it says GO.

The clone must land at **`~/fyp/FYP`** (i.e. `$WORK/FYP`). `run_campaign.sh` resolves the
repo as `$WORK/FYP` and needs `AgentAFL/seeds_min`, `AgentAFL/plateau_watch.py` and
`ResourceAwareAFL/monitor.py` from it, so copying only the `cloud/` folder across with
`scp` does **not** work - there is no repo for those paths to resolve against. If you do
want to push a local tree instead of cloning, push the whole repo:

```bash
scp -i afl-key.pem -r /path/to/FYP ubuntu@<ip>:~/fyp/FYP
```

Versions are pinned in the scripts and recorded in every run's `meta.txt`; override only
if you deliberately want to move the whole experiment:

```bash
AFLPP_REF=v5.03c ./setup_machine.sh            # must match `afl-fuzz -h | head -1`
LIBXML2_REF=v2.15.4 USE_ASAN=1 ./build_libxml2.sh
LIBICAL_REF=3d0c8c36... USE_ASAN=1 ./build_libical.sh
TIDY_REF=d08ddc28...   USE_ASAN=1 ./build_tidy.sh
```

### The AgentAFL API key (needed for the `combined` column only)

`AgentAFL/.env` is gitignored, so a fresh clone has no key and
`preflight_agentafl.sh` will fail on exactly that one check. Write it per VM, **after**
launching from the AMI, so the key never ends up inside an image snapshot:

```bash
echo 'CLAUDE_API_KEY=sk-ant-...' > ~/fyp/FYP/AgentAFL/.env
chmod 600 ~/fyp/FYP/AgentAFL/.env
```

Only two locations work: the `CLAUDE_API_KEY` environment variable, or
`AgentAFL/.env`. **Do not** put it in `~/Documents/.env` on the VM: the preflight accepts
that path, but the code that actually makes the call hardcodes
`/home/user/Documents/.env` (`AgentAFL/prompt_tests/batch_harness.py:62`), which does not
exist for user `ubuntu`. That combination passes preflight and then fails on the first
escalation, hours in.

`baseline` and `minyee` need no key at all.

## 4. Smoke test (~15 minutes per target, 2 fuzzers)

Do this once per target before baking the image. The `combined` condition is the one worth
smoke-testing, because it exercises every layer at once:

```bash
for t in libxml2 libical tidy; do
  AUTO_SHUTDOWN=0 ./run_campaign.sh $t combined 0 2 900
  ./verify_run.sh ~/fyp/runs/${t}_combined_run0
  rm -rf ~/fyp/runs/${t}_combined_run0
done
```

`verify_run.sh` is the check that matters: it asserts the mutator loaded in every
instance, the analyzer is evaluating windows (not just calibrating), the escalation daemon
is alive, and the `afl-showmap` self-test sees real per-seed coverage. Also eyeball
`execs/s` per target - tidy is the slowest of the three by some margin.

A 900s run is usually too short for an escalation to fire (the hook needs a Level-1
coverage adaptation to fail first, typically ~30 min), so `escalations=0` here is expected
and reported as INFO. To prove injection end-to-end, run one longer smoke test and check
`synced_addseeds_main > 0`.

## 5. Make a reusable image (so every VM is identical)

1. On the VM: `rm -rf ~/fyp/runs/* ~/fyp/results/*` (don't bake test runs into the image)
2. Console: select the instance → Actions → Image and templates → **Create image**
3. Name it e.g. `afl-3targets-<short commit>`. Takes a few minutes.
4. Terminate the test VM.

**Bake once.** All 45 runs must come from this one image: it fixes the AFL++ build, all
three target builds, and the repo commit. Re-baking partway through means the later cells
were fuzzed against a different toolchain, which is exactly the kind of drift that voided
the earlier batch.

The image costs roughly US$1.50/month to keep. Delete it (and its snapshot) at the end of
the project.

## 6. The real runs

Run one target at a time (see the quota note above). `<target>` is one of `libxml2`,
`libical`, `tidy`.

1. EC2 → AMIs → select your image → **Launch instance from AMI**.
   Type `c7i.xlarge`, same key pair and security group, **Number of instances: 5** per
   cell (15 for a whole target).
2. SSH into each VM and start one run, with a different run id on each.

   **`baseline`** - nothing extra, no preflight needed:

   ```bash
   cd ~/fyp/FYP/cloud
   ./run_campaign.sh <target> baseline 1 3 43200    # VM 1
   ./run_campaign.sh <target> baseline 2 3 43200    # VM 2  ... up to 5
   ```

   **`minyee`** - run the Level-1 preflight once per VM first:

   ```bash
   ./preflight_resource_aware.sh
   ./run_campaign.sh <target> minyee 1 3 43200      # VM 1 ... up to 5
   ```

   **`combined`** - write the API key, then both preflights, once per VM:

   ```bash
   echo 'CLAUDE_API_KEY=sk-ant-...' > ~/fyp/FYP/AgentAFL/.env
   ./preflight_resource_aware.sh
   AGENTAFL_TARGET=<target> ./preflight_agentafl.sh
   ./run_campaign.sh <target> combined 1 3 43200    # VM 1 ... up to 5
   ```

   `preflight_agentafl.sh` checks one target at a time, because the binary, the
   `afl-showmap` argument list and the format probe all differ per target. Pass
   `AGENTAFL_TARGET` to pick which.

3. **At T+15 minutes, on every VM:**

   ```bash
   ./verify_run.sh
   ```

   It prints `GOOD` or `STOP` and exits non-zero on failure. Every way these runs go wrong
   is silent - the campaign fuzzes happily and the arm is quietly worthless - so this is
   the step that saves a wasted 12h. Don't close the laptop until all 5 say GOOD.

4. Then close your laptop. Each VM fuzzes for 12h, packs its results, and powers itself
   off. (A failsafe also powers it off 13h after start, in case anything hangs.)

## What `verify_run.sh` checks, and why each one matters

Run it ~15 min in. It reads `condition=` and `target=` from `meta.txt`, so it cannot be
pointed at a run with the wrong expectations. `SAMPLE_SECS=0 ./verify_run.sh` skips the
45-second coverage-progress sample if you want it instant.

Every check below guards a failure that is **silent**: AFL++ keeps fuzzing, `status.sh`
looks healthy, and you only find out at analysis time.

| Condition | Check | What it catches |
|---|---|---|
| all | 3 fuzzers alive | a CMPLOG/target mismatch or OOM killing an instance |
| all | every `fuzzer_stats` < 120s old | the persistent-mode futex child-sync deadlock on ical/tidy: stats freeze while `afl-whatsup` still says "alive" |
| all | execs/s above a per-target floor | one oversized seed pinning the queue (this cost an hour at 4.5 execs/s on tidy once) |
| all | coverage rising over 45s | nothing is actually progressing |
| all | `AFL_SYNC_TIME=1`, `AFL_FUZZER_STATS_UPDATE_INTERVAL=10` in `afl_env.txt` | an arm fuzzed with different settings from the others, so it is not comparable |
| all | `AFL_OLD_CHILD_SYNC=1` (ical, tidy), `ASAN_OPTIONS=detect_leaks=0` (tidy) | the deadlock above; and libtidy's upstream ~168-byte-per-parse leak turning every input into a "crash" |
| all | seed count + hash match the frozen fingerprint | a drifted corpus |
| `baseline` | **no** mutator env vars, **no** mutator loaded, no analyzer/daemon pids, `agentafl_hook=0` | a stray `AFL_PYTHON_MODULE` in your shell quietly making "baseline" adaptive |
| `minyee` | mutator loaded in **all 3** instances | the arm being statistically identical to baseline |
| `minyee` | analyzer alive, `eval_windows > 0`, no `UnicodeEncodeError` | calibrating forever, or the U+0394 encoding trap swallowing every trigger |
| `minyee` | `raw_metrics.json` growing | `monitor.py` locked onto the wrong `fuzzer_stats` path |
| `combined` | `agentafl_hook=1`, daemon alive, no `FATAL` | a run that can never escalate |
| `combined` | showmap self-test sees > 50 edges | **the `@@` trap** - see below |
| `combined` | API key resolves | every escalation dying at the LLM call |

`escalations=0` at 15 minutes is **normal**, not a failure: the hook only fires after a
Level-1 coverage adaptation has failed to recover, typically ~30 min in. It is reported as
INFO.

### The `@@` trap

`libxml2` is an ordinary CLI program (`xmllint --noout <file>`), so one argument list
serves everything. The `libical` and `tidy` harnesses are libFuzzer-style
(`LLVMFuzzerTestOneInput` + AFL++'s `libAFLDriver`) and behave differently depending on
who runs them:

- under **`afl-fuzz`** the driver detects it and takes the testcase over **shared memory**,
  ignoring any file argument. That is where the exec/s comes from, so passing `@@` here
  would throw the persistent-mode speedup away.
- under **`afl-showmap`** it reads a **file argument**, and
  `evaluate_seeds.run_showmap()` passes the candidate *only* by substituting `@@` in that
  list (`AgentAFL/evaluate_seeds.py:54-56`). There is no stdin fallback.

So `targets.sh` keeps two separate lists, `FUZZ_ARGS` and `SHOWMAP_ARGS`, and both are
recorded in `meta.txt` as `target_cmd=` and `showmap_cmd=`. Measured on
`libical_fuzzer-asan` with three different seeds:

```
with @@      701 / 544 / 240 edges     per-seed, correct
without @@     1 /   1 /   1 edges     constant - every seed identical
```

Getting this wrong is invisible: `build_baseline` ignores `afl-showmap`'s exit code, so
every generated seed scores 0 new edges, none is ever judged useful, and `combined`
quietly degrades to `minyee` for the whole 12h. Both `preflight_agentafl.sh` and
`verify_run.sh` test for it, and the preflight additionally re-runs without `@@` to prove
the check itself can fail.

(`afl-showmap`'s own `-C -i <dir>` collect mode, which `build_baseline` uses to freeze the
corpus, handles the directory itself and is unaffected - ical measures 982 edges either
way. It is the per-candidate path that breaks.)

## 7. Collect results, then terminate

The next day the VMs will be in the *Stopped* state (no compute charge, disk still kept).
For each one:

1. Instance state → **Start**, wait for "Running", copy the new public IP
2. On your laptop:
   ```bash
   mkdir -p results
   scp -i afl-key.pem 'ubuntu@<ip>:~/fyp/results/*.tar.gz' results/
   tar -tzf results/<target>_<condition>_run1.tar.gz | head   # check it opens
   ```
3. Instance state → **Terminate**

When a whole target is collected you should have 15 tarballs for it, named
`<target>_{baseline,minyee,combined}_run{1..5}.tar.gz`, and 45 once all three targets are
done. Check the count before terminating anything - a missing tarball means that VM's
`finish_campaign.sh` did not complete, and the run dir is still on its disk.

## What's in each results tarball

```
<target>_<condition>_run<N>/
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

Anything here that changes invalidates the comparison, across conditions *and* across
targets. `run_campaign.sh` records all of it into every run's `meta.txt`, and
`verify_run.sh` asserts the parts that are machine-checkable.

### Shared by all nine cells

| | value |
|---|---|
| instance type | `c7i.xlarge` (4 vCPU, 8 GiB), ap-southeast-2 |
| instances | 3: `main` (`-M main -c <target>-cmplog`, `AFL_CYCLE_SCHEDULES=1`), `sec1 -p explore`, `sec2 -p exploit` |
| duration | `43200` (12h), via `-V` |
| common flags | `-m none -t 1000` |
| `AFL_SYNC_TIME` | **`1`** (AFL++ default is 20) |
| `AFL_FUZZER_STATS_UPDATE_INTERVAL` | **`10`** (AFL++ default is 60) |
| other AFL env | `AFL_SKIP_CPUFREQ=1`, `AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1`, `AFL_IMPORT_FIRST=1`, `AFL_NO_UI=1` |
| AFL++ | `v5.03c` (`afl-fuzz++5.03c`), `make source-only` |
| ASan | **on for all three targets** (`AFL_USE_ASAN=1`) |
| dictionaries | none, on any target |

The last two settings are the ones that drifted last time, so they are in the table now:
both are set for **baseline, minyee and combined alike**, so the only difference between
arms is which of our layers is active. See the `AFL_SYNC_TIME` section below for why.

### Per target

| | `libxml2` | `libical` | `tidy` |
|---|---|---|---|
| upstream | `GNOME/libxml2` `v2.15.4` (`96498992`) | `libical/libical` branch `4.0` @ `3d0c8c36` (`v4.0.5-44`) | `htacg/tidy-html5` branch `next` @ `d08ddc28` (5.9.20) |
| harness | `xmllint` (real CLI) | `harnesses/libical_fuzzer.cc` | `harnesses/tidy_parse_string_fuzzer.c` |
| binary | `xmllint-afl` | `libical_fuzzer-afl` | `tidy_parse_string_fuzzer-afl` |
| `target_cmd` (afl-fuzz) | `--noout @@` | *(none - shmem)* | *(none - shmem)* |
| `showmap_cmd` | `--noout @@` | `@@` | `@@` |
| seeds | `AgentAFL/seeds_min` | `cloud/seeds/ical` | `cloud/seeds/html` |
| seed count / hash | 111 / `52974a6a2190a399` | 33 / `2e4bd513ed8eab50` | 317 / `d7bf98169294ab62` |
| extra env | *(none)* | `AFL_OLD_CHILD_SYNC=1` | `AFL_OLD_CHILD_SYNC=1`, `ASAN_OPTIONS=detect_leaks=0` |
| prompt format name | `XML document` | `iCalendar document` | `HTML document` |

Neither libical nor tidy sits on a clean tag, so both are pinned by **commit**, not tag.
Both are CMake; the flags are in `build_libical.sh` / `build_tidy.sh`, recovered from the
`CMakeCache.txt` of the original hand-made builds. ASan and CMPLOG come from
`AFL_USE_ASAN=1` / `AFL_LLVM_CMPLOG=1` at compile time, **not** from any CMake option
(`LIBICAL_DEVMODE_ADDRESS_SANITIZER` stays `OFF`).

The format names are not free text. Nothing downstream validates `--fmt` - it is spliced
straight into the prompt - so a typo degrades seed generation silently. These three
strings are exactly what the `prompt_tests/results3` runs recorded, which is where the
5x5 call budget was measured.

### Seed corpora: why two of them are capped

Both non-xml corpora had a size outlier that wrecks throughput, because a single huge
queue entry can pin an instance for hours:

- **tidy** uses `seeds-tidy-8k` (317 files, 8 KB cap). Uncapped, a 53 KB seed cost
  ~253 ms/exec and held four of six instances at ~4.5 execs/s for over an hour; tidy's
  parse+repair+diagnostics+serialize is super-linear in document length, and an
  `AFL_DISABLE_TRIM=1` A/B showed trimming is not the fix. The 7 dropped seeds were worth
  34 of 3642 edges (0.93%).
- **ical** is capped at **32 KB**, which drops exactly one file (`large.ics`, 1,044,540
  bytes - 89% of the corpus by volume, and 20x the tidy seed that caused the stall).
  Measured edge loss against the full corpus: **0.31%** (979 of 982 edges, 33 of 34
  files). An 8 KB cap was tried and rejected: it drops 7 files for 2.34% loss.

`SEEDS=/path` still overrides, but then the hash no longer matches and `verify_run.sh`
will say so.

### Layout lineage

`run_campaign.sh` uses the `run_tidy.sh` / `run_libical.sh` layout (main with CMPLOG,
secondaries with explore/exploit/...). The older local XML command in
`AgentAFL/common_commands.md` used a different one (CMPLOG on sec1, no `-p`) - don't copy
from there.

### The superseded batch in `baseline/` and `resource_aware/`

The 10 tarballs committed under `baseline/run1..5/` and `resource_aware/run1..5/` are from
12-13 Oct and are kept **for reference only**. They are not comparable to anything run
against the current code, on four counts:

| | those tarballs | a run today |
|---|---|---|
| `AFL_SYNC_TIME` | unset → 20 min | `1` |
| `AFL_FUZZER_STATS_UPDATE_INTERVAL` | unset → 60 s | `10` |
| `controller.evaluate_adaptation` | retained a successful mode | always returns to `DEFAULT` |
| `run_analyzer.py` Coverage Retry | absent | present, and **not** gated by `AGENTAFL_HOOK` |

Check it yourself: `afl_env.txt` inside either tarball lists only four or five `AFL_*`
vars, with no sync time and no stats interval, and no committed `meta.txt` has an
`afl_sync_time=` line. The effect is measurable - those minyee runs show
`analyzer_eval_windows=712` against `monitor_windows=1443`, exactly the half-discarded
pattern a 60s stats interval produces against a 30s monitor window. At `10` a run today
evaluates roughly twice as many windows, so the Level-1 loop gets about twice the trigger
opportunities. Two of the five also end with `final_mode=REDUCE_MUTATION_SIZE`, which is
only reachable under the old retain-on-improvement controller.

Do not mix them into the new matrix.

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
adaptation is one extra mutation exec per queue entry. Expect `<target>_minyee` coverage
around `<target>_baseline`, within noise, on all three targets. The reportable output of this arm is the
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
3. ~~`run_analyzer.py` dereferences `controller.last_adaptation["trigger_reason"]` after
   `evaluate_adaptation()` has set it to `None`, pinning the signal at the last
   non-DEFAULT mode.~~ **Re-checked and withdrawn.** `trigger_reason` is read at
   `run_analyzer.py:420`, *before* the `evaluate_adaptation()` call at :617, in both `HEAD`
   and `ae015802`; and `apply_level1_adaptation` only returns a non-`None` mode after
   setting `last_adaptation`, so the two are set and cleared together. At `HEAD` the
   outcome is unreachable anyway, because `evaluate_adaptation` now always writes
   `DEFAULT`. Don't cite this as a limitation in the write-up.

   The *monitoring* for it is still worth keeping, since a stuck mode would matter however
   it arose: `status.sh` surfaces a climbing `errors=` count plus a mode unchanged for over
   30 minutes, and `summary.txt` records `analyzer_errors=`.
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
echo 'CLAUDE_API_KEY=sk-ant-...' > ~/fyp/FYP/AgentAFL/.env
./preflight_resource_aware.sh                        # once per machine
AGENTAFL_TARGET=<target> ./preflight_agentafl.sh     # once per machine PER TARGET
./run_campaign.sh <target> combined 1 3 43200
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
./run_campaign.sh <target> minyee 1 3 43200
```

With `AGENTAFL_HOOK` unset or `0`, the **AgentAFL escalation hook** in `run_analyzer.py`
is dead code — the `try` is never entered and no file is written. Verified by replaying her
committed metrics through the patched file with the gate off and with the gate on: only
the latter creates `.agentafl_request`. So the Level-2 escalation is cleanly opt-in, and a
`minyee` run never writes that file.

That is a narrower claim than it may look, and the distinction matters for the write-up:
**a `minyee` run at `HEAD` is not her code as committed on 13 Oct.** Two changes to the
Level-1 layer are *not* gated by `AGENTAFL_HOOK` and apply to every `minyee` and
`combined` run:

- `controller.evaluate_adaptation()` used to retain a successful mode; it now always
  writes `DEFAULT`.
- `run_analyzer.py` gained the Coverage Retry block, which clears the stagnation latch
  whenever recovery yields zero edges.

Both are deliberate and both are what the new batch measures — but reproducing the Oct-13
tarballs exactly would need her `controller.py` and `run_analyzer.py` at `ae015802`. That
is not wired up, because you would be comparing against superseded behaviour.

`agentafl_hook=`, `afl_sync_time=` and `afl_fuzzer_stats_update_interval=` are recorded in
`meta.txt` for every condition — `afl_env.txt` only captures `^AFL_|^ASAN_`, so the hook
state would otherwise be invisible.

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

`AFL_FUZZER_STATS_UPDATE_INTERVAL=10` is set for the same reason. AFL++ writes
`fuzzer_stats` every 60s by default, and `run_analyzer.py` discards any window whose
`execs_done` has not moved ("Skipping stale AFL++ stats"), so against `monitor.py`'s 30s
window roughly every other window was thrown away — her effective plateau-detection
granularity was 60s, not the 30s the window implies. Publishing stats at least as often as
the monitor samples fixes that. It is set on all three arms so the fuzzer behaves
identically in each.

Both are asserted by `verify_run.sh`, because an arm that silently ran with different
values is not comparable and nothing else would notice.

### Failure modes to check, in order

All three are silent - the campaign fuzzes happily and the arm just equals `minyee`:

| symptom in `summary.txt` / `status.sh` | meaning |
|---|---|
| `escalations=0`, `requests_dropped=0` | her hook never fired; check the hook is present and that coverage adaptations are happening at all |
| `escalations>0`, `seeds_injected=0` | `afl-addseeds` refused - usually no `*/fuzzer_stats` yet |
| `seeds_injected>0`, `synced_addseeds_main=0` | seeds written but no fuzzer imported them |
| `seeds_with_new_coverage=0` across every escalation, on `libical`/`tidy` | the `@@` trap: `showmap_cmd` in `meta.txt` has no `@@`, so every candidate scored an identical map |

On `libical` and `tidy` there is a fourth, which is not specific to the escalation and
affects **all three conditions**: if `fuzzer_stats` stops updating while `afl-whatsup`
still reports every instance alive, that is the persistent-mode futex child-sync deadlock.
Check `AFL_OLD_CHILD_SYNC=1` is in `afl_env.txt`. `verify_run.sh` tests for it directly;
it is the single most expensive failure here because the run looks completely healthy.

That last one is the trap worth knowing: a campaign started **without** `-M`/`-S`
auto-configures as `-S default`, which is a *secondary*, and secondaries skip any sync
directory lacking an `is_main_node` marker - so `afl-addseeds` would import nothing, with no
error. `run_campaign.sh` always uses `-M main`, which syncs from every sibling directory, so
this only bites hand-rolled local runs (including `ResourceAwareAFL/run_six_experiments.sh`).
`agentafl_cycle.ensure_syncable()` writes the marker when it detects no main node.

### Tests

```bash
cd ResourceAwareAFL && python3 test_agentafl_cycle.py      # 28 tests, no API key needed
```

The one that matters is `test_second_escalation_sees_first_escalations_seeds`.
