sudo bash -c 'echo core > /proc/sys/kernel/core_pattern'
cd 1git-folder/AgentAFL

## Running XML
6 instances

AFL_SKIP_CPUFREQ=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 \
afl-fuzz -i - -o /home/user/Documents/afl-output-libxml2 -M main \
  -- /home/user/Documents/AFLPlus/libxml2-build/xmllint-afl --noout @@

AFL_SKIP_CPUFREQ=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 \
afl-fuzz -i - -o /home/user/Documents/afl-output-libxml2 -S sec1 \
  -c /home/user/Documents/AFLPlus/libxml2-build/xmllint-cmplog \
  -- /home/user/Documents/AFLPlus/libxml2-build/xmllint-afl --noout @@

AFL_SKIP_CPUFREQ=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 \
afl-fuzz -i - -o /home/user/Documents/afl-output-libxml2 -S sec2 \
  -- /home/user/Documents/AFLPlus/libxml2-build/xmllint-afl --noout @@

AFL_SKIP_CPUFREQ=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 \
afl-fuzz -i - -o /home/user/Documents/afl-output-libxml2 -S sec3 \
  -- /home/user/Documents/AFLPlus/libxml2-build/xmllint-afl --noout @@


AFL_SKIP_CPUFREQ=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 \
afl-fuzz -i - -o /home/user/Documents/afl-output-libxml2 -S sec4 \
  -- /home/user/Documents/AFLPlus/libxml2-build/xmllint-afl --noout @@

AFL_SKIP_CPUFREQ=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 \
afl-fuzz -i - -o /home/user/Documents/afl-output-libxml2 -S sec5 \
  -- /home/user/Documents/AFLPlus/libxml2-build/xmllint-afl --noout @@

# Plateau watch

python3 /home/user/Documents/1git-folder/AgentAFL/plateau_watch.py --output-dir /home/user/Documents/1git-folder/AgentAFL/afl-output-libxml2 --interval 60 --plateau-secs 900 --log /home/user/Documents/1git-folder/AgentAFL/plateau_log_libxml2.csv


# Orchestrator

python3 /home/user/Documents/1git-folder/AgentAFL/agentafl_orchestrator.py --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-libxml2 --instance main --format "XML document" --seed-kind text --seed-ext .xml --plateau-log /home/user/Documents/1git-folder/AgentAFL/plateau_log_libxml2.csv --runs-root /home/user/Documents/1git-folder/AgentAFL/agentafl_runs --run-label xml-run --env-file /home/user/Documents/1git-folder/AgentAFL/.env --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/libxml2-build/xmllint-afl --target-args "--noout @@" --poll-interval-s 300 --plateau-threshold-s 900 --injection-cooldown-s 900 --max-llm-calls 500 --max-queue-size 100000 --n-seeds 3 --n-generate 5 --llm-provider gemini --llm-model gemini-3.5-flash-lite --llm-temperature 0.9 --run-duration-hours 24


## Running TIFF
6 instances

AFL_AUTORESUME=1 AFL_CYCLE_SCHEDULES=1 AFL_FAST_CAL=1 AFL_IMPORT_FIRST=1 afl-fuzz -i AFLPlus/seeds-libtiff/ -o afl-output-libtiff -M main -c AFLPlus/libtiff-build/tiffinfo-cmplog -- AFLPlus/libtiff-build/tiffinfo-afl -D @@

AFL_AUTORESUME=1 afl-fuzz -i AFLPlus/seeds-libtiff/ -o afl-output-libtiff -S sec1 -p explore -- AFLPlus/libtiff-build/tiffinfo-afl -D @@

AFL_AUTORESUME=1 afl-fuzz -i AFLPlus/seeds-libtiff/ -o afl-output-libtiff -S sec2 -p exploit -- AFLPlus/libtiff-build/tiffinfo-afl -D @@

AFL_AUTORESUME=1 afl-fuzz -i AFLPlus/seeds-libtiff/ -o afl-output-libtiff -S sec3 -p fast -- AFLPlus/libtiff-build/tiffinfo-afl -D @@

AFL_AUTORESUME=1 afl-fuzz -i AFLPlus/seeds-libtiff/ -o afl-output-libtiff -S sec4 -p coe -- AFLPlus/libtiff-build/tiffinfo-afl -D @@

AFL_AUTORESUME=1 afl-fuzz -i AFLPlus/seeds-libtiff/ -o afl-output-libtiff -S sec5 -p rare -- AFLPlus/libtiff-build/tiffinfo-afl -D @@


## Plateau watch
python3 plateau_watch.py --output-dir afl-output-libtiff --interval 60 --plateau-secs 900 --log plateau_log_libtiff.csv

## Orchestrator

`smoke test no API`
python3 agentafl_orchestrator.py --campaign-root afl-output-libtiff --instance main --format "TIFF image" --seed-kind binary --seed-ext .tif --format-hint "Byte order little-endian ('II'); IFD entry = tag(2)+type(2)+count(4)+value(4)." --plateau-log plateau_log_libtiff.csv --runs-root agentafl_runs --run-label tiff-smoke --env-file .env --target AFLPlus/libtiff-build/tiffinfo-afl --target-args "-D @@" --max-queue-size 100000 --once --dry-run

`One cycle with API call but no injection into run`
python3 agentafl_orchestrator.py --campaign-root afl-output-libtiff --instance main --format "TIFF image" --seed-kind binary --seed-ext .tif --plateau-log plateau_log_libtiff.csv --runs-root agentafl_runs --run-label tiff-dryeval --env-file .env --target AFLPlus/libtiff-build/tiffinfo-afl --target-args "-D @@" --max-queue-size 100000 --plateau-threshold-s 0 --llm-provider claude --llm-model claude-sonnet-5 --once --no-inject

