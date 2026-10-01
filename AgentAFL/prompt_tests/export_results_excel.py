"""
export_results_excel.py — collect every run's summary metrics and per-seed
timelines from a results tree (default: results3/) into one Excel workbook per
seed format (xml, ical, ...).

Expected layout (as written by run_build_context.py):

    <results>/<fmt>/<section>/<section>_run_<N>_<model>/summary_<stamp>.json
    <results>/<fmt>/<section>/<section>_run_<N>_<model>/seeds.jsonl

Per workbook:
  - "Summary" sheet: an overview table (one row per section, mean/median of the
    key metrics across that section's runs), then one block per section with
    one row per run plus Mean / Median formula rows, and one chart per series
    comparing the section mean curves.
  - One sheet per section: seed_number, then one block per series holding one
    column per run plus a Mean column, each with a scatter chart (coloured
    marker series per run + mean line).
  - "Diagnostics" sheet: one row per run recording short runs, index gaps and
    the redundancy tallies, so missing/extra seeds are visible rather than
    silently distorting the means.

Series, all cumulative over seeds walked in generation order (seed_index):

  cum_new_edges           distinct new edges (vs the AFL++ baseline) so far,
                          each edge credited at the FIRST seed that hit it, so
                          the final value equals summary new_edges_total_batch.
  cum_redundant           redundant seeds so far.
  frac_redundant          redundant / (redundant + non-redundant) so far, i.e.
                          over seeds that produced any coverage at all.
  frac_non_redundant_all  non-redundant / all seeds so far, including seeds
                          that produced no coverage at all.

Redundancy is judged on a seed's FULL edge set against the earlier seeds of the
same run, independent of the baseline: a seed is non-redundant when it hits at
least one edge no earlier seed in the run hit, redundant when it has coverage
but every edge was already seen. Seeds with no coverage at all (edge_ids == [])
are neither, and are excluded from frac_redundant but included in
frac_non_redundant_all. new_edge_ids cannot express this rule — batch_harness
sets it to [] by construction for every "BAD (redundant)" seed — so edge_ids is
used, which is always populated.

Runs end at different lengths (a call that should emit 5 seeds sometimes emits
4). Each run's curve is forward-filled with its final value out to the longest
run in the section and the mean is always taken over every run, so a shrinking
denominator can never pull the mean down at the tail. Short runs are listed on
the Diagnostics sheet and warned about on stderr.

Read-only on the results tree; only writes the .xlsx files under --out.

Usage:
    .venv/bin/python prompt_tests/export_results_excel.py
    .venv/bin/python prompt_tests/export_results_excel.py --results prompt_tests/results3 --formats xml
"""

import argparse
import json
import re
import statistics
import sys
from collections import Counter, namedtuple
from pathlib import Path

import xlsxwriter

HERE = Path(__file__).resolve().parent

SeriesSpec = namedtuple("SeriesSpec", "key title y_label val_fmt mean_fmt y_max")

# The per-seed cumulative series, charted on every section sheet and compared
# across sections on the Summary sheet.
SERIES_SPECS = [
    SeriesSpec("cum_new_edges",
               "cumulative distinct new edges",
               "Total new edges found so far",
               None, "dec2", None),
    SeriesSpec("cum_redundant",
               "cumulative redundant seeds",
               "Redundant seeds so far",
               None, "dec2", None),
    SeriesSpec("frac_redundant",
               "redundant share of seeds with coverage",
               "Redundant / (redundant + non-redundant)",
               "dec3", "dec3", 1.0),
    SeriesSpec("frac_non_redundant_all",
               "non-redundant share of all seeds",
               "Non-redundant / all seeds so far",
               "dec3", "dec3", 1.0),
]
SERIES_KEYS = [s.key for s in SERIES_SPECS]

