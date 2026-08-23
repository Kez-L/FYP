#!/usr/bin/env python3
"""
generate_report.py — Build a run summary + per-seed detail + lineage trace +
coverage-over-time plot from an agentafl_orchestrator.py run directory. Read-only
against the run's own artifacts and the live queue directory — safe to run while
the orchestrator is still going, and never writes back to cycles.jsonl.

Usage:
  python3 generate_report.py --run-dir /home/user/Documents/agentafl_runs/treatment1-20260806-021500

Output (written into --run-dir): report.md (human-readable), report.json /
report.csv (machine-readable, for aggregating the 3 repeats later), report.svg
(the coverage-over-time chart standalone).
"""

import argparse
import csv
import io
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import xml_utils

# Status-palette colors (from the project's dataviz reference palette) — used
# for injection-event annotations, which are state markers, not a second
# categorical series.
COLOR_LINE = "#2a78d6"     # sequential blue, single-series line
COLOR_GOOD = "#0ca30c"     # status-good: cycle injected >=1 candidate
COLOR_WARN = "#fab219"     # status-warning: cycle fired but injected 0
COLOR_GRID = "#e5e4df"
COLOR_TEXT_SECONDARY = "#52514e"
COLOR_TEXT_PRIMARY = "#0b0b0b"
COLOR_SURFACE = "#fcfcfb"


# --------------------------------------------------------------------------
# Loading run artifacts
# --------------------------------------------------------------------------

def load_run_config(run_dir: Path) -> dict:
    path = run_dir / "run_config.json"
    if not path.exists():
        raise SystemExit(f"run_config.json not found in {run_dir} — is this a valid orchestrator run_dir?")
    return json.loads(path.read_text())


def load_cycle_records(jsonl_path: Path):
    """Returns (cycles, resolutions, lifecycle) — cycles.jsonl is event-sourced
    with a 'record_type' discriminator; this splits it into the three shapes
    the rest of the report needs."""
    cycles, resolutions, lifecycle = [], [], []
    if not jsonl_path.exists():
        return cycles, resolutions, lifecycle
    for line in jsonl_path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        rt = rec.get("record_type")
        if rt == "cycle":
            cycles.append(rec)
        elif rt == "injection_resolved":
            resolutions.append(rec)
        else:
            lifecycle.append(rec)
    return cycles, resolutions, lifecycle


def _iso_to_unix(iso_str) -> float:
    if not iso_str:
        return None
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp()


def build_candidates_view(cycles: list, resolutions: list) -> list:
    """Flatten every candidate across every cycle into one list, annotated
    with its origin cycle and the resolution state as of the orchestrator's
    own 'injection_resolved' events (report_time_resolution_scan may update
    this further for late syncs)."""
    resolved_by_key = {(r["cycle_id"], r["candidate_idx"]): r for r in resolutions}
    view = []
    for cyc in cycles:
        for cand in cyc.get("candidates", []):
            row = dict(cand)
            row["cycle_id"] = cyc["cycle_id"]
            row["triggered_at"] = cyc.get("triggered_at")
            row["resolution_source"] = "n/a"
            if row.get("injected"):
                row["resolution_source"] = "unresolved"
                res = resolved_by_key.get((cyc["cycle_id"], cand["candidate_idx"]))
                if res:
                    row["main_queue_id"] = res["main_queue_id"]
                    row["main_queue_filename"] = res["main_queue_filename"]
                    row["main_queue_resolved"] = True
                    row["main_queue_resolved_at"] = res["resolved_at"]
                    row["resolution_source"] = "orchestrator"
            view.append(row)
    return view


def report_time_resolution_scan(candidates_view: list, main_queue_dir: Path) -> list:
    """One more resolution pass at report time — for candidates injected but
    not yet resolved by the orchestrator's own per-poll checks (e.g. a late
    sync after the orchestrator process exited). Strictly read-only against
    main_queue_dir; never writes back to cycles.jsonl. Mutates and returns
    candidates_view."""
    pending = [c for c in candidates_view
               if c.get("injected") and not c.get("main_queue_resolved") and c.get("addseeds_local_id")]
    if not pending:
        return candidates_view
    local_ids = {c["addseeds_local_id"] for c in pending}
    found = xml_utils.find_synced_entries_bulk(main_queue_dir, "addseeds", local_ids)
    now_iso = datetime.now().astimezone().isoformat()
    for c in pending:
        fields = found.get(c["addseeds_local_id"])
        if fields:
            c["main_queue_id"] = fields["id"]
            c["main_queue_filename"] = fields["raw"]
            c["main_queue_resolved"] = True
            c["main_queue_resolved_at"] = now_iso
            c["resolution_source"] = "report_time_scan"
    return candidates_view


