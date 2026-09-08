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