# (column header, getter from the loaded run dict)
RUN_METRICS = [
    ("seeds_generated",                lambda r: r["summary"].get("seeds_generated")),
    ("seeds_loaded",                   lambda r: r["health"]["n_seeds"]),
    ("seeds_with_new_coverage",        lambda r: r["summary"].get("coverage", {}).get("seeds_with_new_coverage")),
    ("distinct_new_edge_contributors", lambda r: r["summary"].get("coverage", {}).get("distinct_new_edge_contributors")),
    ("distinct_new_edge_sets",         lambda r: r["summary"].get("coverage", {}).get("distinct_new_edge_sets")),
    ("new_edges_total_batch",          lambda r: r["summary"].get("coverage", {}).get("new_edges_total_batch")),
    ("new_coverage_rate",              lambda r: r["summary"].get("coverage", {}).get("new_coverage_rate")),
    ("no_coverage_seeds",              lambda r: r["health"]["n_no_coverage"]),
    ("redundant_seeds",                lambda r: r["health"]["n_redundant"]),
    ("non_redundant_seeds",            lambda r: r["health"]["n_non_redundant"]),
    ("final_frac_redundant",           lambda r: r["health"]["final_frac_redundant"]),
    ("final_frac_non_redundant_all",   lambda r: r["health"]["final_frac_non_redundant_all"]),
    ("calls",                          lambda r: r["summary"].get("calls")),
    ("input_tokens_total",             lambda r: r["summary"].get("input_tokens_total")),
    ("output_tokens_total",            lambda r: r["summary"].get("output_tokens_total")),
]

# Distinct, colour-blind-friendly-ish palette for run series.
RUN_COLOURS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]
MEAN_COLOUR = "#000000"

DATA_ROW0 = 2  # row 0 = block titles, row 1 = column headers, data starts here


# ---------------------------------------------------------------- collection

def run_number(dir_name):
    m = re.search(r"_run_(\d+)", dir_name)
    return int(m.group(1)) if m else sys.maxsize


def read_seed_rows(path):
    """Parse seeds.jsonl, tolerating a torn final line from a run in progress."""
    rows, unparsable = [], 0
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                unparsable += 1
    rows.sort(key=lambda r: r["seed_index"])
    return rows, unparsable


def build_series(seeds):
    """Walk seeds in generation order, returning the cumulative series + tallies.

    A seed is non-redundant when its edge set contains an edge no earlier seed
    in this run hit; redundant when it has coverage but adds nothing new; and
    neither when it has no coverage at all.
    """
    series = {k: [] for k in SERIES_KEYS}
    seen_new, union = set(), set()
    n_red = n_non_red = n_no_cov = 0

    for r in seeds:
        seen_new.update(r.get("new_edge_ids") or [])
        edges = set(r.get("edge_ids") or [])
        if not edges:
            n_no_cov += 1
        elif edges - union:
            n_non_red += 1
        else:
            n_red += 1
        union |= edges

        n_total = len(series["cum_new_edges"]) + 1  # seeds walked so far
        n_covered = n_red + n_non_red
        series["cum_new_edges"].append(len(seen_new))
        series["cum_redundant"].append(n_red)
        series["frac_redundant"].append(n_red / n_covered if n_covered else None)
        series["frac_non_redundant_all"].append(n_non_red / n_total)

    tallies = {
        "n_seeds": len(seeds),
        "n_no_coverage": n_no_cov,
        "n_redundant": n_red,
        "n_non_redundant": n_non_red,
        "final_frac_redundant": series["frac_redundant"][-1] if seeds else None,
        "final_frac_non_redundant_all": series["frac_non_redundant_all"][-1] if seeds else None,
    }
    return series, tallies


def load_run(run_dir, section):
    summaries = sorted(run_dir.glob("summary_*.json"))
    if not summaries:
        return None
    summary = json.loads(summaries[-1].read_text())  # newest stamp sorts last

    seeds, unparsable = read_seed_rows(run_dir / "seeds.jsonl")
    series, tallies = build_series(seeds)

    indices = [r["seed_index"] for r in seeds]
    counts = Counter(indices)
    health = dict(tallies)
    health.update({
        "max_seed_index": indices[-1] if indices else None,
        "missing_indices": sorted(set(range(indices[-1] + 1)) - set(indices)) if indices else [],
        "duplicate_indices": sorted(i for i, n in counts.items() if n > 1),
        "unparsable_lines": unparsable,
    })

    label = run_dir.name
    if label.startswith(section + "_"):
        label = label[len(section) + 1:]

    expected = summary.get("coverage", {}).get("new_edges_total_batch")
    final = series["cum_new_edges"][-1] if seeds else None
    health["new_edges_total_batch"] = expected
    health["cumulative_matches_summary"] = (expected is None or final == expected)
    if not health["cumulative_matches_summary"]:
        print(f"WARNING: {run_dir}: cumulative ends at {final} "
              f"but summary new_edges_total_batch = {expected}", file=sys.stderr)
    if unparsable:
        print(f"WARNING: {run_dir}: skipped {unparsable} unparsable seeds.jsonl "
              f"line(s) (run still being written?)", file=sys.stderr)

    return {
        "label": label,
        "dir": run_dir,
        "section": section,
        "summary": summary,
        "series": series,
        "health": health,
    }


