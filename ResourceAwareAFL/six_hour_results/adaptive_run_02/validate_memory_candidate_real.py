import csv

FILES = [
    "sensitivity_results_v2/run_01.csv",
    "sensitivity_results_v2/run_02.csv",
    "sensitivity_results_v2/run_03.csv",
]

TOTAL_WINDOWS = 85
CALIBRATION_WINDOWS = 10

TREND_WINDOW = 5
GROWTH_THRESHOLD_MB = 0.50
PERSISTENCE_K = 3


def read_memory(filepath):
    with open(filepath, newline="") as f:
        rows = list(csv.DictReader(f))[:TOTAL_WINDOWS]

    return [
        float(row["memory_mb"])
        for row in rows
    ]


for filepath in FILES:

    memory = read_memory(filepath)

    # Ignore calibration/startup period
    runtime = memory[CALIBRATION_WINDOWS:]

    consecutive = 0
    persistent_events = 0
    first_trigger = None
    in_event = False

    print("\n======================================")
    print(filepath)
    print("======================================")

    for i in range(TREND_WINDOW, len(runtime)):

        growth = (
            runtime[i]
            - runtime[i - TREND_WINDOW]
        )

        abnormal = (
            growth >= GROWTH_THRESHOLD_MB
        )

        if abnormal:
            consecutive += 1
        else:
            consecutive = 0
            in_event = False

        if (
            consecutive >= PERSISTENCE_K
            and not in_event
        ):
            persistent_events += 1
            in_event = True

            # Actual CSV window number
            actual_window = (
                CALIBRATION_WINDOWS + i + 1
            )

            if first_trigger is None:
                first_trigger = actual_window

    print(
        f"W={TREND_WINDOW}, "
        f"T={GROWTH_THRESHOLD_MB:.2f} MB, "
        f"K={PERSISTENCE_K}"
    )

    print(
        f"Persistent events: {persistent_events}"
    )

    print(
        f"First trigger: {first_trigger}"
    )

