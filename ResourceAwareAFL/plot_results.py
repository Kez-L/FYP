"""Compare adaptive and baseline AFL++ experiment_metrics CSVs.

Usage:
    python3 plot_fyp_results.py --input-dir six_hour_results --output-dir plots --coverage-target 4.5

Requires: pandas, numpy, matplotlib.
"""
import argparse
from pathlib import Path
import re
import warnings

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

METRICS = {
    'coverage_pct': ('Code coverage', 'Coverage (%)', '01_coverage.png'),
    'edges_found': ('Total edges found', 'Edges found', '02_total_edges.png'),
    'new_edges': ('New edges since initial observation (cumulative)', 'New edges (cumulative)', '03_new_edges.png'),
    'cpu_percent': ('CPU usage', 'CPU usage (%)', '04_cpu_usage.png'),
    'memory_mb': ('Memory usage', 'Memory (MB)', '05_memory_usage.png'),
    'cpu_seconds_per_new_edge': ('Cumulative CPU seconds per new edge', 'CPU seconds / new edge', '06_cpu_cost_per_edge.png'),
    'memory_mb_seconds_per_new_edge': ('Cumulative MB-seconds per new edge', 'MB-seconds / new edge', '07_memory_cost_per_edge.png'),
    'execs_per_sec': ('Execution throughput', 'Executions / second', '08_execs_per_second.png'),
}

# Supports six_hour_results/adaptive_run_01/experiment_metrics.csv,
# six_hour_results/baseline_run_01/experiment_metrics.csv, and original A1/B1 CSV names.
def identify_run(path):
    for part in reversed(path.parts[:-1]):
        match = re.fullmatch(r'(adaptive|baseline)_run_(\d+)', part, re.I)
        if match:
            group = match.group(1).capitalize()
            return group, int(match.group(2))
    match = re.fullmatch(r'experiment_metrics([AB])(\d+)\.csv', path.name, re.I)
    if match:
        return ('Adaptive' if match.group(1).upper() == 'A' else 'Baseline',
                int(match.group(2)))
    return None


def read_run(path):
    df = pd.read_csv(path)
    required = {'elapsed_sec', 'coverage_pct', 'edges_found', 'new_edges', 'cpu_percent',
                'memory_mb', 'cpu_seconds_per_new_edge', 'memory_mb_seconds_per_new_edge',
                'execs_per_sec'}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f'{path.name}: missing columns {sorted(missing)}')
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=['elapsed_sec']).sort_values('elapsed_sec').drop_duplicates('elapsed_sec', keep='last')
    df = df[df['elapsed_sec'] >= 0].copy()
    if len(df) < 2:
        raise ValueError(f'{path.name}: fewer than 2 usable rows')
    df['hours'] = df['elapsed_sec'] / 3600
    # The two cost fields are undefined before any new edges are found, not zero-cost.
    for col in ('cpu_seconds_per_new_edge', 'memory_mb_seconds_per_new_edge'):
        df.loc[df['new_edges'] <= 0, col] = np.nan
    return df


def crossing_hours(df, target):
    # First recorded observation reaching target; do not invent between-window times.
    hits = df[df['coverage_pct'] >= target]
    return float(hits.iloc[0]['hours']) if not hits.empty else np.nan


def common_curve(group_runs, metric, grid):
    """Interpolate individual runs at common elapsed times, without extrapolation."""
    curves = []
    for _, _, df in group_runs:
        valid = df[['hours', metric]].dropna()
        arr = np.full(grid.shape, np.nan)
        if len(valid) >= 2:
            within = (grid >= valid['hours'].iloc[0]) & (grid <= valid['hours'].iloc[-1])
            arr[within] = np.interp(grid[within], valid['hours'], valid[metric])
        curves.append(arr)
    return np.vstack(curves)


def plot_metric(all_runs, metric, target_dir, common_end):
    title, ylabel, filename = METRICS[metric]
    fig, ax = plt.subplots(figsize=(11, 6))
    colors = {'Adaptive': '#1168a6', 'Baseline': '#dd7e22'}
    groupings = {g: [(g2, n, df) for (g2, n, df) in all_runs if g2 == g]
                 for g in ('Adaptive', 'Baseline')}
    for g, group_runs in groupings.items():
        for _, n, df in group_runs:
            ax.plot(df['hours'], df[metric], color=colors[g], alpha=.26, lw=1,
                    label=f'{g} {n}')
        if not group_runs:
            continue
        # For group averages, use 1-minute common grid, up to shortest run (fair comparison).
        grid = np.linspace(0, common_end, max(2, int(common_end * 60) + 1))
        values = common_curve(group_runs, metric, grid)
        mean = np.full(grid.shape, np.nan)
        count = np.isfinite(values).sum(axis=0)
        valid = count == len(group_runs)  # exclude time points missing any run in group
        mean[valid] = values[:, valid].mean(axis=0)
        ax.plot(grid, mean, color=colors[g], lw=2.8, label=f'{g} mean (n={len(group_runs)})')
    ax.set(title=title, xlabel='Elapsed time (hours)', ylabel=ylabel)
    ax.set_xlim(0, common_end)
    ax.grid(alpha=.22)
    ax.legend(fontsize=8, ncol=2)
    fig.tight_layout()
    fig.savefig(target_dir / filename, dpi=200)
    plt.close(fig)


