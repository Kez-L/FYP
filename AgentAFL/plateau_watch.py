#!/usr/bin/env python3
"""Passive AFL++ coverage/plateau logger — watches all instances, no side effects."""
import csv, sys, time, argparse
from pathlib import Path

def read_plot_data(instance_dir):
    """Parse an AFL++ plot_data file robustly (column order varies by version)."""
    pd = instance_dir / "plot_data"
    if not pd.exists():
        return None
    lines = [l.strip() for l in pd.read_text().splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    header = [h.strip().lstrip('#').strip() for h in lines[0].split(',')]
    last = lines[-1].split(',')
    row = dict(zip(header, last))
    # field name differs slightly across AFL++ versions
    edges = row.get('edges_found') or row.get('map_size') or '0'
    return {
        'unix_time': int(float(row.get('unix_time', time.time()))),
        'cur_item': int(float(row.get('cur_item', row.get('cur_path', 0)))),
        'edges_found': int(float(edges)),
        'saved_crashes': int(float(row.get('saved_crashes', 0))),
        'saved_hangs': int(float(row.get('saved_hangs', 0))),
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output-dir', default='output')
    ap.add_argument('--interval', type=int, default=60, help='seconds between polls')
    ap.add_argument('--plateau-secs', type=int, default=900, help='matches your 15-min definition')
    ap.add_argument('--once', action='store_true', help='single snapshot, no loop')
    ap.add_argument('--log', default='plateau_log.csv')
    args = ap.parse_args()

    out_dir = Path(args.output_dir)
    log_exists = Path(args.log).exists()
    log_f = open(args.log, 'a', newline='')
    writer = csv.writer(log_f)
    if not log_exists:
        writer.writerow(['timestamp', 'best_edges', 'instances', 'total_crashes', 'total_hangs', 'plateau_secs'])

    best_edges = 0
    best_edges_time = time.time()

    while True:
        instances = [d for d in out_dir.iterdir() if d.is_dir()]
        snapshot = {i.name: read_plot_data(i) for i in instances}
        snapshot = {k: v for k, v in snapshot.items() if v}

        if not snapshot:
            print(f"[{time.strftime('%H:%M:%S')}] No plot_data found yet under {out_dir}/<instance>/")
        else:
            cur_best = max(v['edges_found'] for v in snapshot.values())
            total_crashes = sum(v['saved_crashes'] for v in snapshot.values())
            total_hangs = sum(v['saved_hangs'] for v in snapshot.values())

            if cur_best > best_edges:
                best_edges = cur_best
                best_edges_time = time.time()

            idle = time.time() - best_edges_time
            status = "PLATEAU" if idle >= args.plateau_secs else "active"
            print(f"[{time.strftime('%H:%M:%S')}] best_edges={best_edges} "
                  f"instances={len(snapshot)} crashes={total_crashes} hangs={total_hangs} "
                  f"idle={int(idle)}s [{status}]")
            writer.writerow([int(time.time()), best_edges, len(snapshot), total_crashes, total_hangs, int(idle)])
            log_f.flush()

        if args.once:
            break
        time.sleep(args.interval)

    log_f.close()

if __name__ == '__main__':
    main()