def collect_format(fmt_dir):
    """Return {section: [run, ...]} for one format directory."""
    sections = {}
    for section_dir in sorted(p for p in fmt_dir.iterdir() if p.is_dir()):
        section = section_dir.name
        runs = []
        for run_dir in sorted(section_dir.iterdir(), key=lambda p: (run_number(p.name), p.name)):
            if run_dir.is_dir() and (run_dir / "seeds.jsonl").is_file():
                run = load_run(run_dir, section)
                if run and run["health"]["n_seeds"]:
                    runs.append(run)
        if runs:
            sections[section] = runs
    return sections


def aligned_series(runs, key):
    """Forward-fill every run to the longest length, then mean over ALL runs.

    A cumulative curve is monotone, so holding a short run's final value is the
    correct extension: the run produced no further seeds, so it found nothing
    further. Ratios hold their final value for the same reason — recomputing
    them past a run's end would make the denominator keep growing and drag the
    curve down. Averaging only over "the runs that reached this index" is what
    produced the spurious cliff at the right-hand edge of the old charts.
    """
    n = max(len(r["series"][key]) for r in runs)
    filled = []
    for run in runs:
        vals = list(run["series"][key])
        vals += [vals[-1]] * (n - len(vals)) if vals else [None] * n
        filled.append(vals)
    means = []
    for i in range(n):
        col = [v[i] for v in filled if v[i] is not None]
        means.append(statistics.mean(col) if col else None)
    return n, filled, means


# ---------------------------------------------------------------- workbook

def sheet_name(section, used):
    name = re.sub(r"[\[\]:*?/\\]", "_", section)[:31]
    base, n = name, 2
    while name.lower() in used:
        suffix = f"_{n}"
        name = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(name.lower())
    return name


def series_chart(wb, spec, title, sheet, n_rows, run_names, first_col, mean_col, colours):
    """Scatter chart: one marker series per run + a mean line."""
    last_row = DATA_ROW0 + n_rows - 1
    chart = wb.add_chart({"type": "scatter"})
    for i, run_name in enumerate(run_names):
        col = first_col + i
        colour = colours[i % len(colours)]
        chart.add_series({
            "name":       run_name,
            "categories": [sheet, DATA_ROW0, 0, last_row, 0],
            "values":     [sheet, DATA_ROW0, col, last_row, col],
            "marker":     {"type": "circle", "size": 5,
                           "fill": {"color": colour}, "border": {"color": colour}},
            "line":       {"none": True},
        })
    chart.add_series({
        "name":       "Mean",
        "categories": [sheet, DATA_ROW0, 0, last_row, 0],
        "values":     [sheet, DATA_ROW0, mean_col, last_row, mean_col],
        "marker":     {"type": "none"},
        "line":       {"color": MEAN_COLOUR, "width": 2.25},
    })
    chart.set_title({"name": title})
    chart.set_x_axis({"name": "Seed number (1 = first seed generated)", "min": 1,
                      "major_gridlines": {"visible": True, "line": {"color": "#e0e0e0"}}})
    y_axis = {"name": spec.y_label, "min": 0,
              "major_gridlines": {"visible": True, "line": {"color": "#e0e0e0"}}}
    if spec.y_max is not None:
        y_axis["max"] = spec.y_max
    chart.set_y_axis(y_axis)
    chart.set_legend({"position": "bottom"})
    chart.set_size({"width": 820, "height": 460})
    return chart