# --------------------------------------------------------------------------
# (a) Summary stats / (b) per-seed table
# --------------------------------------------------------------------------

def build_summary_stats(cycles: list, candidates_view: list) -> dict:
    total_candidates = len(candidates_view)
    well_formed = sum(1 for c in candidates_view if c.get("well_formed"))
    status_breakdown = defaultdict(int)
    for c in candidates_view:
        status_breakdown[c.get("status", "?")] += 1
    good_count = status_breakdown.get("GOOD (novel coverage)", 0)
    injected = [c for c in candidates_view if c.get("injected")]
    resolved = [c for c in injected if c.get("main_queue_resolved")]

    return {
        "total_llm_calls": len(cycles),
        "total_candidates_generated": total_candidates,
        "well_formed_count": well_formed,
        "well_formed_rate": (well_formed / total_candidates) if total_candidates else 0.0,
        "usefulness_rate": (good_count / total_candidates) if total_candidates else 0.0,
        "status_breakdown": dict(status_breakdown),
        "injected_count": len(injected),
        "injected_resolved_count": len(resolved),
        "injected_unresolved_count": len(injected) - len(resolved),
        "total_new_edges_at_injection_time": sum(c.get("new_edges", 0) or 0 for c in injected),
        "crash_count_unactioned": status_breakdown.get("CRASH", 0),
        "timeout_count_unactioned": status_breakdown.get("TIMEOUT", 0),
    }


_TABLE_COLS = [
    "cycle_id", "triggered_at", "candidate_idx", "candidate_filename", "status", "well_formed",
    "new_edges", "total_edges", "targeted_constructs", "injected",
    "addseeds_local_id", "main_queue_id", "main_queue_resolved",
    "main_queue_resolved_at", "resolution_source",
]


def build_candidate_table(candidates_view: list) -> list:
    return [{k: c.get(k) for k in _TABLE_COLS} for c in candidates_view]


# --------------------------------------------------------------------------
# (c) Lineage / downstream-contribution tracing
# --------------------------------------------------------------------------

def build_queue_index(queue_dir: Path) -> dict:
    """Parse every filename in queue_dir once. Returns {id: parsed_fields}.
    The O(queue size) cost is paid once per report run, not once per seed."""
    fields_by_id = {}
    try:
        entries = list(queue_dir.iterdir())
    except OSError:
        return fields_by_id
    for f in entries:
        if not f.is_file():
            continue
        fields = xml_utils.parse_queue_filename(f.name)
        if fields["id"]:
            fields_by_id[fields["id"]] = fields
    return fields_by_id


def build_children_index(fields_by_id: dict) -> dict:
    children = defaultdict(list)
    for id_, fields in fields_by_id.items():
        for parent_id in fields["src"]:
            children[parent_id].append(id_)
    return children


def trace_lineage(children_index: dict, fields_by_id: dict, seed_main_queue_id: str,
                   max_depth=None) -> dict:
    """
    BFS from an injected seed's resolved main-queue id to find every
    descendant reachable through the queue's src: chains. "Downstream
    contribution" here is a proxy metric — count of coverage-flagged
    (+cov) descendants, not a quantified edge-count sum, since AFL queue
    filenames only carry a boolean +cov flag. An exact version would need
    re-running afl-showmap per descendant against a time-dependent baseline
    (the queue keeps growing during the run) — real complexity, why this is
    the last/optional piece per the original design's own sequencing.
    """
    if not seed_main_queue_id or seed_main_queue_id not in fields_by_id:
        return {"status": "unresolved — seed not yet observed synced into main's queue"}

    visited = {seed_main_queue_id}
    frontier = [seed_main_queue_id]
    by_depth = {}
    depth = 0
    while frontier:
        if max_depth is not None and depth >= max_depth:
            break
        depth += 1
        next_frontier = []
        for pid in frontier:
            for cid in children_index.get(pid, []):
                if cid not in visited:
                    visited.add(cid)
                    next_frontier.append(cid)
        if next_frontier:
            by_depth[depth] = next_frontier
        frontier = next_frontier

    descendants = visited - {seed_main_queue_id}
    cov_descendants = sorted(d for d in descendants if fields_by_id.get(d, {}).get("has_cov"))
    return {
        "status": "traced",
        "total_descendants": len(descendants),
        "cov_descendants": len(cov_descendants),
        "cov_descendant_ids": cov_descendants[:20],
        "max_depth_reached": depth,
        "descendants_by_depth": {k: len(v) for k, v in by_depth.items()},
    }


