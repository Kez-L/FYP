import os
import sys
import time
import json
import psutil


class AFLRuntimeMonitor:

    def __init__(
        self,
        fuzzer_out_dir="out",
        target_proc_name="afl-fuzz",
        window_sec=10,
        output_log="raw_metrics.json"
    ):

        self.fuzzer_out_dir = fuzzer_out_dir
        self.target_proc_name = target_proc_name
        self.window_sec = window_sec
        self.output_log = output_log

        self.window_id = 0

        # AFL++ fuzzer_stats location
        self.stats_file = os.path.join(
            self.fuzzer_out_dir,
            "default",
            "fuzzer_stats"
        )

        # Fallback
        if not os.path.exists(self.stats_file):

            self.stats_file = os.path.join(
                self.fuzzer_out_dir,
                "fuzzer_stats"
            )

    # ---------------------------------------------------------
    # Find AFL++ process
    # ---------------------------------------------------------

    def _get_fuzzer_process(self):

        for proc in psutil.process_iter(
            ["pid", "name", "cmdline"]
        ):

            try:

                name = proc.info["name"] or ""

                cmdline = " ".join(
                    proc.info["cmdline"] or []
                )

                if (
                    self.target_proc_name in name
                    or self.target_proc_name in cmdline
                ):

                    return proc

            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied
            ):

                continue

        return None

    # ---------------------------------------------------------
    # Read AFL++ statistics
    # ---------------------------------------------------------

    def _parse_fuzzer_stats(self):

        stats = {}

        if not os.path.exists(self.stats_file):
            return stats

        try:

            with open(
                self.stats_file,
                "r"
            ) as file:

                for line in file:

                    if ":" not in line:
                        continue

                    key, value = line.strip().split(
                        ":",
                        1
                    )

                    stats[key.strip()] = value.strip()

        except Exception as error:

            print(
                f"[-] Error reading fuzzer_stats: {error}"
            )

        return stats

    # ---------------------------------------------------------
    # Collect one monitoring window
    # ---------------------------------------------------------

    def collect_snapshot(self):

        self.window_id += 1

        timestamp = time.time()

        stats = self._parse_fuzzer_stats()

        # -----------------------------------------------------
        # Coverage
        # -----------------------------------------------------

        coverage_string = stats.get(
            "bitmap_cvg",
            "0.00%"
        )

        coverage_string = coverage_string.replace(
            "%",
            ""
        )

        try:

            total_coverage = float(
                coverage_string
            )

        except ValueError:

            total_coverage = 0.0

        # -----------------------------------------------------
        # Crashes
        # -----------------------------------------------------

        try:

            total_crashes = int(
                stats.get(
                    "unique_crashes",
                    0
                )
            )

        except ValueError:

            total_crashes = 0

        # -----------------------------------------------------
        # CPU + memory
        # -----------------------------------------------------

        process = self._get_fuzzer_process()

        if process is not None:

            try:

                cpu_percent = process.cpu_percent(
                    interval=0.1
                )

                memory_info = process.memory_info()

                memory_mb = (
                    memory_info.rss /
                    (1024 * 1024)
                )

            except Exception:

                cpu_percent = 0.0
                memory_mb = 0.0

        else:

            cpu_percent = psutil.cpu_percent(
                interval=0.1
            )

            memory_mb = (
                psutil.virtual_memory().used /
                (1024 * 1024)
            )

        # -----------------------------------------------------
        # Build record
        # -----------------------------------------------------

        payload = {

            "window_id": self.window_id,

            "timestamp": timestamp,

            "raw_metrics": {

                "total_coverage": round(
                    total_coverage,
                    4
                ),

                "total_crashes": total_crashes,

                "cpu_percent": round(
                    cpu_percent,
                    2
                ),

                "memory_mb": round(
                    memory_mb,
                    2
                )
            }
        }

        return payload

    # ---------------------------------------------------------
    # Start monitoring
    # ---------------------------------------------------------

    def start_monitoring(self):

        print(
            "[+] AFL++ Runtime Monitor started"
        )

        print(
            f"[+] Monitoring window: "
            f"{self.window_sec} seconds"
        )

        print(
            f"[+] Output file: "
            f"{os.path.abspath(self.output_log)}"
        )

        print(
            f"[*] Looking for AFL++ stats at: "
            f"{self.stats_file}"
        )

        while True:

            snapshot = self.collect_snapshot()

            with open(
                self.output_log,
                "a"
            ) as file:

                file.write(
                    json.dumps(snapshot) + "\n"
                )

                file.flush()

            metrics = snapshot["raw_metrics"]

            print(
                f"[Monitor] "
                f"Window {snapshot['window_id']:03d} | "
                f"Cov: {metrics['total_coverage']}% | "
                f"Crashes: {metrics['total_crashes']} | "
                f"CPU: {metrics['cpu_percent']}% | "
                f"Memory: {metrics['memory_mb']} MB"
            )

            time.sleep(
                self.window_sec
            )


# -------------------------------------------------------------
# Main
# -------------------------------------------------------------

if __name__ == "__main__":

    fuzzer_output_directory = "out"

    if len(sys.argv) > 1:

        fuzzer_output_directory = sys.argv[1]

    monitor = AFLRuntimeMonitor(
        fuzzer_out_dir=fuzzer_output_directory,
        window_sec=10,
        output_log="raw_metrics.json"
    )

    monitor.start_monitoring()
