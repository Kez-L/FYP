import csv
import statistics
import os


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

RESULTS_DIR = "sensitivity_results_v2"

RUN_FILES = [
    "run_01.csv",
    "run_02.csv",
    "run_03.csv"
]

CALIBRATION_WINDOWS = 10

K_VALUES = [
    0.5,
    1.0,
    1.5,
    2.0,
    3.0,
    5.0,
    10.0,
    20.0,
    30.0,
    40.0
]


# ---------------------------------------------------------
# Read CPU values from CSV
# ---------------------------------------------------------

def read_cpu_values(filepath):

    cpu_values = []

    with open(filepath, "r") as file:

        reader = csv.DictReader(file)

        for row in reader:

            cpu_values.append(
                float(row["cpu_percent"])
            )

    return cpu_values


# ---------------------------------------------------------
# Analyse one run
# ---------------------------------------------------------

def analyse_run(filepath):

    cpu_values = read_cpu_values(filepath)[:85]

    # First 10 windows = calibration
    calibration_values = cpu_values[
        :CALIBRATION_WINDOWS
    ]

    # Remaining windows = runtime evaluation
    runtime_values = cpu_values[
        CALIBRATION_WINDOWS:
    ]

    # -----------------------------------------------------
    # Median
    # -----------------------------------------------------

    median_cpu = statistics.median(
        calibration_values
    )

    # -----------------------------------------------------
    # MAD
    # -----------------------------------------------------

    absolute_deviations = [
        abs(cpu - median_cpu)
        for cpu in calibration_values
    ]

    mad_cpu = statistics.median(
        absolute_deviations
    )

    results = {}

    # -----------------------------------------------------
    # Test every k value
    # -----------------------------------------------------

    for k in K_VALUES:

        threshold = (
            median_cpu +
            k * mad_cpu
        )

        triggers = [
            cpu >= threshold
            for cpu in runtime_values
        ]

        trigger_count = sum(
            triggers
        )

        trigger_percentage = (
            trigger_count /
            len(runtime_values)
        ) * 100

        # -------------------------------------------------
        # Temporary triggers
        #
        # A temporary trigger is currently defined as:
        #
        # normal -> high -> normal
        #
        # i.e. one isolated threshold exceedance.
        # -------------------------------------------------

        temporary_count = 0

        for i in range(
            len(triggers)
        ):

            if not triggers[i]:
                continue

            previous_high = (
                i > 0
                and triggers[i - 1]
            )

            next_high = (
                i < len(triggers) - 1
                and triggers[i + 1]
            )

            if (
                not previous_high
                and not next_high
            ):
                temporary_count += 1

        results[k] = {

            "threshold": threshold,

            "trigger_count":
                trigger_count,

            "trigger_percentage":
                trigger_percentage,

            "temporary_count":
                temporary_count
        }

    return {
        "samples": len(cpu_values),

        "calibration_samples":
            len(calibration_values),

        "runtime_samples":
            len(runtime_values),

        "median":
            median_cpu,

        "mad":
            mad_cpu,

        "results":
            results
    }


# ---------------------------------------------------------
# Analyse all runs
# ---------------------------------------------------------

all_runs = {}


for run_file in RUN_FILES:

    filepath = os.path.join(
        RESULTS_DIR,
        run_file
    )

    if not os.path.exists(filepath):

        print(
            f"[-] Missing file: {filepath}"
        )

        continue

    all_runs[run_file] = analyse_run(
        filepath
    )


# ---------------------------------------------------------
# Print calibration information
# ---------------------------------------------------------

print()
print("=" * 75)
print("CALIBRATION RESULTS")
print("=" * 75)


for run_name, data in all_runs.items():

    print()

    print(
        f"{run_name}"
    )

    print(
        f"  Total samples: "
        f"{data['samples']}"
    )

    print(
        f"  Calibration samples: "
        f"{data['calibration_samples']}"
    )

    print(
        f"  Runtime samples: "
        f"{data['runtime_samples']}"
    )

    print(
        f"  Median CPU: "
        f"{data['median']:.2f}%"
    )

    print(
        f"  MAD: "
        f"{data['mad']:.2f}%"
    )


# ---------------------------------------------------------
# Trigger frequency table
# ---------------------------------------------------------

print()
print("=" * 75)
print("TRIGGER FREQUENCY (%)")
print("=" * 75)

print(
    f"{'k':<8}"
    f"{'Run 1':<12}"
    f"{'Run 2':<12}"
    f"{'Run 3':<12}"
    f"{'Average':<12}"
)

print("-" * 56)


for k in K_VALUES:

    percentages = []

    row = f"{k:<8.1f}"

    for run_file in RUN_FILES:

        if run_file in all_runs:

            value = (
                all_runs[run_file]
                ["results"][k]
                ["trigger_percentage"]
            )

            percentages.append(
                value
            )

            row += f"{value:<12.2f}"

        else:

            row += f"{'N/A':<12}"

    if percentages:

        average = statistics.mean(
            percentages
        )

        row += f"{average:<12.2f}"

    else:

        row += f"{'N/A':<12}"

    print(row)


# ---------------------------------------------------------
# Temporary trigger table
# ---------------------------------------------------------

print()
print("=" * 75)
print("TEMPORARY / ISOLATED TRIGGERS")
print("=" * 75)

print(
    f"{'k':<8}"
    f"{'Run 1':<12}"
    f"{'Run 2':<12}"
    f"{'Run 3':<12}"
    f"{'Average':<12}"
)

print("-" * 56)


for k in K_VALUES:

    temporary_values = []

    row = f"{k:<8.1f}"

    for run_file in RUN_FILES:

        if run_file in all_runs:

            value = (
                all_runs[run_file]
                ["results"][k]
                ["temporary_count"]
            )

            temporary_values.append(
                value
            )

            row += f"{value:<12}"

        else:

            row += f"{'N/A':<12}"

    if temporary_values:

        average = statistics.mean(
            temporary_values
        )

        row += f"{average:<12.2f}"

    else:

        row += f"{'N/A':<12}"

    print(row)


# ---------------------------------------------------------
# Threshold table
# ---------------------------------------------------------

print()
print("=" * 75)
print("CPU THRESHOLDS (%)")
print("=" * 75)

print(
    f"{'k':<8}"
    f"{'Run 1':<12}"
    f"{'Run 2':<12}"
    f"{'Run 3':<12}"
    f"{'Average':<12}"
)

print("-" * 56)


for k in K_VALUES:

    thresholds = []

    row = f"{k:<8.1f}"

    for run_file in RUN_FILES:

        if run_file in all_runs:

            value = (
                all_runs[run_file]
                ["results"][k]
                ["threshold"]
            )

            thresholds.append(
                value
            )

            row += f"{value:<12.2f}"

        else:

            row += f"{'N/A':<12}"

    if thresholds:

        average = statistics.mean(
            thresholds
        )

        row += f"{average:<12.2f}"

    else:

        row += f"{'N/A':<12}"

    print(row)