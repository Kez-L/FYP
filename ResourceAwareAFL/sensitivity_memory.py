import csv
import statistics

FILES = [
    "sensitivity_results_v2/run_01.csv",
    "sensitivity_results_v2/run_02.csv",
    "sensitivity_results_v2/run_03.csv",
]

CALIBRATION_WINDOWS = 10
K_VALUES = [0.5, 1.0, 1.5, 2.0, 3.0]
MIN_MEMORY_GROWTH_MB = 1.0


def load_memory(filename):
    with open(filename, newline="") as f:
        rows = list(csv.DictReader(f))

    return [float(row["memory_mb"]) for row in rows][:85]


def count_isolated_triggers(triggered):
    count = 0

    for i in range(len(triggered)):
        previous = triggered[i - 1] if i > 0 else False
        current = triggered[i]
        following = (
            triggered[i + 1]
            if i < len(triggered) - 1
            else False
        )

        if current and not previous and not following:
            count += 1

    return count


results = {}

print("=" * 75)
print("MEMORY THRESHOLD SENSITIVITY ANALYSIS")
print("=" * 75)

for filename in FILES:

    memory = load_memory(filename)

    calibration = memory[:CALIBRATION_WINDOWS]
    runtime = memory[CALIBRATION_WINDOWS:]

    calibration_deltas = [
        calibration[i] - calibration[i - 1]
        for i in range(1, len(calibration))
    ]

    mean_growth = statistics.mean(calibration_deltas)
    std_growth = statistics.stdev(calibration_deltas)

    # Match run_analyzer behaviour:
    # previous_memory after calibration is the final
    # calibration memory measurement.
    previous = calibration[-1]

    runtime_growth = []

    for current in runtime:
        growth = current - previous
        runtime_growth.append(growth)
        previous = current

    results[filename] = {}

    print(f"\n{filename}")
    print(f"  Total samples: {len(memory)}")
    print(f"  Calibration samples: {len(calibration)}")
    print(f"  Runtime samples: {len(runtime)}")
    print(f"  Mean calibration growth: {mean_growth:.4f} MB")
    print(f"  Std calibration growth: {std_growth:.4f} MB")

    for k in K_VALUES:

        calculated_threshold = (
            mean_growth + k * std_growth
        )

        threshold = max(
            calculated_threshold,
            MIN_MEMORY_GROWTH_MB
        )

        triggered = [
            growth > 0 and growth >= threshold
            for growth in runtime_growth
        ]

        trigger_count = sum(triggered)

        trigger_frequency = (
            trigger_count / len(runtime_growth) * 100
        )

        isolated = count_isolated_triggers(triggered)

        results[filename][k] = {
            "threshold": threshold,
            "trigger_count": trigger_count,
            "frequency": trigger_frequency,
            "isolated": isolated,
        }


print("\n" + "=" * 75)
print("MEMORY TRIGGER FREQUENCY (%)")
print("=" * 75)

print(
    f"{'k':<8}"
    f"{'Run 1':<12}"
    f"{'Run 2':<12}"
    f"{'Run 3':<12}"
    f"{'Average':<12}"
)

for k in K_VALUES:

    values = [
        results[f][k]["frequency"]
        for f in FILES
    ]

    print(
        f"{k:<8}"
        f"{values[0]:<12.2f}"
        f"{values[1]:<12.2f}"
        f"{values[2]:<12.2f}"
        f"{statistics.mean(values):<12.2f}"
    )


print("\n" + "=" * 75)
print("ISOLATED MEMORY TRIGGERS")
print("=" * 75)

print(
    f"{'k':<8}"
    f"{'Run 1':<12}"
    f"{'Run 2':<12}"
    f"{'Run 3':<12}"
    f"{'Average':<12}"
)

for k in K_VALUES:

    values = [
        results[f][k]["isolated"]
        for f in FILES
    ]

    print(
        f"{k:<8}"
        f"{values[0]:<12}"
        f"{values[1]:<12}"
        f"{values[2]:<12}"
        f"{statistics.mean(values):<12.2f}"
    )


print("\n" + "=" * 75)
print("MEMORY THRESHOLDS (MB)")
print("=" * 75)

print(
    f"{'k':<8}"
    f"{'Run 1':<12}"
    f"{'Run 2':<12}"
    f"{'Run 3':<12}"
    f"{'Average':<12}"
)

for k in K_VALUES:

    values = [
        results[f][k]["threshold"]
        for f in FILES
    ]

    print(
        f"{k:<8}"
        f"{values[0]:<12.4f}"
        f"{values[1]:<12.4f}"
        f"{values[2]:<12.4f}"
        f"{statistics.mean(values):<12.4f}"
    )
