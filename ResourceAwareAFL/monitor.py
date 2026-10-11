import os
import sys
import time
import json
import csv
import psutil


class AFLRuntimeMonitor:

    def __init__(
        self,
        fuzzer_out_dir="out",
        window_sec=30,
        output_log="raw_metrics.json",
        experiment_csv="experiment_metrics.csv",
        calibration_windows=10
    ):

        self.fuzzer_out_dir = fuzzer_out_dir
        self.window_sec = window_sec
        self.output_log = output_log
        self.experiment_csv = experiment_csv
        self.calibration_windows = calibration_windows

        self.monitor_start_time = time.monotonic()

        self.window_id = 0

        # Used for memory-time integration
        self.previous_sample_time = None
        self.cumulative_memory_mb_seconds = 0.0

        # Used to make CPU time relative to monitoring start
        self.initial_cpu_seconds = None

        # Used to calculate average CPU usage per monitoring window
        self.previous_cpu_seconds = None
        self.previous_cpu_sample_time = None

        # Initial number of edges before measuring discoveries
        self.initial_edges = None

        # AFL++ fuzzer_stats
        self.stats_file = os.path.join(
            self.fuzzer_out_dir,
            "default",
            "fuzzer_stats"
        )

        fallback = os.path.join(
            self.fuzzer_out_dir,
            "fuzzer_stats"
        )

        if (
            not os.path.exists(self.stats_file)
            and os.path.exists(fallback)
        ):
            self.stats_file = fallback

        self._create_csv()

    # ---------------------------------------------------------
    # Create CSV
    # ---------------------------------------------------------

    def _create_csv(self):

        if os.path.exists(self.experiment_csv):
            return

        with open(
            self.experiment_csv,
            "w",
            newline=""
        ) as file:

            writer = csv.writer(file)

            writer.writerow([
                "window_id",
                "elapsed_sec",
                "coverage_pct",
                "edges_found",
                "new_edges",
                "execs_done",
                "execs_per_sec",
                "cpu_percent",
                "memory_mb",
                "saved_crashes",
                "saved_hangs",
                "cpu_total_seconds",
                "memory_mb_seconds",
                "cpu_seconds_per_new_edge",
                "memory_mb_seconds_per_new_edge"
            ])

    # ---------------------------------------------------------
    # Safe conversions
    # ---------------------------------------------------------

    def _safe_int(
        self,
        value,
        default=0
    ):

        try:
            return int(value)

        except (
            ValueError,
            TypeError
        ):
            return default

    def _safe_float(
        self,
        value,
        default=0.0
    ):

        try:
            return float(value)

        except (
            ValueError,
            TypeError
        ):
            return default

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

                    stats[
                        key.strip()
                    ] = value.strip()

        except Exception as error:

            print(
                f"[-] Error reading fuzzer_stats: "
                f"{error}"
            )

        return stats

    # ---------------------------------------------------------
    # Get AFL++ process using fuzzer_pid
    # ---------------------------------------------------------

    def _get_fuzzer_process(
        self,
        stats
    ):

        fuzzer_pid = self._safe_int(
            stats.get(
                "fuzzer_pid",
                0
            )
        )

        if fuzzer_pid <= 0:
            return None

        try:

            process = psutil.Process(
                fuzzer_pid
            )

            if not process.is_running():
                return None

            return process

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied
        ):

            return None

    # ---------------------------------------------------------
    # Collect snapshot
    # ---------------------------------------------------------

    def collect_snapshot(self):

        stats = self._parse_fuzzer_stats()

        if not stats:

            print(
                "[Monitor] Waiting for AFL++ "
                "fuzzer_stats..."
            )

            return None

        process = self._get_fuzzer_process(
            stats
        )

        if process is None:

            print(
                "[Monitor] AFL++ process is not "
                "running."
            )

            return "STOPPED"

        self.window_id += 1

        current_sample_time = time.monotonic()

        # -----------------------------------------------------
        # Elapsed experiment time
        # -----------------------------------------------------

        elapsed_sec = (
            current_sample_time
            -
            self.monitor_start_time
        )

        # -----------------------------------------------------
        # Coverage
        # -----------------------------------------------------

        coverage_string = stats.get(
            "bitmap_cvg",
            "0.00%"
        ).replace(
            "%",
            ""
        )

        total_coverage = self._safe_float(
            coverage_string
        )

        # -----------------------------------------------------
        # Edges
        # -----------------------------------------------------

        edges_found = self._safe_int(
            stats.get(
                "edges_found",
                0
            )
        )

        # Store initial edge count from seed corpus
        if self.initial_edges is None:

            self.initial_edges = (
                edges_found
            )

        # Count edges discovered after monitoring started
        new_edges = max(
            0,
            edges_found
            -
            self.initial_edges
        )

        # -----------------------------------------------------
        # Executions
        # -----------------------------------------------------

        execs_done = self._safe_int(
            stats.get(
                "execs_done",
                0
            )
        )

        execs_per_sec = self._safe_float(
            stats.get(
                "execs_per_sec",
                0
            )
        )

        # -----------------------------------------------------
        # Crashes + hangs
        # -----------------------------------------------------

        total_crashes = self._safe_int(
            stats.get(
                "saved_crashes",
                0
            )
        )

        total_hangs = self._safe_int(
            stats.get(
                "saved_hangs",
                0
            )
        )

        # -----------------------------------------------------
        # CPU + memory
        # -----------------------------------------------------

        try:

            # -------------------------------------------------
            # AFL++ process tree
            # -------------------------------------------------

            processes = [
                process
            ]

            try:

                processes.extend(
                    process.children(
                        recursive=True
                    )
                )

            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied
            ):

                pass

            # -------------------------------------------------
            # Total memory used by AFL++ process tree
            # -------------------------------------------------

            total_rss_bytes = 0

            for proc in processes:

                try:

                    total_rss_bytes += (
                        proc.memory_info().rss
                    )

                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied
                ):

                    continue

            memory_mb = (
                total_rss_bytes
                /
                (1024 * 1024)
            )

            # -------------------------------------------------
            # Total cumulative CPU time for AFL++ process tree
            # -------------------------------------------------

            absolute_cpu_seconds = 0.0

            for proc in processes:

                try:

                    cpu_times = (
                        proc.cpu_times()
                    )

                    absolute_cpu_seconds += (
                        cpu_times.user
                        +
                        cpu_times.system
                    )

                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied
                ):

                    continue

            # -------------------------------------------------
            # Average CPU utilisation over monitoring window
            # -------------------------------------------------

            current_cpu_sample_time = (
                time.monotonic()
            )

            if (
                self.previous_cpu_seconds
                is None
                or
                self.previous_cpu_sample_time
                is None
            ):

                cpu_percent = 0.0

            else:

                delta_cpu_seconds = (
                    absolute_cpu_seconds
                    -
                    self.previous_cpu_seconds
                )

                delta_wall_seconds = (
                    current_cpu_sample_time
                    -
                    self.previous_cpu_sample_time
                )

                if delta_wall_seconds > 0:

                    cpu_percent = (
                        delta_cpu_seconds
                        /
                        delta_wall_seconds
                    ) * 100.0

                else:

                    cpu_percent = 0.0

            self.previous_cpu_seconds = (
                absolute_cpu_seconds
            )

            self.previous_cpu_sample_time = (
                current_cpu_sample_time
            )

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied
        ):

            print(
                "[Monitor] AFL++ disappeared "
                "during measurement."
            )

            return "STOPPED"

        # -----------------------------------------------------
        # CPU time
        # -----------------------------------------------------

        if self.initial_cpu_seconds is None:

            self.initial_cpu_seconds = (
                absolute_cpu_seconds
            )

        cpu_total_seconds = (
            absolute_cpu_seconds
            -
            self.initial_cpu_seconds
        )

        cpu_total_seconds = max(
            0.0,
            cpu_total_seconds
        )

        # -----------------------------------------------------
        # Memory-time accumulation
        # -----------------------------------------------------

        if self.previous_sample_time is None:

            delta_time = 0.0

        else:

            delta_time = (
                current_sample_time
                -
                self.previous_sample_time
            )

        self.previous_sample_time = (
            current_sample_time
        )

        memory_this_window = (
            memory_mb
            *
            delta_time
        )

        self.cumulative_memory_mb_seconds += (
            memory_this_window
        )

        # -----------------------------------------------------
        # Resource cost per NEW edge
        # -----------------------------------------------------

        if new_edges > 0:

            cpu_seconds_per_new_edge = (
                cpu_total_seconds
                /
                new_edges
            )

            memory_mb_seconds_per_new_edge = (
                self.cumulative_memory_mb_seconds
                /
                new_edges
            )

        else:

            cpu_seconds_per_new_edge = 0.0

            memory_mb_seconds_per_new_edge = (
                0.0
            )

        # -----------------------------------------------------
        # Build payload
        # -----------------------------------------------------

        payload = {

            "window_id":
                self.window_id,

            "timestamp":
                time.time(),

            "raw_metrics": {

                "total_coverage":
                    round(
                        total_coverage,
                        4
                    ),

                "total_crashes":
                    total_crashes,

                "cpu_percent":
                    round(
                        cpu_percent,
                        2
                    ),

                "memory_mb":
                    round(
                        memory_mb,
                        2
                    ),

                "elapsed_sec":
                    round(
                        elapsed_sec,
                        2
                    ),

                "edges_found":
                    edges_found,

                "new_edges":
                    new_edges,

                "execs_done":
                    execs_done,

                "execs_per_sec":
                    round(
                        execs_per_sec,
                        2
                    ),

                "saved_hangs":
                    total_hangs,

                "cpu_total_seconds":
                    round(
                        cpu_total_seconds,
                        4
                    ),

                "memory_mb_seconds":
                    round(
                        self.cumulative_memory_mb_seconds,
                        4
                    ),

                "cpu_seconds_per_new_edge":
                    round(
                        cpu_seconds_per_new_edge,
                        8
                    ),

                "memory_mb_seconds_per_new_edge":
                    round(
                        memory_mb_seconds_per_new_edge,
                        8
                    )
            }
        }

        return payload

    # ---------------------------------------------------------
    # Write CSV
    # ---------------------------------------------------------

    def _write_csv(
        self,
        snapshot
    ):

        metrics = snapshot[
            "raw_metrics"
        ]

        with open(
            self.experiment_csv,
            "a",
            newline=""
        ) as file:

            writer = csv.writer(
                file
            )

            writer.writerow([

                snapshot[
                    "window_id"
                ],

                metrics[
                    "elapsed_sec"
                ],

                metrics[
                    "total_coverage"
                ],

                metrics[
                    "edges_found"
                ],

                metrics[
                    "new_edges"
                ],

                metrics[
                    "execs_done"
                ],

                metrics[
                    "execs_per_sec"
                ],

                metrics[
                    "cpu_percent"
                ],

                metrics[
                    "memory_mb"
                ],

                metrics[
                    "total_crashes"
                ],

                metrics[
                    "saved_hangs"
                ],

                metrics[
                    "cpu_total_seconds"
                ],

                metrics[
                    "memory_mb_seconds"
                ],

                metrics[
                    "cpu_seconds_per_new_edge"
                ],

                metrics[
                    "memory_mb_seconds_per_new_edge"
                ]
            ])

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
            f"[+] JSON output: "
            f"{os.path.abspath(self.output_log)}"
        )

        print(
            f"[+] Experiment CSV: "
            f"{os.path.abspath(self.experiment_csv)}"
        )

        print(
            f"[*] Looking for AFL++ stats at: "
            f"{self.stats_file}"
        )

        while True:

            snapshot = (
                self.collect_snapshot()
            )

            if snapshot == "STOPPED":

                print(
                    "[+] AFL++ stopped. "
                    "Ending monitor."
                )

                break

            if snapshot is None:

                time.sleep(
                    self.window_sec
                )

                continue

            # -------------------------------------------------
            # JSON
            # -------------------------------------------------

            with open(
                self.output_log,
                "a"
            ) as file:

                file.write(
                    json.dumps(
                        snapshot
                    )
                    +
                    "\n"
                )

                file.flush()

            # -------------------------------------------------
            # CSV
            # -------------------------------------------------

            self._write_csv(
                snapshot
            )

            # -------------------------------------------------
            # Display
            # -------------------------------------------------

            metrics = snapshot[
                "raw_metrics"
            ]

            print(
                f"[Monitor] "
                f"Window "
                f"{snapshot['window_id']:03d} | "
                f"Time: "
                f"{metrics['elapsed_sec']:.0f}s | "
                f"Cov: "
                f"{metrics['total_coverage']}% | "
                f"Edges: "
                f"{metrics['edges_found']} | "
                f"New Edges: "
                f"{metrics['new_edges']} | "
                f"Execs: "
                f"{metrics['execs_done']} | "
                f"CPU: "
                f"{metrics['cpu_percent']}% | "
                f"Memory: "
                f"{metrics['memory_mb']} MB | "
                f"CPU-Time: "
                f"{metrics['cpu_total_seconds']:.2f}s | "
                f"CPU/NewEdge: "
                f"{metrics['cpu_seconds_per_new_edge']:.6f}"
            )

            time.sleep(
                self.window_sec
            )


# -------------------------------------------------------------
# Main
# -------------------------------------------------------------

if __name__ == "__main__":

    fuzzer_output_directory = (
        "out"
    )

    if len(sys.argv) > 1:

        fuzzer_output_directory = (
            sys.argv[1]
        )

    monitor = AFLRuntimeMonitor(

        fuzzer_out_dir=(
            fuzzer_output_directory
        ),

        window_sec=30,

        output_log=(
            "raw_metrics.json"
        ),

        experiment_csv=(
            "experiment_metrics.csv"
        ),

        calibration_windows=10
    )

    monitor.start_monitoring()