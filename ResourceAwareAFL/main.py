import os
import time
import json

from cea import CostEffectivenessAnalyzer
from controller import AdaptiveFuzzingController


def determine_trigger_reason(analysis_result):
    """
    Determine the most relevant lightweight adaptation
    based on the current monitoring result.
    """

    delta_cov = analysis_result["delta_cov"]
    delta_crashes = analysis_result["delta_crashes"]

    # A new crash has been observed.
    if delta_crashes > 0:
        return "CRASH_FOUND"

    # No coverage improvement.
    if delta_cov <= 0:
        return "COVERAGE_SLOWDOWN"

    return "COVERAGE_SLOWDOWN"


def run_stream_analyzer(
    log_file="raw_metrics.json"
):

    analyzer = CostEffectivenessAnalyzer(
        alpha=0.5,
        beta=0.5,
        lambda_u=0.01,
        lambda_m=0.001,
        threshold_ces=0.001
    )

    controller = AdaptiveFuzzingController(
        fuzzer_out_dir="out"
    )

    print(
        "[+] Starting Cost-Effectiveness Analyzer..."
    )

    print(
        f"[+] Streaming metrics from: "
        f"{os.path.abspath(log_file)}"
    )

    # -------------------------------------------------------------
    # Wait for monitor to create the log.
    # -------------------------------------------------------------

    while not os.path.exists(log_file):

        print(
            f"[*] Waiting for {log_file} "
            f"to be created by monitor.py..."
        )

        time.sleep(2)

    # -------------------------------------------------------------
    # Continuously read monitoring results.
    # -------------------------------------------------------------

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
                # Calculate CE, CrE and CES.
                # -------------------------------------------------

                analysis_result = (
                    analyzer.compute_ces(record)
                )

                window_id = (
                    analysis_result["window_id"]
                )

                delta_cov = (
                    analysis_result["delta_cov"]
                )

                delta_crashes = (
                    analysis_result["delta_crashes"]
                )

                ces_score = (
                    analysis_result["CES"]
                )

                status = (
                    analysis_result["status"]
                )

                print(
                    f"[Window {window_id:03d}] "
                    f"ΔCov: {delta_cov:<8} | "
                    f"ΔCrashes: {delta_crashes:<2} | "
                    f"CE: {analysis_result['CE']:<10} | "
                    f"CrE: {analysis_result['CrE']:<10} | "
                    f"CES: {ces_score:<10} | "
                    f"Status: [{status}]"
                )

                # -------------------------------------------------
                # Lightweight adaptation.
                # -------------------------------------------------

                if status == "LOW_EFFICIENCY":

                    trigger_reason = (
                        determine_trigger_reason(
                            analysis_result
                        )
                    )

                    controller.apply_adaptation(
                        window_id=window_id,
                        ces_score=ces_score,
                        trigger_reason=trigger_reason
                    )

                elif status == "HEALTHY":

                    print(
                        "[Adaptive Controller] "
                        "Performance is healthy. "
                        "No adaptation required."
                    )

                # -------------------------------------------------
                # Plateau placeholder.
                # -------------------------------------------------

                # Plateau detection / AgentAFL recovery
                # will be connected here later.

            except json.JSONDecodeError:

                continue

            except Exception as error:

                print(
                    f"[-] Error processing entry: "
                    f"{error}"
                )

                time.sleep(1)


if __name__ == "__main__":

    run_stream_analyzer()
