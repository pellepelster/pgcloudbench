#!/usr/bin/env python3

import argparse
import json
import os
import sys

import matplotlib.pyplot as plt
import matplotlib.cm as cm
import numpy as np
import yaml

from bench_run import parse_pgbench_output


REPO_ROOT   = os.path.dirname(os.path.abspath(__file__))
TESTS_YAML  = os.path.join(REPO_ROOT, "tests", "tests.yaml")
OUTPUT_DIR  = os.path.join(REPO_ROOT, "output")


def load_runs(testbed_name: str) -> tuple[dict, list[dict]]:
    """Load meta.json and parse all raw files for a testbed.

    Returns (meta, runs) where each run has 'clients', 'threads', 'progress',
    and 'summary' keys.
    """
    result_dir = os.path.join(OUTPUT_DIR, testbed_name)
    meta_path  = os.path.join(result_dir, "meta.json")
    with open(meta_path) as f:
        meta = json.load(f)

    runs = []
    for entry in meta["runs"]:
        raw_path = os.path.join(result_dir, entry["raw_file"])
        with open(raw_path) as f:
            raw = f.read()
        parsed = parse_pgbench_output(raw)
        runs.append({"clients": entry["clients"], "threads": entry["threads"], **parsed})

    return meta, runs


def make_figure(testbed_name: str, meta: dict, runs: list[dict], dpi: int) -> str:
    run_cfg    = meta.get("pgbench", {}).get("run", {})
    pg_host    = meta.get("postgresql_host", "?")
    duration   = run_cfg.get("duration", "?")
    dbname     = run_cfg.get("dbname", "pgbench")
    title      = f"{testbed_name} ({duration}s / run)"

    colors = cm.tab10(np.linspace(0, 0.9, len(runs)))

    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    fig.suptitle(f"pgbench — {title}", fontsize=13, fontweight="bold")

    ax_tps_ts, ax_lat_ts = axes[0]
    ax_tps_bar, ax_lat_bar = axes[1]

    has_progress = any(r["progress"] for r in runs)
    if not has_progress:
        for ax in (ax_tps_ts, ax_lat_ts):
            ax.text(0.5, 0.5, "No progress data", ha="center", va="center",
                    transform=ax.transAxes, color="grey")
    else:
        for run, color in zip(runs, colors):
            pts = run["progress"]
            if not pts:
                continue
            label = f"{run['clients']}c"
            xs = [p["elapsed_s"] for p in pts]
            ax_tps_ts.plot(xs, [p["tps"]        for p in pts], label=label, color=color)
            ax_lat_ts.plot(xs, [p["lat_avg_ms"] for p in pts], label=label, color=color)

    ax_tps_ts.set_title("TPS over time")
    ax_tps_ts.set_xlabel("Elapsed (s)")
    ax_tps_ts.set_ylabel("Transactions / sec")
    ax_tps_ts.legend(title="clients", fontsize=8)
    ax_tps_ts.grid(True, alpha=0.3)

    ax_lat_ts.set_title("Latency over time")
    ax_lat_ts.set_xlabel("Elapsed (s)")
    ax_lat_ts.set_ylabel("Avg latency (ms)")
    ax_lat_ts.legend(title="clients", fontsize=8)
    ax_lat_ts.grid(True, alpha=0.3)

    clients_labels = [str(r["clients"]) for r in runs]
    x     = np.arange(len(runs))
    bar_w = 0.6

    tps_vals = [r["summary"].get("tps_excl", 0) for r in runs]
    lat_vals = [r["summary"].get("lat_avg_ms", 0) for r in runs]

    bars_tps = ax_tps_bar.bar(x, tps_vals, width=bar_w, color=colors)
    ax_tps_bar.set_title("TPS vs clients (summary)")
    ax_tps_bar.set_xlabel("Client count")
    ax_tps_bar.set_ylabel("TPS (excl. connection setup)")
    ax_tps_bar.set_xticks(x)
    ax_tps_bar.set_xticklabels(clients_labels)
    ax_tps_bar.grid(axis="y", alpha=0.3)
    ax_tps_bar.bar_label(bars_tps, fmt="%.0f", padding=3, fontsize=8)

    bars_lat = ax_lat_bar.bar(x, lat_vals, width=bar_w, color=colors)
    ax_lat_bar.set_title("Latency vs clients (summary)")
    ax_lat_bar.set_xlabel("Client count")
    ax_lat_bar.set_ylabel("Avg latency (ms)")
    ax_lat_bar.set_xticks(x)
    ax_lat_bar.set_xticklabels(clients_labels)
    ax_lat_bar.grid(axis="y", alpha=0.3)
    ax_lat_bar.bar_label(bars_lat, fmt="%.1f ms", padding=3, fontsize=8)

    lat_std = [r["summary"].get("lat_stddev_ms") for r in runs]
    if all(v is not None for v in lat_std):
        ax_lat_bar.errorbar(x, lat_vals, yerr=lat_std,
                            fmt="none", color="black", capsize=4, linewidth=1.2)

    fig.tight_layout()
    output = os.path.join(OUTPUT_DIR, testbed_name, "results.png")
    fig.savefig(output, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return output


def main():
    ap = argparse.ArgumentParser(description="Plot pgbench results for testbeds in tests/tests.yaml")
    ap.add_argument("testbed", nargs="?", default=None, help="Plot only this testbed (default: all)")
    ap.add_argument("--dpi", type=int, default=120)
    args = ap.parse_args()

    with open(TESTS_YAML) as f:
        config = yaml.safe_load(f)

    testbeds = config.get("testbeds") or []

    if args.testbed and not any(t["name"] == args.testbed for t in testbeds):
        print(f"ERROR: testbed '{args.testbed}' not found in {TESTS_YAML}", file=sys.stderr)
        sys.exit(1)

    plotted = 0
    for testbed in testbeds:
        name = testbed["name"]
        if args.testbed and name != args.testbed:
            continue

        meta_path = os.path.join(OUTPUT_DIR, name, "meta.json")
        if not os.path.isfile(meta_path):
            print(f"Skipping {name}: no results at {meta_path}")
            continue

        meta, runs = load_runs(name)
        if not runs:
            print(f"Skipping {name}: no runs in meta.json")
            continue

        output = make_figure(name, meta, runs, args.dpi)
        print(f"Plot saved to {output}")
        plotted += 1

    print(f"\nDone. Plotted {plotted} testbed(s).")


if __name__ == "__main__":
    main()
