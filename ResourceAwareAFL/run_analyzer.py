import time
import json
import os
import statistics

from cea import CostEffectivenessAnalyzer
from controller import AdaptiveFuzzingController


class AdaptiveResourceAnalyzer:

    def __init__(
        self,
        calibration_windows=10,
        cpu_std_multiplier=2.0,
        memory_std_multiplier=2.0
    ):

        self.calibration_windows = (
            calibration_windows
        )

        self.cpu_std_multiplier = (
            cpu_std_multiplier
        )

        self.memory_std_multiplier = (
            memory_std_multiplier
        )

        self.cpu_history = []
        self.memory_history = []

        self.cpu_threshold = None
        self.memory_growth_threshold = None

    # ---------------------------------------------------------
    # Calibration
    # ---------------------------------------------------------

    def update_baseline(
        self,
        cpu,
        memory
    ):

        if len(self.cpu_history) < self.calibration_windows:

            self.cpu_history.append(cpu)
            self.memory_history.append(memory)

        # -----------------------------------------------------
        # Calculate thresholds once enough samples exist
        # -----------------------------------------------------

        if (
            len(self.cpu_history)
            >= self.calibration_windows
        ):

            cpu_mean = statistics.mean(
                self.cpu_history
            )

            cpu_std = statistics.stdev(
                self.cpu_history
            ) if len(self.cpu_history) > 1 else 0.0

            self.cpu_threshold = (

                cpu_mean

                +

                self.cpu_std_multiplier
                * cpu_std
            )

            # -----------------------------------------------
            # Memory growth
            # -----------------------------------------------

            memory_deltas = []

            for i in range(
                1,
                len(self.memory_history)
            ):

                delta = (
                    self.memory_history[i]
                    -
                    self.memory_history[i - 1]
                )

                memory_deltas.append(
                    delta
                )

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

                self.memory_growth_threshold = (

                    memory_mean

                    +

                    self.memory_std_multiplier
                    * memory_std
                )

                # Never allow a negative threshold
                self.memory_growth_threshold = max(
                    self.memory_growth_threshold,
                    0.0
                )

    # ---------------------------------------------------------
    # Determine resource condition
    # ---------------------------------------------------------

    def determine_resource_condition(
        self,
        cpu,
        memory
    ):

        # -----------------------------------------------------
        # Still calibrating
        # -----------------------------------------------------

        if self.cpu_threshold is None:

            return None

        cpu_spike = (
            cpu >= self.cpu_threshold
        )

        # -----------------------------------------------------
        # Memory growth
        # -----------------------------------------------------

        memory_spike = False

        if (
            self.memory_growth_threshold
            is not None
            and len(self.memory_history) >= 2
        ):

            current_memory_growth = (

                self.memory_history[-1]
                -
                self.memory_history[-2]
            )

            memory_spike = (

                current_memory_growth
                >=
                self.memory_growth_threshold
            )

        # -----------------------------------------------------
        # Determine condition
        # -----------------------------------------------------

        if cpu_spike and memory_spike:

            return "CPU_MEMORY_SPIKE"

        if cpu_spike:

            return "CPU_SPIKE"

        if memory_spike:

            return "MEMORY_SPIKE"

        return None


# =============================================================
# Main analyzer
# =============================================================

def run_stream_analyzer(
    log_file="raw_metrics.json"
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

        calibration_windows=10,

        cpu_std_multiplier=2.0,

        memory_std_multiplier=2.0
    )

    # ---------------------------------------------------------
    # Controller
    # ---------------------------------------------------------

    controller = AdaptiveFuzzingController(
        fuzzer_out_dir="out"
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
        "mean + 2*std"
    )

    print(
        "[+] Memory threshold: "
        "abnormal growth based on baseline"
    )

    print(
        "[+] Calibration windows: 10"
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
                        f"{calibration_count}/10 "
                        f"windows | "
                        f"CPU={cpu}% | "
                        f"Memory={memory} MB"
                    )

                    continue

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
                ):

                    reason = "COVERAGE_SLOWDOWN"

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

    run_stream_analyzer()
