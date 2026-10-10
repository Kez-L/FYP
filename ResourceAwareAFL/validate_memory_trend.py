TREND_WINDOWS = [2, 3, 5, 10]
THRESHOLDS_MB = [0.25, 0.5, 1.0]

# Start from a realistic stable memory level
BASELINE = 42.8

SCENARIOS = {
    # Normal tiny fluctuations
    "stable": [
        42.80, 42.81, 42.80, 42.82, 42.81,
        42.82, 42.81, 42.83, 42.82, 42.83,
        42.82, 42.83, 42.82, 42.84, 42.83,
    ],

    # One temporary jump, then recovery
    "temporary_spike": [
        42.80, 42.81, 42.80, 42.81, 42.80,
        43.40,
        42.82, 42.81, 42.82, 42.83,
        42.82, 42.83, 42.82, 42.83, 42.84,
    ],

    # Slow persistent growth: +0.10 MB per window
    "slow_growth": [
        BASELINE + (0.10 * i)
        for i in range(15)
    ],

    # Faster persistent growth: +0.25 MB per window
    "fast_growth": [
        BASELINE + (0.25 * i)
        for i in range(15)
    ],

    # Memory jumps upward and stays high
    "step_and_hold": [
        42.80, 42.81, 42.80, 42.81, 42.80,
        44.00, 44.01, 44.00, 44.01, 44.00,
        44.01, 44.00, 44.01, 44.00, 44.01,
    ],
}


def analyse(values, trend_window, threshold):

    results = []

    for i in range(trend_window, len(values)):

        growth = (
            values[i]
            - values[i - trend_window]
        )

        results.append(growth >= threshold)

    trigger_count = sum(results)

    first_trigger = None

    for i, triggered in enumerate(results):

        if triggered:
            # Convert back to human-readable window number
            first_trigger = i + trend_window + 1
            break

    return trigger_count, first_trigger


# ---------------------------------------------------------
# Original sensitivity analysis
# ---------------------------------------------------------

for scenario_name, values in SCENARIOS.items():

    print("\n======================================")
    print(scenario_name)
    print("======================================")

    for trend_window in TREND_WINDOWS:

        for threshold in THRESHOLDS_MB:

            count, first = analyse(
                values,
                trend_window,
                threshold
            )

            print(
                f"W={trend_window:<2} | "
                f"T={threshold:.2f} MB | "
                f"triggers={count:<2} | "
                f"first={first}"
            )


# ---------------------------------------------------------
# Test candidate with persistence
# Candidate:
# Trend window W = 5
# Growth threshold = 0.50 MB
# Persistence K = 3
# ---------------------------------------------------------

def analyse_with_persistence(
    values,
    trend_window,
    threshold,
    persistence=3
):

    consecutive = 0
    first_persistent = None

    for i in range(trend_window, len(values)):

        growth = (
            values[i]
            - values[i - trend_window]
        )

        abnormal = growth >= threshold

        if abnormal:
            consecutive += 1
        else:
            consecutive = 0

        if (
            consecutive >= persistence
            and first_persistent is None
        ):
            # Human-readable window number
            first_persistent = i + 1

    return first_persistent


print("\n\n======================================")
print("CANDIDATE: W=5, T=0.50 MB, K=3")
print("======================================")

for scenario_name, values in SCENARIOS.items():

    first = analyse_with_persistence(
        values,
        trend_window=5,
        threshold=0.50,
        persistence=3
    )

    print(
        f"{scenario_name:<20} "
        f"persistent_trigger={first}"
    )