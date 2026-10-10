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
   the small test machine in step 1 but **not** for a `c7i.2xlarge` (8 vCPUs).

## Which machine size

Your laptop runs 6 fuzzer instances on 8 CPUs / 10 GB. The closest match is
**`c7i.2xlarge` (8 vCPUs, 16 GiB)** running 6 fuzzers, which leaves 2 vCPUs spare for the
loggers now and Min Yee's analyzer / AgentAFL later, so every condition gets the same 6
fuzzers. Don't use `t2`/`t3`/`t4g` types (they throttle) or Lightsail.

| Per 12-hour run | N. Virginia | Sydney |
|---|---|---|
| `c7i.2xlarge` (8 vCPU) | ~US$4.30 | ~US$5.60 |
| 5 runs (one target, one condition) | ~US$21 | ~US$28 |
| All 45 runs | ~US$195 | ~US$250 |

Plus disk: 30 GiB costs about US$0.10/day while the VM exists (running or stopped).

---

## 1. Launch a test VM

EC2 → Launch instance:

- **Name:** `afl-test`
- **AMI:** Ubuntu Server 24.04 LTS, 64-bit (x86)
- **Instance type:** `c7i.large` while you wait for the quota, `c7i.2xlarge` once approved
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
   Type `c7i.2xlarge`, same key pair and security group, **Number of instances: 5**.
2. SSH into each VM and start one run, with a different run id on each:

   ```bash
   cd ~/fyp/FYP/cloud
   ./run_campaign.sh libxml2 baseline 1      # VM 1
   ./run_campaign.sh libxml2 baseline 2      # VM 2  ... up to 5
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
  monitor/raw_metrics.json Min Yee's monitor.py, every 10s
  logs/                    per-process logs
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

## Things to decide / check before the real baseline

- **Instance layout.** `run_campaign.sh` uses the `run_tidy.sh` / `run_libical.sh` layout
  (main with CMPLOG, secondaries with explore/exploit/coe/fast/rare). Your local XML
  command used a different layout (CMPLOG on sec1, no `-p`). Pick one and use it for
  every target and condition.
- **Seeds.** Defaults to `AgentAFL/seeds_min` (111 files). Override with `SEEDS=/path`.
- **Freeze the setup.** Baselines only stay valid if AFL++ version, libxml2 commit,
  build flags, seeds, layout and instance type are the same for the later conditions.

## Notes on Min Yee's monitor (found while wiring it in)

Running as a passive logger works, but these affect the data it records:

1. It reads `unique_crashes` from `fuzzer_stats`; AFL++ 4.x/5.x writes `saved_crashes`,
   so its crash count is always 0.
2. It measures CPU/memory of whichever `afl-fuzz` process it finds first, not the whole
   campaign, and an `afl-fuzz` process's own CPU excludes the target it runs.
3. Coverage comes from `bitmap_cvg` (a percentage with 2 decimals), which moves much less
   finely than `edges_found`.
4. The controller's signal file is in the repo folder (`ResourceAwareAFL/adaptation/`),
   so two campaigns on one machine would share it. One campaign per VM avoids this.
5. `main.py` and `run_analyzer.py` both look like entry points - worth confirming which
   one the integrated pipeline uses.
