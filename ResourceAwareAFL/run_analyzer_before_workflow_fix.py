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
        cpu_mad_multiplier=1.5,
        memory_trend_windows=5,
        memory_growth_threshold_mb=0.5,
        persistence_windows=3
    ):

        self.calibration_windows = calibration_windows
        self.cpu_mad_multiplier = cpu_mad_multiplier

        # Memory trend configuration
        self.memory_trend_windows = memory_trend_windows
        self.memory_growth_threshold_mb = memory_growth_threshold_mb

        self.persistence_windows = persistence_windows

        # Calibration histories
        self.cpu_history = []
        self.memory_history = []

        # CPU threshold calculated during calibration
        self.cpu_threshold = None

        # Recent runtime memory samples used for trend detection
        self.memory_recent = []

        # Persistence counters
        self.cpu_abnormal_count = 0
        self.memory_abnormal_count = 0

        # Coverage stagnation state
        self.coverage_stagnation_count = 0
        self.coverage_adaptation_triggered = False

        # Adaptation feedback
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
        # Calculate CPU threshold once calibration is complete
        # -----------------------------------------------------

        if (
            len(self.cpu_history)
            >= self.calibration_windows
        ):

            cpu_median = statistics.median(
                self.cpu_history
            )

            cpu_deviations = [
                abs(cpu_value - cpu_median)
                for cpu_value in self.cpu_history
            ]

            cpu_mad = statistics.median(
                cpu_deviations
            )

            self.cpu_threshold = (
                cpu_median
                +
                self.cpu_mad_multiplier * cpu_mad
            )

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
        # Memory trend abnormality
        # -----------------------------------------------------

        # Store current runtime memory sample
        self.memory_recent.append(memory)

        memory_spike = False
        current_memory_growth = None

        # Need W previous windows plus the current sample
        if (
            len(self.memory_recent)
            > self.memory_trend_windows
        ):

            current_memory_growth = (
                self.memory_recent[-1]
                -
                self.memory_recent[
                    -(self.memory_trend_windows + 1)
                ]
            )

            memory_spike = (
                current_memory_growth
                >= self.memory_growth_threshold_mb
            )

        # Keep only the samples required for the trend
        max_memory_samples = (
            self.memory_trend_windows + 1
        )

        if (
            len(self.memory_recent)
            > max_memory_samples
        ):

            self.memory_recent = (
                self.memory_recent[
                    -max_memory_samples:
                ]
            )

        # -----------------------------------------------------
        # Memory persistence
        # -----------------------------------------------------

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
        cpu_mad_multiplier=1.5,
        memory_trend_windows=5,
        memory_growth_threshold_mb=0.5,
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
        f"[+] CPU threshold: median + "
        f"{resource_analyzer.cpu_mad_multiplier}*MAD"
    )

    print(
        f"[+] Memory detector: "
        f"{resource_analyzer.memory_growth_threshold_mb:.2f} MB "
        f"growth over "
        f"{resource_analyzer.memory_trend_windows} windows"
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

                # Treat all calibration samples as calibration-only windows.
                # Runtime detection begins on the following window.
                if window_id <= resource_analyzer.calibration_windows:

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

                if (
                    resource_analyzer.last_adaptation_record
                    is not None
                ):

                    before_record = (
                        resource_analyzer
                        .last_adaptation_record
                    )

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
                # Print current metrics and thresholds
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
                    f"{resource_analyzer.memory_growth_threshold_mb:.2f} MB "
                    f"over "
                    f"{resource_analyzer.memory_trend_windows} windows"
                )

                # -------------------------------------------------
                # Coverage stagnation
                # -------------------------------------------------

                if (
                    status == "LOW_EFFICIENCY"
                    and delta_cov <= 0
                ):

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
                #
                # Resource pressure alone is not sufficient.
                # The campaign must also be LOW_EFFICIENCY.
                # -------------------------------------------------

                elif (
                    resource_condition is not None
                    and status == "LOW_EFFICIENCY"
                ):

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

                if (
                    reason is not None
                    and not controller.agentafl_escalation_pending
                ):

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

                elif controller.agentafl_escalation_pending:

                    print(
                        "[Escalation] "
                        "AgentAFL handoff pending. "
                        "Further Level-1 adaptations are paused."
                    )

                    controller._write_signal("DEFAULT")

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