python3 agentafl_orchestrator.py --campaign-root afl-output-libtiff --instance main --format "TIFF image" --seed-kind binary --seed-ext .tif --plateau-log plateau_log_libtiff.csv --runs-root agentafl_runs --run-label tiff-dryeval --env-file .env --target AFLPlus/libtiff-build/tiffinfo-afl --target-args "-D @@" --max-queue-size 100000 --plateau-threshold-s 0 --llm-provider claude --llm-model claude-sonnet-5 --once --no-inject

`Real run for binary`
python3 agentafl_orchestrator.py --campaign-root afl-output-libtiff --instance main --format "TIFF image" --seed-kind binary --seed-ext .tif --plateau-log plateau_log_libtiff.csv --runs-root agentafl_runs --run-label tiff-run --env-file .env --target AFLPlus/libtiff-build/tiffinfo-afl --target-args "-D @@" --poll-interval-s 300 --plateau-threshold-s 900 --injection-cooldown-s 900 --max-llm-calls 500 --max-queue-size 100000 --n-seeds 3 --n-generate 5 --llm-provider claude --llm-model claude-sonnet-5 --run-duration-hours 1

`Real run for binary, Gemini instead (needs GEMINI_API_KEY in .env)`
python3 agentafl_orchestrator.py --campaign-root afl-output-libtiff --instance main --format "TIFF image" --seed-kind binary --seed-ext .tif --plateau-log plateau_log_libtiff.csv --runs-root agentafl_runs --run-label tiff-run --env-file .env --target AFLPlus/libtiff-build/tiffinfo-afl --target-args "-D @@" --poll-interval-s 300 --plateau-threshold-s 900 --injection-cooldown-s 900 --max-llm-calls 500 --max-queue-size 100000 --n-seeds 3 --n-generate 5 --llm-provider gemini --llm-model gemini-3.5-flash-lite --llm-temperature 0.9 --run-duration-hours 24


## Running HTML (tidy-html5)
6 instances

Target: OSS-Fuzz `tidy_parse_string_fuzzer` harness (whole input = one HTML document ->
`tidyParseString` + `tidyCleanAndRepair` + `tidyRunDiagnostics` + `tidySaveBuffer`). Same
shape as the libical harness, so html stays comparable to ical. No `-x` dictionary, matching
the dictionary-free xml and ical campaigns.

Once per reboot (needs sudo):

sudo bash -c 'echo core > /proc/sys/kernel/core_pattern'

Launch the 12h campaign (all 6 instances, see run_tidy.sh for the per-instance flags):

cd /home/user/Documents/1git-folder/AgentAFL && ./run_tidy.sh

# ./run_tidy.sh /some/out 3600    # custom output dir + duration (seconds)

## Plateau watch

python3 /home/user/Documents/1git-folder/AgentAFL/plateau_watch.py --output-dir /home/user/Documents/1git-folder/AgentAFL/afl-output-tidy-20261006 --interval 60 --plateau-secs 900 --log /home/user/Documents/1git-folder/AgentAFL/plateau_log_tidy_20261006.csv

## Status / health

watch -n10 'afl-whatsup -s /home/user/Documents/1git-folder/AgentAFL/afl-output-tidy-20261006'

for d in /home/user/Documents/1git-folder/AgentAFL/afl-output-tidy-20261006/*/; do echo "$(basename $d) last_update=$(( $(date +%s) - $(awk -F: '/^last_update/{print $2}' $d/fuzzer_stats) ))s ago"; done

A last_update that stops rising while afl-whatsup still reports the instance alive is the
persistent-mode child-sync deadlock. AFL_OLD_CHILD_SYNC=1 in run_tidy.sh is there to prevent
it; if it happens anyway, drop to 4 instances the way run_libical.sh does.

## Gotcha: @@ or no @@

libAFLDriver.a ignores argv file arguments when it detects it is running under afl-fuzz, and
takes input over shared memory in a persistent loop instead. So:

  - afl-fuzz       -> NO @@  (shmem persistent mode; this is where the exec/s comes from)
  - afl-showmap    -> NEEDS @@ (outside afl-fuzz the driver reads a file argument)
  - evaluate_seeds / run_build_context.py --target-args -> "@@"

Feeding the driver on plain stdin silently parses an EMPTY document - every input yields the
same 876-tuple map. If a coverage number ever looks suspiciously constant, check this first.

## Gotcha: afl-cmin does not work against this target

afl-cmin injects `-H <fixed file>` whenever it sees @@ in the target args, which collides with
its own batch `-I filelist` mode here: every per-input trace comes back empty, gets classified
as a crash, and is dropped ("Found 0 unique tuples across 386 files"). Without @@ it fails
earlier with "no instrumentation output detected", because the driver will not read stdin.

Minimise with afl-showmap's directory mode instead, then apply cmin's own selection rule
(files sorted smallest-first; each tuple claimed by the smallest file covering it; keep every
file that claims at least one tuple). That produced AFLPlus/seeds-tidy: 401 -> 324 files,
289 KB, lossless over all 12255 (edge, hit-count-bucket) tuples.

## Crash triage (ASAN build)

ALWAYS set detect_leaks=0. libtidy leaks ~168 bytes per parse upstream (src/alloc.c
defaultAlloc), so with LSan on, every single input reports as a failure:

ASAN_OPTIONS=detect_leaks=0 /home/user/Documents/1git-folder/AgentAFL/AFLPlus/tidy-html5-build/tidy_parse_string_fuzzer-asan <crash-file>
