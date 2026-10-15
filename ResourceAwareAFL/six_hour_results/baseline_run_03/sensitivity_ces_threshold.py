import csv

CSV_FILE = "experiment_metrics.csv"

# Same parameters currently used by cea.py
ALPHA = 0.5
BETA = 0.5
LAMBDA_U = 0.01
LAMBDA_M = 0.001
EPSILON = 1e-6

# Do not evaluate calibration windows
CALIBRATION_WINDOWS = 10

THRESHOLDS = [
    0.0001,
    0.0005,
    0.0010,
    0.0020,
    0.0050,
    0.0100,
]


def load_data():
    rows = []

    with open(CSV_FILE, newline="") as file:
        reader = csv.DictReader(file)

        for row in reader:
            rows.append({
                "window_id": int(row["window_id"]),
                "elapsed_sec": float(row["elapsed_sec"]),
                "coverage_pct": float(row["coverage_pct"]),
                "cpu_percent": float(row["cpu_percent"]),
                "memory_mb": float(row["memory_mb"]),
                "saved_crashes": int(row["saved_crashes"]),
            })

    return rows


def calculate_windows(rows):
    results = []

    for i in range(1, len(rows)):
        previous = rows[i - 1]
        current = rows[i]

        # Skip calibration period.
        # First evaluated runtime window is Window 11.
        if current["window_id"] <= CALIBRATION_WINDOWS:
            continue

        delta_time = (
            current["elapsed_sec"]
            - previous["elapsed_sec"]
        )

        delta_cov = max(
            0.0,
            current["coverage_pct"]
            - previous["coverage_pct"]
        )

        delta_crashes = max(
            0,
            current["saved_crashes"]
            - previous["saved_crashes"]
        )

        resource_cost = (
            delta_time
            + LAMBDA_U * current["cpu_percent"]
            + LAMBDA_M * current["memory_mb"]
            + EPSILON
        )

        ce = delta_cov / resource_cost
        cre = delta_crashes / resource_cost

        ces = (
            ALPHA * ce
            + BETA * cre
        )

        actual_progress = (
            delta_cov > 0
            or delta_crashes > 0
        )

        results.append({
            "window_id": current["window_id"],
            "delta_cov": delta_cov,
            "delta_crashes": delta_crashes,
            "ces": ces,
            "actual_progress": actual_progress,
        })

    return results


def analyse_threshold(results, threshold):
    healthy = 0
    low = 0

    progress_healthy = 0
    progress_low = 0

    no_progress_healthy = 0
    no_progress_low = 0

    for row in results:

        predicted_healthy = (
            row["ces"] >= threshold
        )

        actual_progress = row["actual_progress"]

        if predicted_healthy:
            healthy += 1
        else:
            low += 1

        if actual_progress and predicted_healthy:
            progress_healthy += 1

        elif actual_progress and not predicted_healthy:
            progress_low += 1

        elif not actual_progress and predicted_healthy:
            no_progress_healthy += 1

        else:
            no_progress_low += 1

    return {
        "healthy": healthy,
        "low": low,
        "progress_healthy": progress_healthy,
        "progress_low": progress_low,
        "no_progress_healthy": no_progress_healthy,
        "no_progress_low": no_progress_low,
    }


def main():

    rows = load_data()
    results = calculate_windows(rows)

    print()
    print("CES THRESHOLD SENSITIVITY")
    print("=========================")

    print(
        f"Runtime windows analysed: "
        f"{len(results)}"
    )

    progress_windows = sum(
        1
        for row in results
        if row["actual_progress"]
    )

    print(
        f"Actual progress windows: "
        f"{progress_windows}"
    )

    print(
        f"No-progress windows: "
        f"{len(results) - progress_windows}"
    )

    print()

    for threshold in THRESHOLDS:

        result = analyse_threshold(
            results,
            threshold
        )

        print(
            f"Threshold = {threshold:.4f}"
        )

        print(
            f"  HEALTHY: {result['healthy']}"
        )

        print(
            f"  LOW_EFFICIENCY: {result['low']}"
        )

        print(
            "  Progress -> HEALTHY: "
            f"{result['progress_healthy']}"
        )

        print(
            "  Progress -> LOW: "
            f"{result['progress_low']}"
        )

        print(
            "  No progress -> HEALTHY: "
            f"{result['no_progress_healthy']}"
        )

        print(
            "  No progress -> LOW: "
            f"{result['no_progress_low']}"
        )

        print()

    print("Individual progress windows")
    print("---------------------------")

    for row in results:

        if row["actual_progress"]:

            print(
                f"Window {row['window_id']:03d} | "
                f"ΔCov={row['delta_cov']:.4f} | "
                f"ΔCrash={row['delta_crashes']} | "
                f"CES={row['ces']:.6f}"
            )


if __name__ == "__main__":
    main()