def write_section_sheet(wb, fmts, name, section, runs):
    """Timeline sheet: one block of columns per series + a chart each.

    Returns {series key: (sheet, n_rows, mean_col)} for the Summary charts.
    """
    ws = wb.add_worksheet(name)
    ws.write(1, 0, "seed_number", fmts["header"])

    n_rows = max(len(r["series"][SERIES_KEYS[0]]) for r in runs)
    for i in range(n_rows):
        ws.write_number(DATA_ROW0 + i, 0, i + 1)

    refs = {}
    col = 1
    for spec in SERIES_SPECS:
        n, filled, means = aligned_series(runs, spec.key)
        ws.write(0, col, spec.title, fmts["title"])
        for c, run in enumerate(runs):
            ws.write(1, col + c, run["label"], fmts["header"])
        mean_col = col + len(runs)
        ws.write(1, mean_col, "Mean", fmts["header"])

        val_fmt = fmts[spec.val_fmt] if spec.val_fmt else None
        mean_fmt = fmts["bold_" + spec.mean_fmt]
        for c, vals in enumerate(filled):
            for i, v in enumerate(vals):
                if v is not None:
                    ws.write_number(DATA_ROW0 + i, col + c, v, val_fmt)
        for i, m in enumerate(means):
            if m is not None:
                ws.write_number(DATA_ROW0 + i, mean_col, m, mean_fmt)

        refs[spec.key] = (name, n, mean_col)
        col = mean_col + 2  # blank spacer column between blocks

    ws.set_column(0, 0, 13)
    ws.set_column(1, col, max(12, max(len(r["label"]) for r in runs) + 2))
    ws.freeze_panes(DATA_ROW0, 1)

    run_names = [r["label"] for r in runs]
    for i, spec in enumerate(SERIES_SPECS):
        _, n, mean_col = refs[spec.key]
        first_col = mean_col - len(runs)
        chart = series_chart(wb, spec, f"{section} — {spec.title}", name, n,
                             run_names, first_col, mean_col, RUN_COLOURS)
        ws.insert_chart(1 + i * 24, col + 1, chart)
    return refs