def summarize(all_runs, target, common_end, output_dir):
    rows = []
    comparable = []
    for group, num, df in all_runs:
        # The last observation at/before common end: comparable horizon, no extrapolation.
        end = df[df['hours'] <= common_end].iloc[-1]
        row = {
            'configuration': group, 'run': num, 'monitoring_windows': len(df),
            'logged_duration_hours': df['hours'].max(),
            'comparison_time_hours': float(end['hours']),
            'coverage_pct': end['coverage_pct'],
            'edges_found': end['edges_found'],
            'new_edges_cumulative': end['new_edges'],
            'executions': end['execs_done'] if 'execs_done' in df else np.nan,
            'cpu_seconds_per_new_edge': end['cpu_seconds_per_new_edge'],
            'memory_mb_seconds_per_new_edge': end['memory_mb_seconds_per_new_edge'],
            'mean_cpu_percent': df[df['hours'] <= common_end]['cpu_percent'].iloc[1:].mean(),
            'mean_memory_mb': df[df['hours'] <= common_end]['memory_mb'].iloc[1:].mean(),
            f'time_to_{target:g}pct_hours': crossing_hours(df, target),
            'saved_crashes': end.get('saved_crashes', np.nan),
            'saved_hangs': end.get('saved_hangs', np.nan),
        }
        rows.append(row)
        comparable.append(row)
    summary = pd.DataFrame(rows)
    summary.to_csv(output_dir / 'run_summary.csv', index=False)
    numeric = ['coverage_pct', 'edges_found', 'new_edges_cumulative',
               'cpu_seconds_per_new_edge', 'memory_mb_seconds_per_new_edge',
               'mean_cpu_percent', 'mean_memory_mb', f'time_to_{target:g}pct_hours']
    grouped = summary.groupby('configuration')[numeric].agg(['mean', 'std', 'count'])
    grouped.to_csv(output_dir / 'group_summary.csv')
    return summary


def plot_target(all_runs, target, output_dir):
    fig, ax = plt.subplots(figsize=(9, 5))
    colors = {'Adaptive': '#1168a6', 'Baseline': '#dd7e22'}
    positions = {'Adaptive': [], 'Baseline': []}
    labels = []
    for i, (group, num, df) in enumerate(all_runs):
        time = crossing_hours(df, target)
        labels.append(f'{group} {num}')
        if np.isfinite(time):
            ax.bar(i, time, color=colors[group], width=.55)
            ax.text(i, time + .04, f'{time:.2f}h', ha='center', fontsize=9)
        else:
            ax.bar(i, 0, color=colors[group], width=.55)
            ax.text(i, .06, 'Not reached', ha='center', rotation=90, fontsize=9)
    ax.set_xticks(range(len(labels)), labels=labels)
    ax.set(ylabel='Hours until first recorded threshold',
           title=f'Time to reach {target:g}% coverage (not reached = no bar)')
    ax.set_ylim(0, max(1, max(df['hours'].max() for _, _, df in all_runs) * 1.1))
    ax.grid(axis='y', alpha=.2)
    fig.tight_layout()
    fig.savefig(output_dir / '09_time_to_coverage_target.png', dpi=200)
    plt.close(fig)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input-dir', type=Path, default=Path('.'))
    p.add_argument('--output-dir', type=Path, default=Path('plots'))
    p.add_argument('--coverage-target', type=float, default=4.5)
    p.add_argument('--runs', nargs='+', metavar='RUN',
                   help='Only include specified run IDs, e.g. --runs A2 A3 B1 B2')
    args = p.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    all_runs = []
    for path in sorted(args.input_dir.rglob('experiment_metrics*.csv')):
        id_ = identify_run(path)
        if id_:
            all_runs.append((*id_, read_run(path)))
    if args.runs:
        requested = [r.upper() for r in args.runs]
        invalid = [r for r in requested if not re.fullmatch(r'[AB]\d+', r)]
        if invalid:
            p.error(f'Invalid run IDs: {invalid}. Use IDs like A2 A3 B1 B2')
        def run_id(group, num):
            return ('A' if group == 'Adaptive' else 'B') + str(num)
        all_runs = [(g, n, df) for g, n, df in all_runs
                    if run_id(g, n) in requested]
        found = {run_id(g, n) for g, n, _ in all_runs}
        missing = sorted(set(requested) - found)
        if missing:
            p.error(f'Requested runs not found: {", ".join(missing)}. '
                    'Check folders under --input-dir.')
    if not all_runs:
        p.error('No experiment_metrics.csv found under adaptive_run_XX/baseline_run_XX, or A1/B1-style files')
    common_end = min(df['hours'].max() for _, _, df in all_runs)
    print(f'Loaded {len(all_runs)} runs; common plotting horizon: {common_end:.4f} hours')
    for g, n, df in all_runs:
        print(f'  {g} {n}: {len(df)} windows, {df.hours.max():.3f} hours')
    for metric in METRICS:
        plot_metric(all_runs, metric, args.output_dir, common_end)
    plot_target(all_runs, args.coverage_target, args.output_dir)
    result = summarize(all_runs, args.coverage_target, common_end, args.output_dir)
    print('\nComparable endpoint summary:\n', result[['configuration','run','coverage_pct',
          'new_edges_cumulative','cpu_seconds_per_new_edge']].to_string(index=False))
    print(f'\nFigures and CSVs written to: {args.output_dir.resolve()}')
    print('Note: efficiency ratios are cumulative and undefined before new edges are found.')

if __name__ == '__main__':
    main()
