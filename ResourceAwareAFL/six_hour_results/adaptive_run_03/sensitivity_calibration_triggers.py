import csv
import statistics

FILES = [
    "sensitivity_results_v2/run_01.csv",
    "sensitivity_results_v2/run_02.csv",
    "sensitivity_results_v2/run_03.csv",
]

CALIBRATION_VALUES = [5, 10, 15, 20, 30]
TOTAL_WINDOWS = 85
CPU_K = 1.5

# Use windows 31-85 as the same evaluation region
# for every calibration size.
EVALUATION_START = 30


def read_cpu(filepath):
    with open(filepath, newline="") as f:
        rows = list(csv.DictReader(f))[:TOTAL_WINDOWS]

    return [
        float(row["cpu_percent"])
        for row in rows
    ]


def threshold(values):
    median = statistics.median(values)

    deviations = [
        abs(value - median)
        for value in values
    ]

    mad = statistics.median(deviations)

    return median, mad, median + CPU_K * mad


for filepath in FILES:

    cpu = read_cpu(filepath)

    evaluation = cpu[EVALUATION_START:]

    print("\n======================================")
    print(filepath)
    print("======================================")

    for n in CALIBRATION_VALUES:

        calibration = cpu[:n]

        median, mad, cpu_threshold = threshold(
            calibration
        )

        abnormal = [
            value >= cpu_threshold
            for value in evaluation
        ]

        count = sum(abnormal)

        frequency = (
            count / len(evaluation) * 100
        )

        print(
            f"N={n:<2} | "
            f"median={median:5.2f}% | "
            f"MAD={mad:4.2f} | "
            f"threshold={cpu_threshold:5.2f}% | "
            f"high={count}/{len(evaluation)} | "
            f"frequency={frequency:5.2f}%"
        )