import os
import time


class AdaptiveFuzzingController:

    def __init__(
        self,
        fuzzer_out_dir="out"
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

        self.low_efficiency_counter = 0

        self.escalation_threshold = 3

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
    # Apply adaptation
    # ---------------------------------------------------------

    def apply_level1_adaptation(
        self,
        window_id,
        ces_score,
        trigger_reason
    ):

        self.low_efficiency_counter += 1

        print(
            f"\n[Adaptive Controller] "
            f"Window {window_id}: "
            f"CES = {ces_score:.6f}"
        )

        print(
            f"[Adaptive Controller] "
            f"Low efficiency count: "
            f"{self.low_efficiency_counter}/"
            f"{self.escalation_threshold}"
        )

        # -----------------------------------------------------
        # CPU
        # -----------------------------------------------------

        if trigger_reason == "CPU_SPIKE":

            mode = "THROTTLE_POWER"

            print(
                "[Level 1] "
                "CPU spike detected."
            )

        # -----------------------------------------------------
        # Memory
        # -----------------------------------------------------

        elif trigger_reason == "MEMORY_SPIKE":

            mode = "REDUCE_MUTATION_SIZE"

            print(
                "[Level 1] "
                "Memory growth detected."
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
        # Crash
        # -----------------------------------------------------

        elif trigger_reason == "CRASH_FOUND":

            mode = "EXPLOIT_CRASH"

            print(
                "[Level 1] "
                "New crash detected."
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
        # Plateau placeholder
        # -----------------------------------------------------

        if (
            self.low_efficiency_counter
            >= self.escalation_threshold
        ):

            print(
                "[Level 2] "
                "Low efficiency sustained."
            )

            self._trigger_plateau_placeholder()

        # -----------------------------------------------------
        # Send signal
        # -----------------------------------------------------

        self._write_signal(
            mode
        )

    # ---------------------------------------------------------
    # Reset
    # ---------------------------------------------------------

    def reset_counter(self):

        if self.low_efficiency_counter > 0:

            print(
                "[Adaptive Controller] "
                "Performance recovered."
            )

        self.low_efficiency_counter = 0

        self._write_signal(
            "DEFAULT"
        )

    # ---------------------------------------------------------
    # Plateau placeholder
    # ---------------------------------------------------------

    def _trigger_plateau_placeholder(self):

        print(
            "[Plateau Placeholder] "
            "Sustained plateau detected."
        )

        print(
            "[Plateau Placeholder] "
            "AgentAFL/LLM recovery would "
            "be invoked here."
        )


if __name__ == "__main__":

    controller = AdaptiveFuzzingController()

    controller.apply_level1_adaptation(
        window_id=1,
        ces_score=0.0,
        trigger_reason="CPU_SPIKE"
    )
