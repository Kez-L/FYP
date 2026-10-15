import csv
import statistics
from pathlib import Path

RESULT_DIR = Path("baseline_results")

for csv_file in sorted(RESULT_DIR.glob("run_*.csv")):
    with open(csv_file, newline="") as f:
        rows = list(csv.DictReader(f))

    # Keep first 85 windows
    rows = rows[:85]

    # Convert numeric values
    for row in rows:
        for key in row:
            if key != "window_id":
                row[key] = float(row[key])
        row["window_id"] = int(row["window_id"])

    first = rows[0]
    final = rows[-1]

    # Ignore Window 1 for CPU statistics because CPU=0 is
    # the monitor initialization sample.
    runtime_rows = rows[1:]

    cpu_values = [r["cpu_percent"] for r in runtime_rows]
    memory_values = [r["memory_mb"] for r in runtime_rows]

    print("=" * 60)
    print(csv_file.name)
    print("=" * 60)

    print(f"Windows:                {len(rows)}")
    print(f"Duration:               {final['elapsed_sec']:.2f} sec")

    print(f"Initial coverage:       {first['coverage_pct']:.2f}%")
    print(f"Final coverage:         {final['coverage_pct']:.2f}%")
    print(
        f"Coverage gain:          "
        f"{final['coverage_pct'] - first['coverage_pct']:.2f}%"
    )

    print(f"Initial edges:          {int(first['edges_found'])}")
    print(f"Final edges:            {int(final['edges_found'])}")
    print(
        f"Edge gain:              "
        f"{int(final['edges_found'] - first['edges_found'])}"
    )

    print(f"Average CPU:            {statistics.mean(cpu_values):.2f}%")
    print(f"Median CPU:             {statistics.median(cpu_values):.2f}%")
    print(f"Peak CPU:               {max(cpu_values):.2f}%")

    print(f"Average memory:         {statistics.mean(memory_values):.2f} MB")
    print(f"Peak memory:            {max(memory_values):.2f} MB")

    print(f"Crashes:                {int(final['saved_crashes'])}")
    print(f"Hangs:                  {int(final['saved_hangs'])}")

    print(f"CPU time:               {final['cpu_total_seconds']:.2f} sec")
    print(
        f"Memory-time:            "
        f"{final['memory_mb_seconds']:.2f} MB-sec"
    )

    print(
        f"CPU sec/new edge:       "
        f"{final['cpu_seconds_per_new_edge']:.6f}"
    )

    print(
        f"Memory MB-sec/new edge: "
        f"{final['memory_mb_seconds_per_new_edge']:.6f}"
    )

    print()