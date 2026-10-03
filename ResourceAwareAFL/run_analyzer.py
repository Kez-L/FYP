import time
import json
import os
import statistics
import argparse

from cea import CostEffectivenessAnalyzer
from controller import AdaptiveFuzzingController


class AdaptiveResourceAnalyzer:

    def __init__(
        self,
        calibration_windows=10,
        cpu_mad_multiplier=2.0,
        memory_std_multiplier=2.0,
        persistence_windows=3,
        min_memory_growth_mb=1.0
    ):

        self.calibration_windows = calibration_windows

        self.cpu_mad_multiplier = cpu_mad_multiplier

        self.memory_std_multiplier = memory_std_multiplier

        self.persistence_windows = persistence_windows
        self.min_memory_growth_mb = min_memory_growth_mb

        self.cpu_history = []
        self.memory_history = []

        self.cpu_threshold = None
        self.memory_growth_threshold = None
        self.previous_memory = None

        # Persistence counters
        self.cpu_abnormal_count = 0
        self.memory_abnormal_count = 0
        self.coverage_stagnation_count = 0
        self.coverage_adaptation_triggered = False

        self.last_adaptation_record = None
        self.adaptation_wait_windows = 1

    # ---------------------------------------------------------
    # Calibration
    # ---------------------------------------------------------

    def update_baseline(
        self,
        cpu,
        memory
    ):

        # Only collect calibration samples initially
        if len(self.cpu_history) < self.calibration_windows:

            self.cpu_history.append(cpu)
            self.memory_history.append(memory)

        # -----------------------------------------------------
        # Calculate thresholds once calibration is complete
        # -----------------------------------------------------

        if (
            len(self.cpu_history)
            >= self.calibration_windows
        ):

            cpu_median = statistics.median(
                self.cpu_history
            )

            cpu_deviations = [
                abs(cpu - cpu_median)
                for cpu in self.cpu_history
            ]

            cpu_mad = statistics.median(
                cpu_deviations
            )

            self.cpu_threshold = (
                cpu_median
                +
                self.cpu_mad_multiplier * cpu_mad
            )

            # -------------------------------------------------
            # Memory growth baseline
            # -------------------------------------------------

            memory_deltas = []

            for i in range(
                1,
                len(self.memory_history)
            ):

                delta = (
                    self.memory_history[i] - self.memory_history[i - 1]
                )

                memory_deltas.append(delta)

            if memory_deltas:

                memory_mean = statistics.mean(
                    memory_deltas
                )

                memory_std = (
                    statistics.stdev(
                        memory_deltas
                    )
                    if len(memory_deltas) > 1
                    else 0.0
                )

                calculated_threshold = memory_mean + (
                    self.memory_std_multiplier * memory_std
                )

                self.memory_growth_threshold = max(
                    calculated_threshold,
                    self.min_memory_growth_mb
                )
            
            if self.previous_memory is None:
                self.previous_memory = self.memory_history[-1]

    # ---------------------------------------------------------
    # Determine resource condition
    # ---------------------------------------------------------

    def determine_resource_condition(
        self,
        cpu,
        memory
    ):

        # Still calibrating
        if self.cpu_threshold is None:

            return None

        # -----------------------------------------------------
        # CPU abnormality
        # -----------------------------------------------------

        cpu_spike = (
            cpu >= self.cpu_threshold
        )

        if cpu_spike:

            self.cpu_abnormal_count += 1

        else:

            self.cpu_abnormal_count = 0

        # -----------------------------------------------------
        # Memory abnormality
        # -----------------------------------------------------

        memory_spike = False

        if (
            self.memory_growth_threshold is not None
            and self.previous_memory is not None
        ):

            current_memory_growth = (
                memory - self.previous_memory
            )

            memory_spike = (
                current_memory_growth > 0
                and current_memory_growth >= self.memory_growth_threshold
            )

        # Update previous memory for the next window
        self.previous_memory = memory

        if memory_spike:

            self.memory_abnormal_count += 1

        else:

            self.memory_abnormal_count = 0

        # -----------------------------------------------------
        # Persistence
        # -----------------------------------------------------

        cpu_persistent = (
            self.cpu_abnormal_count
            >= self.persistence_windows
        )

        memory_persistent = (
            self.memory_abnormal_count
            >= self.persistence_windows
        )

        # -----------------------------------------------------
        # Determine persistent condition
        # -----------------------------------------------------

        if cpu_persistent and memory_persistent:

            return "CPU_MEMORY_SPIKE"

        if cpu_persistent:

            return "CPU_SPIKE"

        if memory_persistent:

            return "MEMORY_SPIKE"

        return None

