import os
import time


class AdaptiveFuzzingController:

    def __init__(
        self,
        fuzzer_out_dir="out",
        persistence_windows=3,
        escalation_threshold=3
    ):

        self.fuzzer_out_dir = fuzzer_out_dir

        self.signal_dir = os.path.join(
            os.path.dirname(
                os.path.abspath(__file__)
            ),
            "adaptation"
        )

        self.signal_file = os.path.join(
            self.signal_dir,
            ".adapt_signal"
        )

        # -----------------------------------------------------
        # Configurable parameters
        # -----------------------------------------------------

        self.persistence_windows = (
            persistence_windows
        )

        self.escalation_threshold = (
            escalation_threshold
        )

        # -----------------------------------------------------
        # Runtime state
        # -----------------------------------------------------

        self.cpu_abnormal_counter = 0

        self.memory_abnormal_counter = 0

        self.resource_abnormal_counter = 0

        self.unsuccessful_adaptation_counter = 0

        self.previous_unique_crashes = 0

        self.previous_coverage = None

        self.last_adaptation = None

        os.makedirs(
            self.signal_dir,
            exist_ok=True
        )

        self._write_signal(
            "DEFAULT"
        )

    # ---------------------------------------------------------
    # Write signal
    # ---------------------------------------------------------

    def _write_signal(self, mode):

        try:

            with open(
                self.signal_file,
                "w"
            ) as file:

                file.write(
                    f"MODE={mode};"
                    f"TIMESTAMP={time.time()}\n"
                )

            print(
                f"[Adaptive Controller] "
                f"Signal -> {mode}"
            )

        except OSError as error:

            print(
                f"[-] Failed to write signal: "
                f"{error}"
            )

    # ---------------------------------------------------------
    # Detect new unique crash
    # ---------------------------------------------------------

    def is_new_unique_crash(
        self,
        current_unique_crashes
    ):

        new_crash = (
            current_unique_crashes
            >
            self.previous_unique_crashes
        )

        self.previous_unique_crashes = (
            current_unique_crashes
        )

        return new_crash

    # ---------------------------------------------------------
    # Update resource persistence
    # ---------------------------------------------------------

    def update_resource_persistence(
        self,
        cpu_abnormal,
        memory_abnormal
    ):

        if cpu_abnormal:

            self.cpu_abnormal_counter += 1

        else:

            self.cpu_abnormal_counter = 0

        if memory_abnormal:

            self.memory_abnormal_counter += 1

        else:

            self.memory_abnormal_counter = 0

        resource_abnormal = (
            cpu_abnormal
            or
            memory_abnormal
        )

        if resource_abnormal:

            self.resource_abnormal_counter += 1

        else:

            self.resource_abnormal_counter = 0

        persistent = (
            self.resource_abnormal_counter
            >=
            self.persistence_windows
        )

        return persistent

    # ---------------------------------------------------------
    # Determine trigger
    # ---------------------------------------------------------

    def determine_trigger(
        self,
        unique_crashes,
        cpu_abnormal=False,
        memory_abnormal=False
    ):
        """
        Determine the highest-priority intervention.

        Priority:

        1. New unique crash
        2. Persistent CPU + memory abnormality
        3. Persistent CPU abnormality
        4. Persistent memory abnormality
        5. No intervention
        """

        # -----------------------------------------------------
        # Highest priority: NEW unique crash
        # -----------------------------------------------------

        new_crash = self.is_new_unique_crash(
            unique_crashes
        )

        if new_crash:

            return "CRASH_FOUND"

        # -----------------------------------------------------
        # Resource persistence
        # -----------------------------------------------------

        persistent = (
            self.update_resource_persistence(
                cpu_abnormal,
                memory_abnormal
            )
        )

        if not persistent:

            return None

        # -----------------------------------------------------
        # CPU + memory
        # -----------------------------------------------------

        if (
            cpu_abnormal
            and
            memory_abnormal
        ):

            return "CPU_MEMORY_SPIKE"

        # -----------------------------------------------------
        # CPU
        # -----------------------------------------------------

        if cpu_abnormal:

            return "CPU_SPIKE"

        # -----------------------------------------------------
        # Memory
        # -----------------------------------------------------

        if memory_abnormal:

            return "MEMORY_SPIKE"

        return None

    # ---------------------------------------------------------
    # Apply Level 1 adaptation
    # ---------------------------------------------------------

    def apply_level1_adaptation(
        self,
        window_id,
        ces_score,
        trigger_reason
    ):

        print(
            f"\n[Adaptive Controller] "
            f"Window {window_id}: "
            f"CES = {ces_score:.6f}"
        )

        print(
            f"[Adaptive Controller] "
            f"Trigger = {trigger_reason}"
        )

        # -----------------------------------------------------
        # CPU
        # -----------------------------------------------------

        if trigger_reason == "CPU_SPIKE":

            mode = "THROTTLE_POWER"

            print(
                "[Level 1] "
                "Persistent CPU spike detected."
            )

        # -----------------------------------------------------
        # Memory
        # -----------------------------------------------------

        elif trigger_reason == "MEMORY_SPIKE":

            mode = "REDUCE_MUTATION_SIZE"

            print(
                "[Level 1] "
                "Persistent memory growth detected."
            )

        # -----------------------------------------------------
        # CPU + memory
        # -----------------------------------------------------

        elif trigger_reason == "CPU_MEMORY_SPIKE":

            mode = "STRONG_THROTTLE"

            print(
                "[Level 1] "
                "CPU and memory pressure detected."
            )

        # -----------------------------------------------------
        # New unique crash
        # -----------------------------------------------------

        elif trigger_reason == "CRASH_FOUND":

            mode = "EXPLOIT_CRASH"

            print(
                "[Level 1] "
                "NEW unique crash detected."
            )

        # -----------------------------------------------------
        # Timeout
        # -----------------------------------------------------

        elif trigger_reason == "TIMEOUT_SPIKE":

            mode = "SKIP_TIMEOUTS"

            print(
                "[Level 1] "
                "Timeout spike detected."
            )

        # -----------------------------------------------------
        # Coverage slowdown
        # -----------------------------------------------------

        else:

            mode = "EXPLORE_HEAVY"

            print(
                "[Level 1] "
                "Coverage improvement is low."
            )

        # -----------------------------------------------------
        # Remember intervention
        # -----------------------------------------------------

        self.last_adaptation = {

            "window_id": window_id,

            "mode": mode,

            "trigger_reason": trigger_reason,

            "ces": ces_score,

            "timestamp": time.time()
        }

        # -----------------------------------------------------
        # Send signal
        # -----------------------------------------------------

        self._write_signal(
            mode
        )

        return mode

    # ---------------------------------------------------------
    # Evaluate adaptation result
    # ---------------------------------------------------------

    def evaluate_adaptation(
        self,
        improved
    ):

        if self.last_adaptation is None:

            return

        if improved:

            print(
                "[Adaptive Controller] "
                "Intervention improved performance."
            )

            self.unsuccessful_adaptation_counter = 0

        else:

            self.unsuccessful_adaptation_counter += 1

            print(
                "[Adaptive Controller] "
                "Intervention did NOT improve "
                "performance."
            )

            print(
                "[Adaptive Controller] "
                "Unsuccessful adaptations: "
                f"{self.unsuccessful_adaptation_counter}/"
                f"{self.escalation_threshold}"
            )

        self.last_adaptation = None

    # ---------------------------------------------------------
    # Check AgentAFL escalation
    # ---------------------------------------------------------

    def should_trigger_agentafl(
        self,
        coverage_stagnant,
        ces_low
    ):

        return (
            coverage_stagnant
            and
            ces_low
            and
            self.unsuccessful_adaptation_counter
            >=
            self.escalation_threshold
        )

    # ---------------------------------------------------------
    # Trigger AgentAFL
    # ---------------------------------------------------------

    def trigger_agentafl(self):

        print(
            "\n[Level 2] "
            "Persistent stagnation detected."
        )

        print(
            "[Level 2] "
            "Level 1 adaptations were unsuccessful."
        )

        print(
            "[Level 2] "
            "AgentAFL should now be triggered."
        )

        self._write_signal(
            "AGENT_AFL"
        )

    # ---------------------------------------------------------
    # Reset
    # ---------------------------------------------------------

    def reset_counter(self):

        self.cpu_abnormal_counter = 0

        self.memory_abnormal_counter = 0

        self.resource_abnormal_counter = 0

        self.unsuccessful_adaptation_counter = 0

        self._write_signal(
            "DEFAULT"
        )


# -------------------------------------------------------------
# Direct test
# -------------------------------------------------------------

if __name__ == "__main__":

    controller = AdaptiveFuzzingController(
        persistence_windows=3,
        escalation_threshold=3
    )

    # Simulate persistent CPU abnormality

    print("\n--- Window 1 ---")

    print(
        controller.determine_trigger(
            unique_crashes=0,
            cpu_abnormal=True,
            memory_abnormal=False
        )
    )

    print("\n--- Window 2 ---")

    print(
        controller.determine_trigger(
            unique_crashes=0,
            cpu_abnormal=True,
            memory_abnormal=False
        )
    )

    print("\n--- Window 3 ---")

    trigger = controller.determine_trigger(
        unique_crashes=0,
        cpu_abnormal=True,
        memory_abnormal=False
    )

    print(trigger)

    if trigger:

        controller.apply_level1_adaptation(
            window_id=3,
            ces_score=0.0,
            trigger_reason=trigger
        )