def write_summary_sheet(wb, fmts, ws, fmt, sections, sheet_refs):
    metric_names = [m[0] for m in RUN_METRICS]

    # --- overview: one row per section, mean & median of each metric
    row = 0
    ws.write(row, 0, f"{fmt} — section overview (mean / median across runs)", fmts["title"])
    row += 1
    ws.write(row, 0, "section", fmts["header"])
    ws.write(row, 1, "n_runs", fmts["header"])
    col = 2
    for m in metric_names:
        ws.write(row, col, f"{m} mean", fmts["header"])
        ws.write(row, col + 1, f"{m} median", fmts["header"])
        col += 2
    row += 1
    for section, runs in sections.items():
        ws.write(row, 0, section, fmts["bold"])
        ws.write_number(row, 1, len(runs))
        col = 2
        for _, get in RUN_METRICS:
            vals = [v for v in (get(r) for r in runs) if isinstance(v, (int, float))]
            if vals:
                ws.write_number(row, col, statistics.mean(vals), fmts["dec2"])
                ws.write_number(row, col + 1, statistics.median(vals), fmts["dec2"])
            col += 2
        row += 1
    overview_end = row

    # --- per-section blocks: one row per run + Mean / Median formula rows
    row += 2
    headers = ["run", "model", "run_stamp"] + metric_names
    first_metric_col = 3
    for section, runs in sections.items():
        ws.write(row, 0, section, fmts["title"])
        row += 1
        for c, h in enumerate(headers):
            ws.write(row, c, h, fmts["header"])
        row += 1
        first = row
        for run in runs:
            s = run["summary"]
            ws.write(row, 0, run["label"])
            ws.write(row, 1, s.get("model", ""))
            ws.write(row, 2, s.get("run_stamp", ""))
            for c, (_, get) in enumerate(RUN_METRICS, start=first_metric_col):
                v = get(run)
                if isinstance(v, (int, float)):
                    ws.write_number(row, c, v)
            row += 1
        last = row - 1
        for label, fn, py in (("Mean", "AVERAGE", statistics.mean),
                              ("Median", "MEDIAN", statistics.median)):
            ws.write(row, 0, label, fmts["bold"])
            for c, (_, get) in enumerate(RUN_METRICS, start=first_metric_col):
                vals = [v for v in (get(r) for r in runs) if isinstance(v, (int, float))]
                if not vals:
                    continue
                rng = (xlsxwriter.utility.xl_rowcol_to_cell(first, c) + ":" +
                       xlsxwriter.utility.xl_rowcol_to_cell(last, c))
                ws.write_formula(row, c, f"={fn}({rng})", fmts["bold_dec2"], py(vals))
            row += 1
        row += 1

    ws.set_column(0, 0, 28)
    ws.set_column(1, 2, 18)
    ws.set_column(3, 2 + 2 * len(metric_names), 14)

    # --- one cross-section comparison chart per series, of the section means
    chart_col = 2 + 2 * len(metric_names) + 1
    for i, spec in enumerate(SERIES_SPECS):
        chart = wb.add_chart({"type": "scatter"})
        for j, section in enumerate(sheet_refs):
            name, n, mean_col = sheet_refs[section][spec.key]
            last_row = DATA_ROW0 + n - 1
            chart.add_series({
                "name":       section,
                "categories": [name, DATA_ROW0, 0, last_row, 0],
                "values":     [name, DATA_ROW0, mean_col, last_row, mean_col],
                "marker":     {"type": "none"},
                "line":       {"color": RUN_COLOURS[j % len(RUN_COLOURS)], "width": 2},
            })
        chart.set_title({"name": f"{fmt} — mean {spec.title} by section"})
        chart.set_x_axis({"name": "Seed number (1 = first seed generated)", "min": 1})
        y_axis = {"name": spec.y_label, "min": 0,
                  "major_gridlines": {"visible": True, "line": {"color": "#e0e0e0"}}}
        if spec.y_max is not None:
            y_axis["max"] = spec.y_max
        chart.set_y_axis(y_axis)
        chart.set_legend({"position": "right"})
        chart.set_size({"width": 900, "height": 480})
        ws.insert_chart(overview_end + 1 + i * 25, chart_col, chart)


DIAG_COLUMNS = [
    ("section",                lambda r, longest: r["section"]),
    ("run",                    lambda r, longest: r["label"]),
    ("model",                  lambda r, longest: r["summary"].get("model", "")),
    ("run_stamp",              lambda r, longest: r["summary"].get("run_stamp", "")),
    ("seeds_expected",         lambda r, longest: r["summary"].get("seeds_expected")),
    ("seeds_generated",        lambda r, longest: r["summary"].get("seeds_generated")),
    ("seeds_loaded",           lambda r, longest: r["health"]["n_seeds"]),
    ("max_seed_index",         lambda r, longest: r["health"]["max_seed_index"]),
    ("section_longest_run",    lambda r, longest: longest),
    ("short_by",               lambda r, longest: longest - r["health"]["n_seeds"]),
    ("forward_filled_from",    lambda r, longest: (r["health"]["n_seeds"] + 1
                                                   if r["health"]["n_seeds"] < longest else "")),
    ("missing_indices",        lambda r, longest: ",".join(map(str, r["health"]["missing_indices"]))),
    ("duplicate_indices",      lambda r, longest: ",".join(map(str, r["health"]["duplicate_indices"]))),
    ("unparsable_lines",       lambda r, longest: r["health"]["unparsable_lines"]),
    ("no_coverage_seeds",      lambda r, longest: r["health"]["n_no_coverage"]),
    ("redundant_seeds",        lambda r, longest: r["health"]["n_redundant"]),
    ("non_redundant_seeds",    lambda r, longest: r["health"]["n_non_redundant"]),
    ("final_new_edges",        lambda r, longest: r["series"]["cum_new_edges"][-1]),
    ("new_edges_total_batch",  lambda r, longest: r["health"]["new_edges_total_batch"]),
    ("cumulative_matches",     lambda r, longest: "yes" if r["health"]["cumulative_matches_summary"] else "NO"),
]