# --------------------------------------------------------------------------
# (d) Coverage-over-time plot — hand-rolled SVG, zero dependencies
# --------------------------------------------------------------------------

def load_coverage_points(plateau_log_path: Path, run_start_ts, run_end_ts=None) -> list:
    """Read plateau_log.csv rows within [run_start_ts, run_end_ts] (or to EOF
    if run_end_ts is None, e.g. a still-running run), returning
    [(hours_elapsed_since_run_start, best_edges), ...] sorted by time. Hours
    since run start (not wall-clock) is what makes this directly overlayable
    against a differently-scheduled baseline run's own curve at the same
    snapshot cadence."""
    points = []
    if run_start_ts is None or not plateau_log_path.exists():
        return points
    raw = plateau_log_path.read_bytes().replace(b"\x00", b"")
    lines = [l.strip() for l in raw.decode("utf-8", errors="replace").splitlines() if l.strip()]
    if len(lines) < 2:
        return points
    header = [h.strip() for h in lines[0].split(",")]
    for line in lines[1:]:
        row = dict(zip(header, line.split(",")))
        try:
            ts = int(row["timestamp"])
            edges = int(row["best_edges"])
        except (KeyError, ValueError):
            continue
        if ts < run_start_ts:
            continue
        if run_end_ts is not None and ts > run_end_ts:
            continue
        points.append(((ts - run_start_ts) / 3600.0, edges))
    points.sort(key=lambda p: p[0])
    return points


def render_coverage_svg(points: list, injection_events: list, width=960, height=320) -> str:
    """points: [(hours, edges), ...] sorted by hours.
    injection_events: [{"hours": float, "injected_count": int, "cycle_id": int}, ...].
    Single series -> no legend box (the section heading names it); injection
    events use the status palette (state, not identity) with a text label,
    never color alone. Static/light-mode only, no hover layer — the delivery
    medium (Markdown viewed via cat/GitHub/editor preview) has no reliable
    JS/CSS-media-query execution, a deliberate, documented scope-down."""
    margin_l, margin_r, margin_t, margin_b = 64, 30, 34, 40
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b

    if not points:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
                f'<rect width="{width}" height="{height}" fill="{COLOR_SURFACE}"/>'
                f'<text x="{width/2}" y="{height/2}" text-anchor="middle" '
                f'font-family="sans-serif" font-size="14" fill="{COLOR_TEXT_SECONDARY}">'
                f'No coverage data points in this run\'s time window '
                f'(check --campaign-root/plateau_log path).</text></svg>')

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_min, x_max = 0.0, max(xs[-1], 0.001)
    y_min, y_max = min(ys), max(ys)
    if y_min == y_max:
        y_min, y_max = max(0, y_min - 1), y_max + 1

    def sx(x):
        return margin_l + (x - x_min) / (x_max - x_min) * plot_w

    def sy(y):
        return margin_t + plot_h - (y - y_min) / (y_max - y_min) * plot_h

    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'font-family="sans-serif">',
           f'<rect x="0" y="0" width="{width}" height="{height}" fill="{COLOR_SURFACE}"/>']

    n_ticks = 4
    for i in range(n_ticks + 1):
        y_val = y_min + (y_max - y_min) * i / n_ticks
        yy = sy(y_val)
        svg.append(f'<line x1="{margin_l}" y1="{yy:.1f}" x2="{width - margin_r}" y2="{yy:.1f}" '
                    f'stroke="{COLOR_GRID}" stroke-width="1"/>')
        svg.append(f'<text x="{margin_l - 8}" y="{yy + 4:.1f}" text-anchor="end" '
                    f'font-size="11" fill="{COLOR_TEXT_SECONDARY}">{int(y_val):,}</text>')

    n_xticks = min(6, max(1, int(x_max)))
    for i in range(n_xticks + 1):
        x_val = x_max * i / n_xticks
        xx = sx(x_val)
        svg.append(f'<text x="{xx:.1f}" y="{height - margin_b + 16}" text-anchor="middle" '
                    f'font-size="11" fill="{COLOR_TEXT_SECONDARY}">{x_val:.0f}h</text>')

    for ev in injection_events:
        if ev["hours"] is None or ev["hours"] < x_min or ev["hours"] > x_max:
            continue
        xx = sx(ev["hours"])
        color = COLOR_GOOD if ev["injected_count"] > 0 else COLOR_WARN
        svg.append(f'<line x1="{xx:.1f}" y1="{margin_t}" x2="{xx:.1f}" y2="{height - margin_b}" '
                    f'stroke="{color}" stroke-width="1" stroke-dasharray="2,2"/>')
        svg.append(f'<circle cx="{xx:.1f}" cy="{margin_t}" r="4" fill="{color}"/>')
        svg.append(f'<text x="{xx:.1f}" y="{margin_t - 6}" text-anchor="middle" font-size="10" '
                    f'fill="{color}">c{ev["cycle_id"]}</text>')

    poly_points = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in points)
    svg.append(f'<polyline points="{poly_points}" fill="none" stroke="{COLOR_LINE}" '
                f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')

    x0, y0 = points[0]
    x1, y1 = points[-1]
    for x, y, anchor in ((x0, y0, "start"), (x1, y1, "end")):
        svg.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="4" fill="{COLOR_LINE}" '
                    f'stroke="{COLOR_SURFACE}" stroke-width="2"/>')
        svg.append(f'<text x="{sx(x):.1f}" y="{sy(y) - 10:.1f}" text-anchor="{anchor}" '
                    f'font-size="11" fill="{COLOR_TEXT_PRIMARY}">{int(y):,}</text>')

    svg.append(f'<text x="{width/2}" y="{height - 6}" text-anchor="middle" font-size="11" '
                f'fill="{COLOR_TEXT_SECONDARY}">Hours elapsed since run start</text>')
    svg.append('</svg>')
    return "\n".join(svg)


