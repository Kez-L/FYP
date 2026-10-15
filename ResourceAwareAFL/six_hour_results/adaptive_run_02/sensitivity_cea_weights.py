import csv

CSV_FILE = "experiment_metrics.csv"

ALPHA = 0.5
BETA = 0.5
EPSILON = 1e-6
CES_THRESHOLD = 0.001
CALIBRATION_WINDOWS = 10

# Candidate CPU weights
LAMBDA_U_VALUES = [
    0.001,
    0.005,
    0.01,     # current
    0.02,
    0.05,
]

# Candidate memory weights
LAMBDA_M_VALUES = [
    0.0001,
    0.0005,
    0.001,    # current
    0.002,
    0.005,
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


def calculate_windows(rows, lambda_u, lambda_m):
    results = []

    for i in range(1, len(rows)):
        previous = rows[i - 1]
        current = rows[i]

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
            + lambda_u * current["cpu_percent"]
            + lambda_m * current["memory_mb"]
            + EPSILON
        )

        ce = delta_cov / resource_cost
        cre = delta_crashes / resource_cost

        ces = (
            ALPHA * ce
            + BETA * cre
        )

        progress = (
            delta_cov > 0
            or delta_crashes > 0
        )

        healthy = ces >= CES_THRESHOLD

        results.append({
            "window_id": current["window_id"],
            "ces": ces,
            "progress": progress,
            "healthy": healthy,
        })

    return results


def summarise(results):
    progress_total = 0
    progress_healthy = 0
    progress_low = 0

    no_progress_total = 0
    no_progress_healthy = 0

    progress_ces = []

    for row in results:

        if row["progress"]:
            progress_total += 1
            progress_ces.append(row["ces"])

            if row["healthy"]:
                progress_healthy += 1
            else:
                progress_low += 1

        else:
            no_progress_total += 1

            if row["healthy"]:
                no_progress_healthy += 1

    if progress_ces:
        minimum_progress_ces = min(progress_ces)
        average_progress_ces = (
            sum(progress_ces)
            / len(progress_ces)
        )
    else:
        minimum_progress_ces = 0
        average_progress_ces = 0

    return {
        "progress_total": progress_total,
        "progress_healthy": progress_healthy,
        "progress_low": progress_low,
        "no_progress_total": no_progress_total,
        "no_progress_healthy": no_progress_healthy,
        "min_progress_ces": minimum_progress_ces,
        "avg_progress_ces": average_progress_ces,
    }


def main():
    rows = load_data()

    print()
    print("CEA RESOURCE-WEIGHT SENSITIVITY")
    print("===============================")

    print(
        f"CES threshold = {CES_THRESHOLD}"
    )

    print()

    for lambda_u in LAMBDA_U_VALUES:

        for lambda_m in LAMBDA_M_VALUES:

            results = calculate_windows(
                rows,
                lambda_u,
                lambda_m
            )

            summary = summarise(results)

            marker = ""

            if (
                lambda_u == 0.01
                and lambda_m == 0.001
            ):
                marker = " <-- CURRENT"

            print(
                f"lambda_u={lambda_u:<6} "
                f"lambda_m={lambda_m:<7}"
                f"{marker}"
            )

            print(
                f"  Progress healthy: "
                f"{summary['progress_healthy']}/"
                f"{summary['progress_total']}"
            )

            print(
                f"  Progress misclassified LOW: "
                f"{summary['progress_low']}"
            )

            print(
                f"  No-progress classified HEALTHY: "
                f"{summary['no_progress_healthy']}/"
                f"{summary['no_progress_total']}"
            )

            print(
                f"  Minimum progress CES: "
                f"{summary['min_progress_ces']:.6f}"
            )

            print(
                f"  Average progress CES: "
                f"{summary['avg_progress_ces']:.6f}"
            )

            print()


if __name__ == "__main__":
    main()
