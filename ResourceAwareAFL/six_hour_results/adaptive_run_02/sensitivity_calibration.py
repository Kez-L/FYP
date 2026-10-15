import csv
import statistics

FILES = [
    "sensitivity_results_v2/run_01.csv",
    "sensitivity_results_v2/run_02.csv",
    "sensitivity_results_v2/run_03.csv",
]

CALIBRATION_VALUES = [5, 10, 15, 20, 30]
TOTAL_WINDOWS = 85


def read_values(filepath):
    with open(filepath, newline="") as f:
        rows = list(csv.DictReader(f))[:TOTAL_WINDOWS]

    cpu = [float(row["cpu_percent"]) for row in rows]
    memory = [float(row["memory_mb"]) for row in rows]

    return cpu, memory


def median_mad(values):
    median = statistics.median(values)

    deviations = [
        abs(value - median)
        for value in values
    ]

    mad = statistics.median(deviations)

    return median, mad


for filepath in FILES:

    cpu, memory = read_values(filepath)

    print("\n======================================")
    print(filepath)
    print("======================================")

    for n in CALIBRATION_VALUES:

        cpu_cal = cpu[:n]
        memory_cal = memory[:n]

        cpu_median, cpu_mad = median_mad(cpu_cal)
        mem_median, mem_mad = median_mad(memory_cal)

        print(
            f"N={n:<2} | "
            f"CPU median={cpu_median:6.2f}% "
            f"MAD={cpu_mad:5.2f} | "
            f"Memory median={mem_median:7.3f} MB "
            f"MAD={mem_mad:6.3f}"
        )
