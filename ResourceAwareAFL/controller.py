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
            os.path.dirname(
                os.path.abspath(__file__)
            ),
            "adaptation"
        )

        self.signal_file = os.path.join(
            self.signal_dir,
            ".adapt_signal"
        )

        self.persistence_windows = persistence_windows

        # -----------------------------------------------------
        # Runtime state
        # -----------------------------------------------------

        self.cpu_abnormal_counter = 0
        self.memory_abnormal_counter = 0
        self.resource_abnormal_counter = 0

        self.previous_unique_crashes = 0
        self.previous_coverage = None

        self.last_adaptation = None

        self.active_mode = "DEFAULT"

        # -----------------------------------------------------
        # Recovery / AgentAFL state
        # -----------------------------------------------------

        # True immediately after a Level-1 adaptation.
        self.awaiting_recovery = False

        # True when Level-1 did not recover the campaign.
        self.recovery_failed = False

        # True when the persistent plateau requires AgentAFL.
        self.agentafl_escalation_pending = False

        os.makedirs(
            self.signal_dir,
            exist_ok=True
        )

        self._write_signal("DEFAULT")

    # ---------------------------------------------------------
    # Write adaptation signal
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
    # Apply Level-1 adaptation
    # ---------------------------------------------------------

    def apply_level1_adaptation(
        self,
        window_id,
        ces_score,
        trigger_reason
    ):

        # Do not apply another Level-1 adaptation while
        # recovery is being evaluated or AgentAFL is pending.

        if (
            self.awaiting_recovery
            or self.recovery_failed
            or self.agentafl_escalation_pending
        ):

            print(
                "[Adaptive Controller] "
                "Level-1 adaptation blocked while "
                "recovery/escalation is being evaluated."
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

        # -----------------------------------------------------
        # Select adaptation
        # -----------------------------------------------------

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

        # -----------------------------------------------------
        # Store intervention
        # -----------------------------------------------------

        self.last_adaptation = {
            "window_id": window_id,
            "mode": mode,
            "trigger_reason": trigger_reason,
            "ces": ces_score,
            "timestamp": time.time()
        }

        # -----------------------------------------------------
        # Enter recovery-observation state
        # -----------------------------------------------------

        self.awaiting_recovery = True
        self.recovery_failed = False

        # -----------------------------------------------------
        # Send signal to adaptive mutator
        # -----------------------------------------------------

        self._write_signal(mode)

        return mode

    # ---------------------------------------------------------
    # Evaluate Level-1 recovery
    # ---------------------------------------------------------

    def evaluate_adaptation(
        self,
        improved
    ):

        if self.last_adaptation is None:
            return

        if improved:

            successful_mode = self.last_adaptation["mode"]

            print(
                "[Adaptive Controller] "
                "Intervention improved performance."
            )

            print(
                "[Adaptive Controller] "
                f"Retaining successful strategy: {successful_mode}"
            )

            self.awaiting_recovery = False
            self.recovery_failed = False
            self.agentafl_escalation_pending = False

            self._write_signal(successful_mode)

        else:

            print(
                "[Adaptive Controller] "
                "Intervention did NOT improve performance."
            )

            print(
                "[Adaptive Controller] "
                "Checking whether the coverage plateau "
                "persists."
            )

            # Level-1 has failed.
            # Do not immediately perform another Level-1
            # adaptation.
            self.awaiting_recovery = False
            self.recovery_failed = True
            self._write_signal("DEFAULT")

        self.last_adaptation = None

    # ---------------------------------------------------------
    # Determine whether AgentAFL is required
    # ---------------------------------------------------------

    def should_trigger_agentafl(
        self,
        coverage_stagnant,
        ces_low
    ):

        return (
            self.recovery_failed
            and coverage_stagnant
            and ces_low
            and not self.agentafl_escalation_pending
        )

    # ---------------------------------------------------------
    # Trigger AgentAFL handoff
    # ---------------------------------------------------------

    def trigger_agentafl(self):

        if self.agentafl_escalation_pending:
            return

        print(
            "\n[Level 2] "
            "Level-1 adaptation did not recover "
            "the campaign."
        )

        print(
            "[Level 2] "
            "Coverage plateau persists."
        )

        print(
            "[Level 2] "
            "AgentAFL handoff required."
        )

        self.agentafl_escalation_pending = True
        self.recovery_failed = False
        self.awaiting_recovery = False

        # This is currently the interface boundary.
        # Keziah's AgentAFL will later consume this state.
        self._write_signal("AGENT_AFL")

    # ---------------------------------------------------------
    # AgentAFL completion
    # ---------------------------------------------------------

    def agentafl_completed(self):

        print(
            "[Level 2] "
            " AgentAFL completion received."
        )

        print(
            "[Level 2] "
            "Returning to monitoring."
        )

        self.agentafl_escalation_pending = False
        self.recovery_failed = False
        self.awaiting_recovery = False

        self._write_signal("DEFAULT")

    # ---------------------------------------------------------
    # Reset transient resource counters
    # ---------------------------------------------------------

    def reset_counter(self):

        self.cpu_abnormal_counter = 0
        self.memory_abnormal_counter = 0
        self.resource_abnormal_counter = 0

        # Only reset transient detection counters.
        # Do not change the active mutation strategy here.