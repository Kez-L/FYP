import os
import time


class AdaptiveFuzzingController:
    def __init__(
        self,
        fuzzer_out_dir="out",
        persistence_windows=3
    ):
        self.fuzzer_out_dir = fuzzer_out_dir

        self.signal_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "adaptation"
        )

        self.signal_file = os.path.join(
            self.signal_dir,
            ".adapt_signal"
        )

        self.persistence_windows = persistence_windows

        # Runtime state
        self.cpu_abnormal_counter = 0
        self.memory_abnormal_counter = 0
        self.resource_abnormal_counter = 0

        self.previous_unique_crashes = 0
        self.previous_coverage = None

        self.last_adaptation = None
        self.active_mode = "DEFAULT"

        # True while an adaptation is being evaluated
        self.awaiting_recovery = False

        os.makedirs(
            self.signal_dir,
            exist_ok=True
        )

        self._write_signal("DEFAULT")

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

            self.active_mode = mode

            print(
                f"[Adaptive Controller] "
                f"Signal -> {mode}"
            )

        except OSError as error:
            print(
                f"[-] Failed to write signal: "
                f"{error}"
            )

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

    def apply_level1_adaptation(
        self,
        window_id,
        ces_score,
        trigger_reason
    ):
        if self.awaiting_recovery:
            print(
                "[Adaptive Controller] "
                "Level-1 adaptation blocked while "
                "recovery is being evaluated."
            )
            return None

        print(
            f"\n[Adaptive Controller] "
            f"Window {window_id}: "
            f"CES = {ces_score:.6f}"
        )

        print(
            f"[Adaptive Controller] "
            f"Trigger = {trigger_reason}"
        )

        if trigger_reason == "CPU_SPIKE":
            mode = "THROTTLE_POWER"

            print(
                "[Level 1] "
                "Persistent CPU pressure with low "
                "effectiveness detected."
            )

        elif trigger_reason == "MEMORY_SPIKE":
            mode = "REDUCE_MUTATION_SIZE"

            print(
                "[Level 1] "
                "Persistent memory growth with low "
                "effectiveness detected."
            )

        elif trigger_reason == "CPU_MEMORY_SPIKE":
            mode = "STRONG_THROTTLE"

            print(
                "[Level 1] "
                "Persistent CPU and memory pressure "
                "with low effectiveness detected."
            )

        elif trigger_reason == "CRASH_FOUND":
            mode = "EXPLOIT_CRASH"

            print(
                "[Level 1] "
                "New unique crash detected."
            )

        elif trigger_reason == "TIMEOUT_SPIKE":
            mode = "SKIP_TIMEOUTS"

            print(
                "[Level 1] "
                "Timeout pressure detected."
            )

        elif trigger_reason == "COVERAGE_SLOWDOWN":
            mode = "EXPLORE_HEAVY"

            print(
                "[Level 1] "
                "Persistent coverage slowdown detected."
            )

        else:
            print(
                "[Adaptive Controller] "
                f"Unknown trigger: {trigger_reason}"
            )
            return None

        self.last_adaptation = {
            "window_id": window_id,
            "mode": mode,
            "trigger_reason": trigger_reason,
            "ces": ces_score,
            "timestamp": time.time()
        }

        self.awaiting_recovery = True

        self._write_signal(mode)

        return mode

    def evaluate_adaptation(self, improved):
        if self.last_adaptation is None:
            return

        previous_mode = self.last_adaptation["mode"]

        if improved:
            print(
                "[Adaptive Controller] "
                f"Intervention {previous_mode} improved performance."
            )
        else:
            print(
                "[Adaptive Controller] "
                f"Intervention {previous_mode} did NOT improve performance."
            )

        print(
            "[Adaptive Controller] "
            "Recovery evaluation completed. "
            "Returning to DEFAULT."
        )

        self.awaiting_recovery = False
        self._write_signal("DEFAULT")
        self.last_adaptation = None

    def reset_counter(self):
        self.cpu_abnormal_counter = 0
        self.memory_abnormal_counter = 0
        self.resource_abnormal_counter = 0