cd /home/user/Documents/1git-folder/AgentAFL/prompt_tests

# All commands below default: --calls 15 (75 seeds), output under results3/<format-dir>/
# Run --skip-eval first if you just want to sanity-check a command's wiring/labeling
# without needing afl-showmap or spending an API call.


# ============================================================
# XML (libxml2 / xmllint) — format-dir: xml (the default, no flag needed)
# ============================================================

# --- Stage 1/2: good-seed selection criterion (negative feedback on by default) ---

# Stage 1 (Raw) — select_diverse_seeds, byte-content dedup (also includes 2 bad seeds)
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --seed-selector default

# Stage 1 (Jaccard) — same ranking as Raw, edge-Jaccard dedup instead (also includes 2 bad seeds)
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --seed-selector stage1-jaccard
# Stage 1 (Jaccard) — No 2 bad seeds
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --seed-selector stage1-jaccard --no-feedback

# Stage 2 (Coverage) — rank by total edges hit
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --seed-selector coverage

# Stage 2 (Coverage per byte) — rank by edges-per-byte density
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --seed-selector coverage-per-byte

# Stage 2 (Rare Coverage) — rank by edge rarity (AFLFast-style)
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --seed-selector rare-coverage

# --- Stage 4: feedback mode ---

# Negative only — NO good seeds at all, ever (not AFL, not AI): call 0 is Stage-0-like
# (format name only, nothing else), avoid block on from call 1 onward
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --seed-selector no-seed --label stage4_negative_only

# Positive only — no avoid block; good seeds are past AI seeds that found new coverage
# (Stage 2 Coverage supplies the good seeds until enough past-AI seeds exist)
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --seed-selector coverage --positive-feedback --no-feedback

# Negative + Positive (both) — avoid block on, AND good seeds are past AI seeds
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --seed-selector coverage --positive-feedback

# Negative only, AFL seed bootstrap — Stage 2 Coverage good seeds on call 0 only (to
# jump-start), avoid block on every round, zero good-seed examples from call 1 onward
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --seed-selector coverage --good-seeds-first-round-only --label stage4_negative_bootstrap1


# ============================================================
# iCalendar (libical) — format-dir: ical
# ============================================================

# --- Stage 1/2: good-seed selection criterion (negative feedback on by default) ---

# Stage 1 (Raw)
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --format-dir ical --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-libical-20260915 --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/libical-build/libical_fuzzer-afl --target-args "@@" --fmt "iCalendar document" --reuse-baseline results/baseline_edges_libical.json --seed-selector default

# Stage 1 (Jaccard)
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --format-dir ical --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-libical-20260915 --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/libical-build/libical_fuzzer-afl --target-args "@@" --fmt "iCalendar document" --reuse-baseline results/baseline_edges_libical.json --seed-selector stage1-jaccard

# Stage 1 (Jaccard) no feedback
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --format-dir ical --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-libical-20260915 --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/libical-build/libical_fuzzer-afl --target-args "@@" --fmt "iCalendar document" --reuse-baseline results/baseline_edges_libical.json --seed-selector stage1-jaccard --no-feedback

# Stage 2 (Coverage)
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --format-dir ical --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-libical-20260915 --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/libical-build/libical_fuzzer-afl --target-args "@@" --fmt "iCalendar document" --reuse-baseline results/baseline_edges_libical.json --seed-selector coverage

# Stage 2 (Coverage per byte)
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --format-dir ical --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-libical-20260915 --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/libical-build/libical_fuzzer-afl --target-args "@@" --fmt "iCalendar document" --reuse-baseline results/baseline_edges_libical.json --seed-selector coverage-per-byte

# Stage 2 (Rare Coverage)
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --format-dir ical --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-libical-20260915 --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/libical-build/libical_fuzzer-afl --target-args "@@" --fmt "iCalendar document" --reuse-baseline results/baseline_edges_libical.json --seed-selector rare-coverage

# --- Stage 4: feedback mode ---

# Negative only — NO good seeds at all, ever (not AFL, not AI): call 0 is Stage-0-like
# (format name only, nothing else), avoid block on from call 1 onward
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --format-dir ical --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-libical-20260915 --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/libical-build/libical_fuzzer-afl --target-args "@@" --fmt "iCalendar document" --reuse-baseline results/baseline_edges_libical.json --seed-selector no-seed --label stage4_negative_only

# Positive only — no avoid block; good seeds are past AI seeds that found new coverage
# (Stage 2 Coverage supplies the good seeds until enough past-AI seeds exist)
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --format-dir ical --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-libical-20260915 --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/libical-build/libical_fuzzer-afl --target-args "@@" --fmt "iCalendar document" --reuse-baseline results/baseline_edges_libical.json --seed-selector coverage --positive-feedback --no-feedback

# Negative + Positive (both) — avoid block on, AND good seeds are past AI seeds
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --format-dir ical --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-libical-20260915 --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/libical-build/libical_fuzzer-afl --target-args "@@" --fmt "iCalendar document" --reuse-baseline results/baseline_edges_libical.json --seed-selector coverage --positive-feedback

# Negative only, AFL seed bootstrap — Stage 2 Coverage good seeds on call 0 only (to
# jump-start), avoid block on every round, zero good-seed examples from call 1 onward
python3 run_build_context.py --provider openai --model gpt-5.6-luna --temperature 1.0 --format-dir ical --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-libical-20260915 --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/libical-build/libical_fuzzer-afl --target-args "@@" --fmt "iCalendar document" --reuse-baseline results/baseline_edges_libical.json --seed-selector coverage --good-seeds-first-round-only --label stage4_negative_bootstrap1
