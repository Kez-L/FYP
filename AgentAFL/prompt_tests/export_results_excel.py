"""
export_results_excel.py — collect every run's summary metrics and cumulative
new-edge timeline from a results tree (default: results3/) into one Excel
workbook per seed format (xml, ical, ...).

Expected layout (as written by run_build_context.py):

    <results>/<fmt>/<section>/<section>_run_<N>_<model>/summary_<stamp>.json
    <results>/<fmt>/<section>/<section>_run_<N>_<model>/seeds.jsonl

Per workbook:
  - "Summary" sheet: an overview table (one row per section, mean/median of the
    key metrics across that section's runs), then one block per section with
    one row per run plus Mean / Median formula rows, and a chart comparing the
    mean cumulative curves of all sections.
  - One sheet per section: seed_index | cumulative new edges per run | Mean,
    with a scatter chart (one coloured marker series per run + smoothed mean
    line).

Cumulative curve: walk seeds in generation order (seed_index) and count each
new edge only at the first seed that hit it — if seed 3 and seed 7 find the
same new edge it is credited at seed 3 only. The final value therefore equals
the summary's new_edges_total_batch (PLAN.md Sec 3 primary metric).

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
from pathlib import Path

import xlsxwriter

HERE = Path(__file__).resolve().parent

# (column header, getter from summary dict)
RUN_METRICS = [
    ("seeds_generated",                lambda s: s.get("seeds_generated")),
    ("seeds_with_new_coverage",        lambda s: s.get("coverage", {}).get("seeds_with_new_coverage")),
    ("distinct_new_edge_contributors", lambda s: s.get("coverage", {}).get("distinct_new_edge_contributors")),
    ("distinct_new_edge_sets",         lambda s: s.get("coverage", {}).get("distinct_new_edge_sets")),
    ("new_edges_total_batch",          lambda s: s.get("coverage", {}).get("new_edges_total_batch")),
    ("new_coverage_rate",              lambda s: s.get("coverage", {}).get("new_coverage_rate")),
    ("calls",                          lambda s: s.get("calls")),
    ("input_tokens_total",             lambda s: s.get("input_tokens_total")),
    ("output_tokens_total",            lambda s: s.get("output_tokens_total")),
]

# Distinct, colour-blind-friendly-ish palette for run series.
RUN_COLOURS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]
MEAN_COLOUR = "#000000"


# ---------------------------------------------------------------- collection

def run_number(dir_name):
    m = re.search(r"_run_(\d+)", dir_name)
    return int(m.group(1)) if m else sys.maxsize


def load_run(run_dir, section):
    summaries = sorted(run_dir.glob("summary_*.json"))
    if not summaries:
        return None
    summary = json.loads(summaries[-1].read_text())  # newest stamp sorts last

    seeds = []
    with open(run_dir / "seeds.jsonl") as fh:
        for line in fh:
            if line.strip():
                seeds.append(json.loads(line))
    seeds.sort(key=lambda r: r["seed_index"])

    seen = set()
    cumulative = []  # list of (seed_index, cumulative distinct new edges)
    for r in seeds:
        seen.update(r.get("new_edge_ids") or [])
        cumulative.append((r["seed_index"], len(seen)))

    label = run_dir.name
    if label.startswith(section + "_"):
        label = label[len(section) + 1:]

    expected = summary.get("coverage", {}).get("new_edges_total_batch")
    if expected is not None and cumulative and cumulative[-1][1] != expected:
        print(f"WARNING: {run_dir}: cumulative ends at {cumulative[-1][1]} "
              f"but summary new_edges_total_batch = {expected}", file=sys.stderr)

    return {
        "label": label,
        "dir": run_dir,
        "summary": summary,
        "cumulative": cumulative,
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
                if run:
                    runs.append(run)
        if runs:
            sections[section] = runs
    return sections


def mean_curve(runs):
    """Mean cumulative value per seed_index, over the runs that reached it."""
    by_index = {}
    for run in runs:
        for idx, val in run["cumulative"]:
            by_index.setdefault(idx, []).append(val)
    return [(idx, statistics.mean(vals)) for idx, vals in sorted(by_index.items())]


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


def write_section_sheet(wb, fmts, name, section, runs):
    """Timeline sheet + chart. Returns (sheet name, first row, last row) of the Mean column."""
    ws = wb.add_worksheet(name)
    ws.write(0, 0, "seed_index", fmts["header"])
    for c, run in enumerate(runs, start=1):
        ws.write(0, c, run["label"], fmts["header"])
    mean_col = len(runs) + 1
    ws.write(0, mean_col, "Mean", fmts["header"])

    means = mean_curve(runs)
    row_of = {}
    for r, (idx, m) in enumerate(means, start=1):
        row_of[idx] = r
        ws.write_number(r, 0, idx)
        ws.write_number(r, mean_col, m, fmts["dec2"])
    for c, run in enumerate(runs, start=1):
        for idx, val in run["cumulative"]:
            ws.write_number(row_of[idx], c, val)
    last_row = len(means)
    ws.set_column(0, 0, 11)
    ws.set_column(1, mean_col, max(12, max(len(r["label"]) for r in runs) + 2))
    ws.freeze_panes(1, 1)

    chart = wb.add_chart({"type": "scatter"})
    for c, run in enumerate(runs, start=1):
        n = len(run["cumulative"])
        colour = RUN_COLOURS[(c - 1) % len(RUN_COLOURS)]
        chart.add_series({
            "name":       [name, 0, c],
            "categories": [name, 1, 0, n, 0],
            "values":     [name, 1, c, n, c],
            "marker":     {"type": "circle", "size": 5,
                           "fill": {"color": colour}, "border": {"color": colour}},
            "line":       {"none": True},
        })
    chart.add_series({
        "name":       [name, 0, mean_col],
        "categories": [name, 1, 0, last_row, 0],
        "values":     [name, 1, mean_col, last_row, mean_col],
        "marker":     {"type": "none"},
        "line":       {"color": MEAN_COLOUR, "width": 2.25},
        "smooth":     True,
    })
    chart.set_title({"name": f"{section} — cumulative distinct new edges"})
    chart.set_x_axis({"name": "Seed number (seed_index)", "min": 0,
                      "major_gridlines": {"visible": True, "line": {"color": "#e0e0e0"}}})
    chart.set_y_axis({"name": "Total new edges found so far", "min": 0,
                      "major_gridlines": {"visible": True, "line": {"color": "#e0e0e0"}}})
    chart.set_legend({"position": "bottom"})
    chart.set_size({"width": 820, "height": 460})
    ws.insert_chart(1, mean_col + 2, chart)
    return name, last_row


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
            vals = [v for v in (get(r["summary"]) for r in runs) if isinstance(v, (int, float))]
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
                v = get(s)
                if isinstance(v, (int, float)):
                    ws.write_number(row, c, v)
            row += 1
        last = row - 1
        for label, fn, py in (("Mean", "AVERAGE", statistics.mean),
                              ("Median", "MEDIAN", statistics.median)):
            ws.write(row, 0, label, fmts["bold"])
            for c, (_, get) in enumerate(RUN_METRICS, start=first_metric_col):
                vals = [v for v in (get(r["summary"]) for r in runs) if isinstance(v, (int, float))]
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

    # --- comparison chart of every section's mean curve
    chart = wb.add_chart({"type": "scatter"})
    for i, (section, (name, last_row)) in enumerate(sheet_refs.items()):
        mean_col = len(sections[section]) + 1
        chart.add_series({
            "name":       section,
            "categories": [name, 1, 0, last_row, 0],
            "values":     [name, 1, mean_col, last_row, mean_col],
            "marker":     {"type": "none"},
            "line":       {"color": RUN_COLOURS[i % len(RUN_COLOURS)], "width": 2},
            "smooth":     True,
        })
    chart.set_title({"name": f"{fmt} — mean cumulative new edges by section"})
    chart.set_x_axis({"name": "Seed number (seed_index)", "min": 0})
    chart.set_y_axis({"name": "Total new edges found so far", "min": 0,
                      "major_gridlines": {"visible": True, "line": {"color": "#e0e0e0"}}})
    chart.set_legend({"position": "right"})
    chart.set_size({"width": 900, "height": 480})
    ws.insert_chart(overview_end + 1, 2 + 2 * len(metric_names) + 1, chart)


def build_workbook(fmt, sections, out_path):
    wb = xlsxwriter.Workbook(str(out_path))
    fmts = {
        "title":     wb.add_format({"bold": True, "font_size": 12}),
        "header":    wb.add_format({"bold": True, "bg_color": "#dde6f0", "border": 1, "text_wrap": True}),
        "bold":      wb.add_format({"bold": True}),
        "dec2":      wb.add_format({"num_format": "0.00"}),
        "bold_dec2": wb.add_format({"bold": True, "num_format": "0.00"}),
    }
    summary_ws = wb.add_worksheet("Summary")  # first tab
    used = {"summary"}
    sheet_refs = {}
    for section, runs in sections.items():
        sheet_refs[section] = write_section_sheet(wb, fmts, sheet_name(section, used), section, runs)
    write_summary_sheet(wb, fmts, summary_ws, fmt, sections, sheet_refs)
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
            finals = ", ".join(f"{r['label']}={r['cumulative'][-1][1]}" for r in runs)
            print(f"    {section}: {finals}")


if __name__ == "__main__":
    main()
