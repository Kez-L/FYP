import time
import json
import os
import statistics
import argparse

from cea import CostEffectivenessAnalyzer
from controller import AdaptiveFuzzingController


# =============================================================
# Resource analyzer
# =============================================================

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

        self.memory_trend_windows = memory_trend_windows
        self.memory_growth_threshold_mb = (
            memory_growth_threshold_mb
        )

        self.persistence_windows = persistence_windows

        # Calibration histories
        self.cpu_history = []
        self.memory_history = []

        # CPU threshold
        self.cpu_threshold = None

        # Baseline CPU variability
        self.cpu_mad = None

        # Memory trend samples
        self.memory_recent = []

        # Resource persistence
        self.cpu_abnormal_count = 0
        self.memory_abnormal_count = 0

        # Coverage plateau state
        self.coverage_stagnation_count = 0
        self.coverage_adaptation_triggered = False

        # Recovery after failed Level-1 intervention
        self.productive_recovery_count = 0

        # Previous Level-1 intervention record
        self.last_adaptation_record = None

        # Number of monitoring windows used to observe recovery
        self.adaptation_wait_windows = 3
        self.adaptation_observation_count = 0
        self.adaptation_observations = []

    # ---------------------------------------------------------
    # Calibration
    # ---------------------------------------------------------

    def update_baseline(
        self,
        cpu,
        memory
    ):

        if (
            len(self.cpu_history)
            < self.calibration_windows
        ):

            self.cpu_history.append(cpu)
            self.memory_history.append(memory)

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

            self.cpu_mad = cpu_mad

            self.cpu_threshold = (
                cpu_median
                +
                self.cpu_mad_multiplier * cpu_mad
            )

    # ---------------------------------------------------------
    # Resource-condition detection
    # ---------------------------------------------------------

    def determine_resource_condition(
        self,
        cpu,
        memory
    ):

        if self.cpu_threshold is None:
            return None

        # -----------------------------------------------------
        # CPU
        # -----------------------------------------------------

        cpu_spike = (
            cpu >= self.cpu_threshold
        )

        if cpu_spike:
            self.cpu_abnormal_count += 1
        else:
            self.cpu_abnormal_count = 0

        # -----------------------------------------------------
        # Memory trend
        # -----------------------------------------------------

        self.memory_recent.append(memory)

        memory_spike = False

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
    # Cost-effectiveness analyzer
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
        persistence_windows=persistence_windows
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
            f"[*] Waiting for {log_file}..."
        )

        time.sleep(2)

    # ---------------------------------------------------------
    # Read monitor stream
    # ---------------------------------------------------------

    last_execs_done = None
    valid_sample_count = 0
    last_edges_found = None
    startup_sample_skipped = False

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

                raw_metrics = record.get(
                    "raw_metrics",
                    {}
                )

                current_edges_found = raw_metrics.get(
                    "edges_found",
                    0
                )

                current_execs_done = raw_metrics.get(
                    "execs_done"
                )

                # -------------------------------------------------
                # Ignore AFL++ startup sample
                # -------------------------------------------------

                if (
                    not startup_sample_skipped
                    and valid_sample_count == 0
                    and (
                        current_execs_done is None
                        or current_execs_done <= 10
                    )
                ):

                    print(
                        "[Analyzer] Skipping AFL++ startup sample "
                        f"(execs_done={current_execs_done})"
                    )

                    last_execs_done = current_execs_done
                    startup_sample_skipped = True

                    continue

                # -------------------------------------------------
                # Ignore stale AFL++ fuzzer_stats samples
                # -------------------------------------------------

                if (
                    last_execs_done is not None
                    and current_execs_done
                    == last_execs_done
                ):

                    print(
                        f"[Analyzer] Skipping stale AFL++ stats "
                        f"(execs_done={current_execs_done})"
                    )

                    continue

                last_execs_done = current_execs_done
                valid_sample_count += 1

                # -------------------------------------------------
                # Calculate new AFL++ edges
                # -------------------------------------------------

                if last_edges_found is None:

                    delta_new_edges = 0

                else:

                    delta_new_edges = max(
                        0,
                        current_edges_found
                        -
                        last_edges_found
                    )

                last_edges_found = current_edges_found

                # -------------------------------------------------
                # Cost-effectiveness calculation
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
                # Calibration
                # -------------------------------------------------

                resource_analyzer.update_baseline(
                    cpu,
                    memory
                )

                if (
                    valid_sample_count
                    <= resource_analyzer.calibration_windows
                ):

                    print(
                        f"[Calibration] "
                        f"{valid_sample_count}/"
                        f"{resource_analyzer.calibration_windows} "
                        f"windows | "
                        f"CPU={cpu}% | "
                        f"Memory={memory} MB"
                    )

                    continue

                # -------------------------------------------------
                # Observe and evaluate previous Level-1 intervention
                # -------------------------------------------------

                if (
                    resource_analyzer.last_adaptation_record
                    is not None
                ):

                    resource_analyzer.adaptation_observation_count += 1

                    resource_analyzer.adaptation_observations.append(
                        result
                    )

                    print(
                        f"[Recovery] Observation "
                        f"{resource_analyzer.adaptation_observation_count}/"
                        f"{resource_analyzer.adaptation_wait_windows}"
                    )

                    if (
                        resource_analyzer.adaptation_observation_count
                        >= resource_analyzer.adaptation_wait_windows
                    ):

                        before_record = (
                            resource_analyzer
                            .last_adaptation_record
                        )

                        evaluations = [

                            analyzer.evaluate_intervention(
                                before_record,
                                observation
                            )

                            for observation
                            in resource_analyzer.adaptation_observations
                        ]

                        trigger_reason = (
                            controller
                            .last_adaptation[
                                "trigger_reason"
                            ]
                        )

                        # -----------------------------------------
                        # Trigger-specific recovery evaluation
                        # -----------------------------------------

                        after_cpus = [

                            observation[
                                "cpu_percent"
                            ]

                            for observation
                            in resource_analyzer.adaptation_observations
                        ]

                        after_memories = [

                            observation[
                                "memory_mb"
                            ]

                            for observation
                            in resource_analyzer.adaptation_observations
                        ]

                        median_after_cpu = (
                            statistics.median(
                                after_cpus
                            )
                        )

                        before_cpu = (
                            before_record[
                                "cpu_percent"
                            ]
                        )

                        cpu_reduction = (
                            before_cpu
                            -
                            median_after_cpu
                        )

                        cpu_mad = (

                            resource_analyzer.cpu_mad

                            if resource_analyzer.cpu_mad
                            is not None

                            else 0.0
                        )

                        cpu_improved = (

                            cpu_reduction >= cpu_mad

                            and median_after_cpu
                            < resource_analyzer.cpu_threshold
                        )

                        # -----------------------------------------
                        # Memory recovery evaluation
                        # -----------------------------------------

                        observed_memory_intervals = max(
                            1,
                            len(after_memories) - 1
                        )

                        allowed_memory_growth = (

                            resource_analyzer
                            .memory_growth_threshold_mb

                            * observed_memory_intervals

                            / resource_analyzer
                            .memory_trend_windows
                        )

                        recovery_memory_growth = (

                            after_memories[-1]
                            -
                            after_memories[0]
                        )

                        memory_improved = (

                            recovery_memory_growth
                            < allowed_memory_growth
                        )

                        # -----------------------------------------
                        # CPU adaptation
                        # -----------------------------------------

                        if trigger_reason == "CPU_SPIKE":

                            improved = cpu_improved

                            print(
                                f"[CPU Evaluation] "
                                f"Before CPU: "
                                f"{before_cpu:.2f}% | "
                                f"Median After CPU: "
                                f"{median_after_cpu:.2f}% | "
                                f"Reduction: "
                                f"{cpu_reduction:.2f}% | "
                                f"Required >= MAD: "
                                f"{cpu_mad:.2f}% | "
                                f"Improved: {improved}"
                            )

                        # -----------------------------------------
                        # Memory adaptation
                        # -----------------------------------------

                        elif (
                            trigger_reason
                            == "MEMORY_SPIKE"
                        ):

                            improved = memory_improved

                            print(
                                f"[Memory Evaluation] "
                                f"Recovery Growth: "
                                f"{recovery_memory_growth:.2f} MB | "
                                f"Allowed: < "
                                f"{allowed_memory_growth:.2f} MB | "
                                f"Improved: {improved}"
                            )

                        # -----------------------------------------
                        # CPU + memory adaptation
                        # -----------------------------------------

                        elif (
                            trigger_reason
                            == "CPU_MEMORY_SPIKE"
                        ):

                            improved = (
                                cpu_improved
                                and memory_improved
                            )

                            print(
                                f"[CPU+Memory Evaluation] "
                                f"CPU Reduction: "
                                f"{cpu_reduction:.2f}% "
                                f"(MAD={cpu_mad:.2f}%) | "
                                f"Memory Growth: "
                                f"{recovery_memory_growth:.2f} MB "
                                f"(Allowed < "
                                f"{allowed_memory_growth:.2f} MB) | "
                                f"Improved: {improved}"
                            )

                        # -----------------------------------------
                        # Coverage / crash-oriented adaptations
                        # -----------------------------------------

                        else:

                            improved_count = sum(

                                evaluation[
                                    "improved"
                                ]

                                for evaluation
                                in evaluations
                            )

                            improved = (
                                improved_count >= 2
                            )

                        best_evaluation = max(

                            evaluations,

                            key=lambda evaluation:
                                evaluation[
                                    "after_ces"
                                ]
                        )

                        print(
                            f"[Intervention Evaluation] "
                            f"Previous Window: "
                            f"{before_record['window_id']} | "
                            f"Observed: "
                            f"{resource_analyzer.adaptation_observation_count} "
                            f"windows | "
                            f"Best CES: "
                            f"{best_evaluation['before_ces']:.6f} -> "
                            f"{best_evaluation['after_ces']:.6f} | "
                            f"Improved: {improved}"
                        )

                        controller.evaluate_adaptation(
                            improved
                        )

                        resource_analyzer.last_adaptation_record = None

                        resource_analyzer.adaptation_observation_count = 0

                        resource_analyzer.adaptation_observations = []

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
                # Print metrics
                # -------------------------------------------------

                print(
                    f"[Window {window_id:03d}] "
                    f"ΔCov: {delta_cov:<7} | "
                    f"NewEdges: {delta_new_edges:<5} | "
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
                # Coverage plateau tracking
                # -------------------------------------------------

                if (
                    status == "LOW_EFFICIENCY"
                    and delta_new_edges <= 0
                ):

                    resource_analyzer.coverage_stagnation_count += 1

                else:

                    resource_analyzer.coverage_stagnation_count = 0

                    resource_analyzer.coverage_adaptation_triggered = False

                coverage_stagnant = (

                    resource_analyzer.coverage_stagnation_count
                    >= resource_analyzer.persistence_windows
                )

                ces_low = (
                    status == "LOW_EFFICIENCY"
                )

                # -------------------------------------------------
                # Cancel pending Level-2 escalation if productive
                # progress has genuinely resumed
                # -------------------------------------------------

                if controller.recovery_failed:

                    if (
                        delta_new_edges > 0
                        or status == "HEALTHY"
                    ):

                        resource_analyzer.productive_recovery_count += 1

                    else:

                        resource_analyzer.productive_recovery_count = 0

                    if (
                        resource_analyzer.productive_recovery_count
                        >= resource_analyzer.persistence_windows
                    ):

                        print(
                            "[Recovery] Campaign resumed productive "
                            "progress for multiple valid windows. "
                            "Cancelling pending Level-2 escalation."
                        )

                        controller.recovery_failed = False

                        controller.agentafl_escalation_pending = False

                        resource_analyzer.coverage_stagnation_count = 0

                        resource_analyzer.coverage_adaptation_triggered = False

                        resource_analyzer.productive_recovery_count = 0

                        coverage_stagnant = False

                else:

                    resource_analyzer.productive_recovery_count = 0

                # -------------------------------------------------
                # Recovery failed -> check persistent plateau
                # -------------------------------------------------

                if (
                    controller.should_trigger_agentafl(
                        coverage_stagnant=coverage_stagnant,
                        ces_low=ces_low
                    )
                ):

                    print(
                        "[Recovery] "
                        "Level-1 adaptation did not recover "
                        "the campaign and the coverage "
                        "plateau persists."
                    )

                    controller.trigger_agentafl()

                # -------------------------------------------------
                # Select Level-1 intervention
                # -------------------------------------------------

                reason = None

                level1_available = (

                    not controller.awaiting_recovery

                    and not controller.recovery_failed

                    and not controller.agentafl_escalation_pending
                )

                if level1_available:

                    # ---------------------------------------------
                    # Crash has highest priority
                    # ---------------------------------------------

                    if delta_crashes > 0:

                        reason = "CRASH_FOUND"

                    # ---------------------------------------------
                    # Resource pressure + low effectiveness
                    # ---------------------------------------------

                    elif (
                        resource_condition is not None
                        and status == "LOW_EFFICIENCY"
                        and delta_new_edges <= 0
                    ):

                        reason = resource_condition

                    # ---------------------------------------------
                    # Productive resource pressure
                    # ---------------------------------------------

                    elif (
                        resource_condition is not None
                        and delta_new_edges > 0
                    ):

                        print(
                            "[Resource] Resource pressure detected, "
                            f"but {delta_new_edges} new edges were found. "
                            "Treating resource usage as productive."
                        )

                        reason = None

                    # ---------------------------------------------
                    # Coverage slowdown
                    # ---------------------------------------------

                    elif (
                        status == "LOW_EFFICIENCY"

                        and delta_new_edges <= 0

                        and
                        resource_analyzer.coverage_stagnation_count
                        >= resource_analyzer.persistence_windows

                        and not
                        resource_analyzer.coverage_adaptation_triggered
                    ):

                        reason = "COVERAGE_SLOWDOWN"

                        resource_analyzer.coverage_adaptation_triggered = True

                # -------------------------------------------------
                # Apply Level-1 adaptation
                # -------------------------------------------------

                if reason is not None:

                    print(
                        f"[Feedback] "
                        f"Trigger = {reason}"
                    )

                    mode = (

                        controller.apply_level1_adaptation(
                            window_id,
                            ces,
                            reason
                        )
                    )

                    if mode is not None:

                        resource_analyzer.last_adaptation_record = (
                            result
                        )

                # -------------------------------------------------
                # AgentAFL handoff state
                # -------------------------------------------------

                elif controller.agentafl_escalation_pending:

                    print(
                        "[Level 2] "
                        "AgentAFL handoff triggered. "
                        "AgentAFL not connected - "
                        "simulating completion."
                    )

                    controller.agentafl_completed()

                # -------------------------------------------------
                # Failed recovery but plateau not persistent yet
                # -------------------------------------------------

                elif controller.recovery_failed:

                    print(
                        "[Recovery] "
                        "Level-1 recovery failed. "
                        "Monitoring whether the plateau persists."
                    )

                # -------------------------------------------------
                # Waiting for intervention evaluation
                # -------------------------------------------------

                elif controller.awaiting_recovery:

                    print(
                        "[Recovery] "
                        "Observing campaign after "
                        "Level-1 adaptation."
                    )

                # -------------------------------------------------
                # Normal fuzzing
                # -------------------------------------------------

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