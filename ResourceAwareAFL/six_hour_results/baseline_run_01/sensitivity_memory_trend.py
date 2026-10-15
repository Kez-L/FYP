import csv
import statistics

FILES = [
    "sensitivity_results_v2/run_01.csv",
    "sensitivity_results_v2/run_02.csv",
    "sensitivity_results_v2/run_03.csv",
]

TOTAL_WINDOWS = 85

# Number of monitoring windows over which memory growth is measured
TREND_WINDOWS = [2, 3, 5, 10]

# Test several growth amounts
GROWTH_THRESHOLDS_MB = [0.1, 0.25, 0.5, 1.0]


def read_memory(filepath):
    with open(filepath, newline="") as f:
        rows = list(csv.DictReader(f))[:TOTAL_WINDOWS]

    return [
        float(row["memory_mb"])
        for row in rows
    ]


for filepath in FILES:

    memory = read_memory(filepath)

    # Ignore first 10 startup/calibration windows
    runtime = memory[10:]

    print("\n======================================")
    print(filepath)
    print("======================================")

    for trend_window in TREND_WINDOWS:

        print(f"\nTrend window = {trend_window}")

        for threshold in GROWTH_THRESHOLDS_MB:

            abnormal = []

            for i in range(
                trend_window,
                len(runtime)
            ):

                growth = (
                    runtime[i]
                    - runtime[i - trend_window]
                )

                abnormal.append(
                    growth >= threshold
                )

            count = sum(abnormal)

            frequency = (
                count / len(abnormal) * 100
                if abnormal
                else 0
            )

            print(
                f"  threshold={threshold:.2f} MB | "
                f"high={count}/{len(abnormal)} | "
                f"frequency={frequency:.2f}%"
            )