# =============================================================
# Main analyzer
# =============================================================

def run_stream_analyzer(
    log_file="raw_metrics.json",
    calibration_windows=10,
    persistence_windows=3
):

    # ---------------------------------------------------------
    # CEA
    # ---------------------------------------------------------

    analyzer = CostEffectivenessAnalyzer(
        alpha=0.5,
        beta=0.5,
        lambda_u=0.01,
        lambda_m=0.001,
        threshold_ces=0.001
    )

    # ---------------------------------------------------------
    # Resource analyzer
    # ---------------------------------------------------------

    resource_analyzer = AdaptiveResourceAnalyzer(
        calibration_windows=calibration_windows,
        cpu_mad_multiplier=0.5,
        memory_std_multiplier=2.0,
        persistence_windows=persistence_windows
    )

    # ---------------------------------------------------------
    # Controller
    # ---------------------------------------------------------

    controller = AdaptiveFuzzingController(
        fuzzer_out_dir="out",
        persistence_windows=persistence_windows,
        escalation_threshold=3
    )

    print(
        "[+] Starting adaptive analyzer..."
    )

    print(
        f"[+] Reading: "
        f"{os.path.abspath(log_file)}"
    )

    print(
        "[+] CPU threshold: "
        "median + 0.5*MAD"
    )

    print(
        "[+] Memory threshold: "
        "abnormal growth based on baseline"
    )

    print(
        f"[+] Calibration windows: "
        f"{resource_analyzer.calibration_windows}"
    )

    print(
        f"[+] Persistence requirement: "
        f"{resource_analyzer.persistence_windows} "
        f"consecutive windows"
    )

    # ---------------------------------------------------------
    # Wait for monitor
    # ---------------------------------------------------------

    while not os.path.exists(log_file):

        print(
            f"[*] Waiting for "
            f"{log_file}..."
        )

        time.sleep(2)

    # ---------------------------------------------------------
    # Read metrics
    # ---------------------------------------------------------

    with open(
        log_file,
        "r"
    ) as file:

        while True:

            line = file.readline()

            if not line:

                time.sleep(1)

                continue

            try:

                record = json.loads(
                    line.strip()
                )

                # -------------------------------------------------
                # Calculate CEA
                # -------------------------------------------------

                result = analyzer.compute_ces(
                    record
                )

                window_id = result[
                    "window_id"
                ]

                delta_cov = result[
                    "delta_cov"
                ]

                delta_crashes = result[
                    "delta_crashes"
                ]

                cpu = result[
                    "cpu_percent"
                ]

                memory = result[
                    "memory_mb"
                ]

                ces = result[
                    "CES"
                ]

                status = result[
                    "status"
                ]

                # -------------------------------------------------
                # Update resource baseline
                # -------------------------------------------------

                resource_analyzer.update_baseline(
                    cpu,
                    memory
                )

                # -------------------------------------------------
                # Calibration message
                # -------------------------------------------------

                calibration_count = len(
                    resource_analyzer.cpu_history
                )

                if (
                    resource_analyzer.cpu_threshold
                    is None
                ):

                    print(
                        f"[Calibration] "
                        f"{calibration_count}/"
                        f"{resource_analyzer.calibration_windows} "
                        f"windows | "
                        f"CPU={cpu}% | "
                        f"Memory={memory} MB"
                    )

                    continue

                # -------------------------------------------------
                # Evaluate previous adaptation
                # -------------------------------------------------

                if resource_analyzer.last_adaptation_record is not None:

                    before_record = resource_analyzer.last_adaptation_record

                    evaluation = analyzer.evaluate_intervention(
                        before_record,
                        result
                    )

                    print(
                        f"[Intervention Evaluation] "
                        f"Previous Window: "
                        f"{before_record['window_id']} | "
                        f"Current Window: "
                        f"{record['window_id']} | "
                        f"CES: "
                        f"{evaluation['before_ces']:.6f} -> "
                        f"{evaluation['after_ces']:.6f} | "
                        f"Coverage: "
                        f"{evaluation['before_coverage_gain']:.2f} -> "
                        f"{evaluation['after_coverage_gain']:.2f} | "
                        f"Improved: "
                        f"{evaluation['improved']}"
                    )

                    controller.evaluate_adaptation(
                        evaluation["improved"]
                    )

                    resource_analyzer.last_adaptation_record = None

                # -------------------------------------------------
                # Determine resource condition
                # -------------------------------------------------

                resource_condition = (

                    resource_analyzer
                    .determine_resource_condition(
                        cpu,
                        memory
                    )
                )

                # -------------------------------------------------
                # Print thresholds
                # -------------------------------------------------

                print(
                    f"[Window {window_id:03d}] "
                    f"ΔCov: {delta_cov:<7} | "
                    f"ΔCrashes: {delta_crashes:<2} | "
                    f"CPU: {cpu:<7}% | "
                    f"Memory: {memory:<8} MB | "
                    f"CES: {ces:<8.6f} | "
                    f"Status: [{status}]"
                )

                print(
                    f"    [Thresholds] "
                    f"CPU >= "
                    f"{resource_analyzer.cpu_threshold:.2f}% | "
                    f"Memory growth >= "
                    f"{resource_analyzer.memory_growth_threshold:.2f} MB"
                )

                if status == "LOW_EFFICIENCY" and delta_cov <= 0:
                    resource_analyzer.coverage_stagnation_count += 1
                else:
                    resource_analyzer.coverage_stagnation_count = 0
                    resource_analyzer.coverage_adaptation_triggered = False

                # -------------------------------------------------
                # Crash has highest priority
                # -------------------------------------------------

                if delta_crashes > 0:

                    reason = "CRASH_FOUND"

                # -------------------------------------------------
                # Resource adaptation
                # -------------------------------------------------

                elif resource_condition is not None:

                    reason = resource_condition

                # -------------------------------------------------
                # Coverage slowdown
                # -------------------------------------------------

                elif (
                    status == "LOW_EFFICIENCY"
                    and delta_cov <= 0
                    and resource_analyzer.coverage_stagnation_count >= 3
                    and not resource_analyzer.coverage_adaptation_triggered
                ):
                    reason = "COVERAGE_SLOWDOWN"
                    resource_analyzer.coverage_adaptation_triggered = True

                else:

                    reason = None

                # -------------------------------------------------
                # Apply adaptation
                # -------------------------------------------------

                if reason is not None:

                    print(
                        f"[Feedback] "
                        f"Trigger = {reason}"
                    )

                    controller.apply_level1_adaptation(

                        window_id,
                        ces,
                        reason
                    )

                    resource_analyzer.last_adaptation_record = record

                else:

                    print(
                        "[Adaptive Controller] "
                        "No adaptation required."
                    )

                    controller.reset_counter()

            except json.JSONDecodeError:

                continue

            except Exception as error:

                print(
                    f"[-] Analyzer error: "
                    f"{error}"
                )

                time.sleep(1)


# =============================================================
# Main
# =============================================================

if __name__ == "__main__":
    import argparse 

    parser = argparse.ArgumentParser(
        description="Adaptive Resource Analyzer"
    )

    parser.add_argument(
        "--calibration",
        type=int,
        default=10
    )

    parser.add_argument(
        "--persistence",
        type=int,
        default=3
    )

    args = parser.parse_args()

    run_stream_analyzer(
        calibration_windows=args.calibration,
        persistence_windows=args.persistence
    )