# --------------------------------------------------------------------------
# Output renderers
# --------------------------------------------------------------------------

def render_markdown(run_config, summary, candidate_table, lineage_by_seed, svg) -> str:
    lines = [f"# AgentAFL Run Report — {run_config.get('run_id', '?')}", ""]
    lines.append(f"Campaign root: `{run_config.get('campaign_root')}` "
                 f"(instance `{run_config.get('instance')}`)  ")
    lines.append(f"Model: `{run_config.get('llm_model')}`  |  Started: {run_config.get('started_at')}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- LLM calls: **{summary['total_llm_calls']}**")
    lines.append(f"- Candidates generated: **{summary['total_candidates_generated']}**")
    tc = summary["total_candidates_generated"]
    lines.append(f"- Well-formed rate: **{summary['well_formed_rate']*100:.1f}%** "
                 f"({summary['well_formed_count']}/{tc})")
    lines.append(f"- Usefulness rate (GOOD / total): **{summary['usefulness_rate']*100:.1f}%**")
    lines.append(f"- Seeds injected: **{summary['injected_count']}** "
                 f"({summary['injected_resolved_count']} confirmed synced into main's queue, "
                 f"{summary['injected_unresolved_count']} not yet observed synced)")
    lines.append(f"- Total new edges attributable to injected seeds at evaluation time: "
                 f"**{summary['total_new_edges_at_injection_time']}**")
    breakdown = ", ".join(f"{k}={v}" for k, v in summary["status_breakdown"].items())
    lines.append(f"- Status breakdown: {breakdown}")
    if summary["crash_count_unactioned"] or summary["timeout_count_unactioned"]:
        lines.append("")
        lines.append(f"> ⚠️ **{summary['crash_count_unactioned']} candidate(s) crashed and "
                     f"{summary['timeout_count_unactioned']} timed out the target — unactioned this "
                     "run (no ASan build / afl-tmin in V1). Repro files are under `candidates/` — "
                     "worth a manual look; a plain non-sanitized crash can still be a real bug.**")
    lines.append("")

    lines.append("## Coverage over time")
    lines.append("")
    lines.append(svg if svg else "*(no coverage data available for this run)*")
    lines.append("")
    lines.append("*Green marker = cycle injected ≥1 candidate. Amber = cycle fired but "
                 "injected 0. X-axis is hours elapsed since run start, not wall-clock — directly "
                 "comparable to a differently-scheduled baseline run's own `plateau_log.csv` at "
                 "the same snapshot cadence.*")
    lines.append("")

    lines.append("## Per-seed detail")
    lines.append("")
    lines.append("| Cycle | Timestamp | # | Filename | Status | Well-formed | New edges | Injected | "
                 "Main queue id | Resolution | Downstream (cov descendants) |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for row in candidate_table:
        lineage = lineage_by_seed.get(row.get("main_queue_id"), {})
        if lineage.get("status") == "traced":
            downstream = f"{lineage['cov_descendants']}/{lineage['total_descendants']}"
        else:
            downstream = lineage.get("status", "n/a")
        lines.append(
            f"| {row['cycle_id']} | {row.get('triggered_at') or '-'} | {row['candidate_idx']} | "
            f"{row['candidate_filename']} | "
            f"{row['status']} | {row['well_formed']} | {row['new_edges']} | "
            f"{'✓' if row['injected'] else ''} | {row.get('main_queue_id') or '-'} | "
            f"{row.get('resolution_source')} | {downstream} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_json(run_config, summary, candidate_table, lineage_by_seed) -> str:
    return json.dumps({
        "run_config": run_config, "summary": summary,
        "candidates": candidate_table, "lineage_by_main_queue_id": lineage_by_seed,
    }, indent=2, default=str)


def render_csv(candidate_table: list) -> str:
    if not candidate_table:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_TABLE_COLS)
    writer.writeheader()
    for row in candidate_table:
        r = dict(row)
        if isinstance(r.get("targeted_constructs"), list):
            r["targeted_constructs"] = ";".join(r["targeted_constructs"])
        writer.writerow(r)
    return buf.getvalue()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--campaign-root", type=Path, default=None,
                     help="Defaults to the value recorded in this run's run_config.json.")
    ap.add_argument("--instance", default=None,
                     help="Defaults to the value recorded in this run's run_config.json.")
    ap.add_argument("--out-prefix", default="report")
    ap.add_argument("--skip-lineage", action="store_true")
    ap.add_argument("--skip-plot", action="store_true")
    ap.add_argument("--max-lineage-depth", type=int, default=None)
    args = ap.parse_args()

    run_config = load_run_config(args.run_dir)
    campaign_root = args.campaign_root or Path(run_config["campaign_root"])
    instance = args.instance or run_config.get("instance", "main")
    main_queue_dir = campaign_root / instance / "queue"

    cycles, resolutions, lifecycle = load_cycle_records(args.run_dir / "cycles.jsonl")
    candidates_view = build_candidates_view(cycles, resolutions)
    candidates_view = report_time_resolution_scan(candidates_view, main_queue_dir)

    summary = build_summary_stats(cycles, candidates_view)
    candidate_table = build_candidate_table(candidates_view)

    lineage_by_seed = {}
    if not args.skip_lineage:
        fields_by_id = build_queue_index(main_queue_dir)
        children_index = build_children_index(fields_by_id)
        for c in candidates_view:
            if c.get("injected") and c.get("main_queue_resolved"):
                seed_id = c["main_queue_id"]
                if seed_id not in lineage_by_seed:
                    lineage_by_seed[seed_id] = trace_lineage(
                        children_index, fields_by_id, seed_id, max_depth=args.max_lineage_depth,
                    )

    svg = ""
    if not args.skip_plot:
        run_start_ts = _iso_to_unix(run_config.get("started_at"))
        run_end_ts = None
        for ev in lifecycle:
            if ev.get("record_type") == "run_end":
                run_end_ts = _iso_to_unix(ev.get("ended_at"))
        plateau_log_path = Path(run_config["plateau_log"]) if run_config.get("plateau_log") else None
        points = load_coverage_points(plateau_log_path, run_start_ts, run_end_ts) if plateau_log_path else []
        injection_events = [
            {
                "hours": ((_iso_to_unix(c["triggered_at"]) - run_start_ts) / 3600.0)
                         if run_start_ts and c.get("triggered_at") else None,
                "injected_count": c["injected_count"], "cycle_id": c["cycle_id"],
            }
            for c in cycles
        ]
        svg = render_coverage_svg(points, injection_events)

    md = render_markdown(run_config, summary, candidate_table, lineage_by_seed, svg)
    js = render_json(run_config, summary, candidate_table, lineage_by_seed)
    cs = render_csv(candidate_table)

    (args.run_dir / f"{args.out_prefix}.md").write_text(md)
    (args.run_dir / f"{args.out_prefix}.json").write_text(js)
    (args.run_dir / f"{args.out_prefix}.csv").write_text(cs)
    if svg:
        (args.run_dir / f"{args.out_prefix}.svg").write_text(svg)

    print(f"Wrote {args.out_prefix}.md / .json / .csv" + (" / .svg" if svg else "")
          + f" to {args.run_dir}")


if __name__ == "__main__":
    main()
