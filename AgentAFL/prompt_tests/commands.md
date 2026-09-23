cd /home/user/Documents/1git-folder/AgentAFL/prompt_tests

# XML
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --seed-selector default
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --seed-selector stage1-jaccard
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --seed-selector coverage
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --seed-selector coverage-per-byte
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --seed-selector rare-coverage



# ical
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --format-dir ical --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-libical-20260915 --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/libical-build/libical_fuzzer-afl --target-args "@@" --fmt "iCalendar document" --reuse-baseline results/baseline_edges_libical.json --seed-selector default

python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --format-dir ical --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-libical-20260915 --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/libical-build/libical_fuzzer-afl --target-args "@@" --fmt "iCalendar document" --reuse-baseline results/baseline_edges_libical.json --seed-selector stage1-jaccard

python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --format-dir ical --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-libical-20260915 --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/libical-build/libical_fuzzer-afl --target-args "@@" --fmt "iCalendar document" --reuse-baseline results/baseline_edges_libical.json --seed-selector coverage
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --format-dir ical --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-libical-20260915 --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/libical-build/libical_fuzzer-afl --target-args "@@" --fmt "iCalendar document" --reuse-baseline results/baseline_edges_libical.json --seed-selector coverage-per-byte
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --format-dir ical --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-libical-20260915 --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/libical-build/libical_fuzzer-afl --target-args "@@" --fmt "iCalendar document" --reuse-baseline results/baseline_edges_libical.json --seed-selector rare-coverage
