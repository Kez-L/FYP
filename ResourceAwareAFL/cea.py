class CostEffectivenessAnalyzer:

    """
    Cost-Effectiveness Analyzer.

    Implements:

        Eq. (2): Coverage Efficiency (CE)
        Eq. (3): Crash Efficiency (CrE)
        Eq. (4): Cost-Effectiveness Score (CES)
    """

    def __init__(
        self,
        alpha=0.5,
        beta=0.5,
        lambda_u=0.01,
        lambda_m=0.001,
        epsilon=1e-6,
        threshold_ces=0.001
    ):

        if abs((alpha + beta) - 1.0) > 1e-5:

            raise ValueError(
                "alpha + beta must equal 1.0"
            )

        self.alpha = alpha
        self.beta = beta

        self.lambda_u = lambda_u
        self.lambda_m = lambda_m

        self.epsilon = epsilon
        self.threshold_ces = threshold_ces

        self.prev_record = None

    # ---------------------------------------------------------
    # Calculate CES
    # ---------------------------------------------------------

    def compute_ces(self, current_record):

        # -----------------------------------------------------
        # First window = baseline
        # -----------------------------------------------------

        if self.prev_record is None:

            self.prev_record = current_record

            metrics = current_record["raw_metrics"]

            return {

                "window_id":
                    current_record.get(
                        "window_id",
                        0
                    ),

                "delta_time": 0.0,

                "delta_cov": 0.0,

                "delta_crashes": 0,

                "resource_cost": 0.0,

                "CE": 0.0,

                "CrE": 0.0,

                "CES": 0.0,

                "cpu_percent":
                    metrics.get(
                        "cpu_percent",
                        0.0
                    ),

                "memory_mb":
                    metrics.get(
                        "memory_mb",
                        0.0
                    ),

                "status":
                    "INITIALIZING"
            }

        # -----------------------------------------------------
        # Current and previous metrics
        # -----------------------------------------------------

        curr_metrics = current_record[
            "raw_metrics"
        ]

        prev_metrics = self.prev_record[
            "raw_metrics"
        ]

        # -----------------------------------------------------
        # Time difference
        # -----------------------------------------------------

        delta_t = max(

            current_record["timestamp"]
            -
            self.prev_record["timestamp"],

            self.epsilon
        )

        # -----------------------------------------------------
        # Coverage gain
        # -----------------------------------------------------

        delta_cov = max(

            curr_metrics["total_coverage"]
            -
            prev_metrics["total_coverage"],

            0.0
        )

        # -----------------------------------------------------
        # Crash gain
        # -----------------------------------------------------

        delta_crashes = max(

            curr_metrics["total_crashes"]
            -
            prev_metrics["total_crashes"],

            0
        )

        # -----------------------------------------------------
        # Resources
        # -----------------------------------------------------

        cpu_utilisation = curr_metrics.get(
            "cpu_percent",
            0.0
        )

        memory_usage = curr_metrics.get(
            "memory_mb",
            0.0
        )

        # -----------------------------------------------------
        # Resource cost
        #
        # R_i =
        # T_i + lambda_U U_i + lambda_M M_i + epsilon
        # -----------------------------------------------------

        resource_cost = (

            delta_t

            +

            self.lambda_u
            * cpu_utilisation

            +

            self.lambda_m
            * memory_usage

            +

            self.epsilon
        )

        # -----------------------------------------------------
        # Coverage efficiency
        # -----------------------------------------------------

        coverage_efficiency = (

            delta_cov /
            resource_cost
        )

        # -----------------------------------------------------
        # Crash efficiency
        # -----------------------------------------------------

        crash_efficiency = (

            delta_crashes /
            resource_cost
        )

        # -----------------------------------------------------
        # CES
        # -----------------------------------------------------

        ces = (

            self.alpha
            * coverage_efficiency

            +

            self.beta
            * crash_efficiency
        )

        # -----------------------------------------------------
        # Status
        # -----------------------------------------------------

        if ces >= self.threshold_ces:

            status = "HEALTHY"

        else:

            status = "LOW_EFFICIENCY"

        # -----------------------------------------------------
        # Save current window
        # -----------------------------------------------------

        self.prev_record = current_record

        return {

            "window_id":
                current_record.get(
                    "window_id",
                    0
                ),

            "delta_time":
                round(
                    delta_t,
                    2
                ),

            "delta_cov":
                round(
                    delta_cov,
                    4
                ),

            "delta_crashes":
                delta_crashes,

            "resource_cost":
                round(
                    resource_cost,
                    4
                ),

            "CE":
                round(
                    coverage_efficiency,
                    6
                ),

            "CrE":
                round(
                    crash_efficiency,
                    6
                ),

            "CES":
                round(
                    ces,
                    6
                ),

            "cpu_percent":
                cpu_utilisation,

            "memory_mb":
                memory_usage,

            "status":
                status
        }


# -------------------------------------------------------------
# Direct test
# -------------------------------------------------------------

if __name__ == "__main__":

    analyzer = CostEffectivenessAnalyzer(
        alpha=0.5,
        beta=0.5,
        lambda_u=0.01,
        lambda_m=0.001,
        threshold_ces=0.001
    )

    records = [

        {
            "window_id": 1,
            "timestamp": 100.0,
            "raw_metrics": {
                "total_coverage": 10.0,
                "total_crashes": 0,
                "cpu_percent": 20.0,
                "memory_mb": 35.0
            }
        },

        {
            "window_id": 2,
            "timestamp": 110.0,
            "raw_metrics": {
                "total_coverage": 15.0,
                "total_crashes": 0,
                "cpu_percent": 22.0,
                "memory_mb": 36.0
            }
        },

        {
            "window_id": 3,
            "timestamp": 120.0,
            "raw_metrics": {
                "total_coverage": 15.0,
                "total_crashes": 1,
                "cpu_percent": 25.0,
                "memory_mb": 38.0
            }
        }
    ]

    for record in records:

        print(
            analyzer.compute_ces(record)
        )