def write_diagnostics_sheet(wb, fmts, ws, fmt, sections):
    """One row per run recording short runs, index gaps and redundancy tallies."""
    ws.write(0, 0, f"{fmt} — run health (short runs are forward-filled to the "
                   f"section's longest run before averaging)", fmts["title"])
    for c, (h, _) in enumerate(DIAG_COLUMNS):
        ws.write(1, c, h, fmts["header"])
    row = 2
    for section, runs in sections.items():
        longest = max(r["health"]["n_seeds"] for r in runs)
        for run in runs:
            flagged = (run["health"]["n_seeds"] < longest
                       or run["health"]["missing_indices"]
                       or run["health"]["duplicate_indices"]
                       or run["health"]["unparsable_lines"]
                       or not run["health"]["cumulative_matches_summary"])
            cell_fmt = fmts["warn"] if flagged else None
            for c, (_, get) in enumerate(DIAG_COLUMNS):
                v = get(run, longest)
                if isinstance(v, (int, float)):
                    ws.write_number(row, c, v, cell_fmt)
                else:
                    ws.write(row, c, v, cell_fmt)
            row += 1
    ws.set_column(0, 0, 28)
    ws.set_column(1, 3, 18)
    ws.set_column(4, len(DIAG_COLUMNS) - 1, 15)
    ws.freeze_panes(2, 2)


def build_workbook(fmt, sections, out_path):
    wb = xlsxwriter.Workbook(str(out_path))
    fmts = {
        "title":     wb.add_format({"bold": True, "font_size": 12}),
        "header":    wb.add_format({"bold": True, "bg_color": "#dde6f0", "border": 1, "text_wrap": True}),
        "bold":      wb.add_format({"bold": True}),
        "warn":      wb.add_format({"bg_color": "#fdebd0"}),
        "dec2":      wb.add_format({"num_format": "0.00"}),
        "dec3":      wb.add_format({"num_format": "0.000"}),
        "bold_dec2": wb.add_format({"bold": True, "num_format": "0.00"}),
        "bold_dec3": wb.add_format({"bold": True, "num_format": "0.000"}),
    }
    summary_ws = wb.add_worksheet("Summary")        # first tab
    diagnostics_ws = wb.add_worksheet("Diagnostics")
    used = {"summary", "diagnostics"}
    sheet_refs = {}
    for section, runs in sections.items():
        sheet_refs[section] = write_section_sheet(wb, fmts, sheet_name(section, used), section, runs)
    write_summary_sheet(wb, fmts, summary_ws, fmt, sections, sheet_refs)
    write_diagnostics_sheet(wb, fmts, diagnostics_ws, fmt, sections)
    wb.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", type=Path, default=HERE / "results3")
    ap.add_argument("--out", type=Path, default=HERE / "analysis" / "excel")
    ap.add_argument("--formats", nargs="*", help="format subdirs to export (default: all found)")
    args = ap.parse_args()

    results = args.results.resolve()
    fmts = args.formats or sorted(p.name for p in results.iterdir() if p.is_dir())
    args.out.mkdir(parents=True, exist_ok=True)

    for fmt in fmts:
        fmt_dir = results / fmt
        if not fmt_dir.is_dir():
            print(f"skip {fmt}: {fmt_dir} not found", file=sys.stderr)
            continue
        sections = collect_format(fmt_dir)
        if not sections:
            print(f"skip {fmt}: no runs found", file=sys.stderr)
            continue
        out_path = args.out / f"{results.name}_{fmt}.xlsx"
        build_workbook(fmt, sections, out_path)
        n_runs = sum(len(r) for r in sections.values())
        print(f"{fmt}: {len(sections)} sections, {n_runs} runs -> {out_path}")
        for section, runs in sections.items():
            longest = max(r["health"]["n_seeds"] for r in runs)
            finals = ", ".join(f"{r['label']}={r['series']['cum_new_edges'][-1]}" for r in runs)
            print(f"    {section}: {finals}")
            for run in runs:
                n = run["health"]["n_seeds"]
                if n < longest:
                    print(f"WARNING: {fmt}/{section}/{run['label']}: {n} seeds vs "
                          f"{longest} in the longest run; forward-filled from seed "
                          f"{n + 1}", file=sys.stderr)


if __name__ == "__main__":
    main()
