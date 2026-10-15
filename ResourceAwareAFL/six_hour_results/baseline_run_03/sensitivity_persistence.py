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

# Selected from Test 1
CPU_MAD_K = 1.5

# Persistence values requested for sensitivity analysis
PERSISTENCE_VALUES = [
    1,
    2,
    3,
    5
]


# ---------------------------------------------------------
# Read CPU values
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
# Count persistent trigger events
# ---------------------------------------------------------

def count_persistent_events(triggers, persistence_k):

    events = 0
    consecutive = 0

    # Records the window where each persistent event
    # first becomes active
    trigger_windows = []

    event_active = False

    for index, is_high in enumerate(triggers):

        if is_high:

            consecutive += 1

            # Trigger once when the sequence reaches K
            if (
                consecutive >= persistence_k
                and not event_active
            ):

                events += 1
                event_active = True

                trigger_windows.append(
                    index + 1
                )

        else:

            consecutive = 0
            event_active = False

    return events, trigger_windows


# ---------------------------------------------------------
# Analyse one run
# ---------------------------------------------------------

def analyse_run(filepath):

    cpu_values = read_cpu_values(filepath)[:85]

    # First 10 windows are calibration
    calibration_values = cpu_values[
        :CALIBRATION_WINDOWS
    ]

    # Remaining windows are runtime
    runtime_values = cpu_values[
        CALIBRATION_WINDOWS:
    ]

    # -----------------------------------------------------
    # Calculate median
    # -----------------------------------------------------

    median_cpu = statistics.median(
        calibration_values
    )

    # -----------------------------------------------------
    # Calculate MAD
    # -----------------------------------------------------

    absolute_deviations = [
        abs(cpu - median_cpu)
        for cpu in calibration_values
    ]

    mad_cpu = statistics.median(
        absolute_deviations
    )

    # -----------------------------------------------------
    # CPU threshold using k = 1.5
    # -----------------------------------------------------

    threshold = (
        median_cpu
        + CPU_MAD_K * mad_cpu
    )

    # -----------------------------------------------------
    # Determine which runtime windows exceed threshold
    # -----------------------------------------------------

    triggers = [
        cpu >= threshold
        for cpu in runtime_values
    ]

    total_high_windows = sum(triggers)

    results = {}

    # -----------------------------------------------------
    # Test each persistence K
    # -----------------------------------------------------

    for persistence_k in PERSISTENCE_VALUES:

        events, trigger_windows = (
            count_persistent_events(
                triggers,
                persistence_k
            )
        )

        results[persistence_k] = {
            "events": events,
            "trigger_windows": trigger_windows
        }

    return {
        "samples": len(cpu_values),
        "runtime_samples": len(runtime_values),
        "median": median_cpu,
        "mad": mad_cpu,
        "threshold": threshold,
        "high_windows": total_high_windows,
        "results": results
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
# Print threshold information
# ---------------------------------------------------------

print()
print("=" * 75)
print("PERSISTENCE SENSITIVITY ANALYSIS")
print("=" * 75)

print(
    f"CPU threshold multiplier k = {CPU_MAD_K}"
)

print(
    f"Persistence values K = {PERSISTENCE_VALUES}"
)

print()


for run_name, data in all_runs.items():

    print(run_name)

    print(
        f"  Median CPU: "
        f"{data['median']:.2f}%"
    )

    print(
        f"  MAD: "
        f"{data['mad']:.2f}%"
    )

    print(
        f"  CPU threshold: "
        f"{data['threshold']:.2f}%"
    )

    print(
        f"  Above-threshold runtime windows: "
        f"{data['high_windows']} / "
        f"{data['runtime_samples']}"
    )

    print()


# ---------------------------------------------------------
# Persistent event table
# ---------------------------------------------------------

print("=" * 75)
print("PERSISTENT TRIGGER EVENTS")
print("=" * 75)

print(
    f"{'K':<8}"
    f"{'Run 1':<12}"
    f"{'Run 2':<12}"
    f"{'Run 3':<12}"
    f"{'Average':<12}"
)

print("-" * 56)


for persistence_k in PERSISTENCE_VALUES:

    event_values = []

    row = f"{persistence_k:<8}"

    for run_file in RUN_FILES:

        if run_file in all_runs:

            events = (
                all_runs[run_file]
                ["results"]
                [persistence_k]
                ["events"]
            )

            event_values.append(events)

            row += f"{events:<12}"

        else:

            row += f"{'N/A':<12}"

    if event_values:

        average = statistics.mean(
            event_values
        )

        row += f"{average:<12.2f}"

    else:

        row += f"{'N/A':<12}"

    print(row)


# ---------------------------------------------------------
# Trigger window details
# ---------------------------------------------------------

print()
print("=" * 75)
print("TRIGGER WINDOW DETAILS")
print("=" * 75)


for run_name, data in all_runs.items():

    print()
    print(run_name)

    for persistence_k in PERSISTENCE_VALUES:

        windows = (
            data["results"]
            [persistence_k]
            ["trigger_windows"]
        )

        print(
            f"  K={persistence_k}: "
            f"{windows}"
        )
