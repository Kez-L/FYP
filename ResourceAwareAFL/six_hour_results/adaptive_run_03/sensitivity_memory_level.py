import csv
import statistics

FILES = [
    "sensitivity_results_v2/run_01.csv",
    "sensitivity_results_v2/run_02.csv",
    "sensitivity_results_v2/run_03.csv",
]

CALIBRATION_WINDOWS = 10
TOTAL_WINDOWS = 85

K_VALUES = [0.5, 1.0, 1.5, 2.0, 3.0, 5.0]


def read_memory_values(filepath):
    values = []

    with open(filepath, newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            values.append(float(row["memory_mb"]))

    return values[:TOTAL_WINDOWS]


def calculate_threshold(calibration, k):
    median = statistics.median(calibration)

    deviations = [
        abs(value - median)
        for value in calibration
    ]

    mad = statistics.median(deviations)

    threshold = median + k * mad

    return median, mad, threshold


for filepath in FILES:

    print("\n======================================")
    print(filepath)
    print("======================================")

    memory_values = read_memory_values(filepath)

    calibration = memory_values[:CALIBRATION_WINDOWS]
    runtime = memory_values[CALIBRATION_WINDOWS:]

    for k in K_VALUES:

        median, mad, threshold = calculate_threshold(
            calibration,
            k
        )

        abnormal = [
            value >= threshold
            for value in runtime
        ]

        trigger_count = sum(abnormal)

        trigger_frequency = (
            trigger_count / len(runtime) * 100
        )

        # Count isolated one-window triggers
        isolated = 0

        for i, value in enumerate(abnormal):

            if not value:
                continue

            before = (
                abnormal[i - 1]
                if i > 0
                else False
            )

            after = (
                abnormal[i + 1]
                if i < len(abnormal) - 1
                else False
            )

            if not before and not after:
                isolated += 1

        print(
            f"k={k:<3} | "
            f"median={median:.4f} MB | "
            f"MAD={mad:.4f} MB | "
            f"threshold={threshold:.4f} MB | "
            f"high={trigger_count}/{len(runtime)} | "
            f"frequency={trigger_frequency:.2f}% | "
            f"isolated={isolated}"
        )
