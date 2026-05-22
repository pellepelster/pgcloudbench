#!/usr/bin/env python3
"""
bench.py — pgbench benchmark tool.

Commands:
  provision   Provision testbed infrastructure via Terraform + Ansible
  initialize  Initialize pgbench schema on testbed database(s)
  run         Run pgbench client sweep against testbed(s)
  plot        Plot results from a previous run
  parse       Parse raw output into a results JSON file
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import yaml


REPO_ROOT        = os.path.dirname(os.path.abspath(__file__))
TESTS_YAML       = os.path.join(REPO_ROOT, "tests", "tests.yaml")
OUTPUT_DIR       = os.path.join(REPO_ROOT, "output")
TERRAFORM_DIR    = os.path.join(REPO_ROOT, "terraform")
ANSIBLE_PLAYBOOK = os.path.join(REPO_ROOT, "ansible", "playbook.yml")


# ---------------------------------------------------------------------------
# Parsing utilities
# ---------------------------------------------------------------------------

_PROGRESS_RE = re.compile(
    r"progress:\s+([\d.]+)\s+s,\s+([\d.]+)\s+tps,\s+lat\s+([\d.]+)\s+ms\s+stddev\s+([\d.]+)"
)
_TPS_EXCL_RE = re.compile(r"tps\s*=\s*([\d.]+)\s+\((?:excluding connections|without initial connection time)\)")
_TPS_INCL_RE = re.compile(r"tps\s*=\s*([\d.]+)\s+\((?:including connections|with initial connection time)\)")
_LAT_AVG_RE  = re.compile(r"latency average\s*=\s*([\d.]+)\s+ms")
_LAT_STD_RE  = re.compile(r"latency stddev\s*=\s*([\d.]+)\s+ms")
_TXN_RE      = re.compile(r"number of transactions actually processed:\s+(\d+)")


def parse_pgbench_output(text: str) -> dict:
    progress = []
    summary  = {}
    for line in text.splitlines():
        m = _PROGRESS_RE.search(line)
        if m:
            progress.append({
                "elapsed_s":     float(m.group(1)),
                "tps":           float(m.group(2)),
                "lat_avg_ms":    float(m.group(3)),
                "lat_stddev_ms": float(m.group(4)),
            })
            continue
        if (m := _TPS_EXCL_RE.search(line)):
            summary["tps_excl"] = float(m.group(1))
        if (m := _TPS_INCL_RE.search(line)):
            summary["tps_incl"] = float(m.group(1))
        if (m := _LAT_AVG_RE.search(line)):
            summary["lat_avg_ms"] = float(m.group(1))
        if (m := _LAT_STD_RE.search(line)):
            summary["lat_stddev_ms"] = float(m.group(1))
        if (m := _TXN_RE.search(line)):
            summary["transactions"] = int(m.group(1))
    return {"progress": progress, "summary": summary}


def parse_ansible_inventory(path: str) -> dict:
    groups: dict = {}
    current_group: str | None = None
    current_is_vars = False

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                header = line[1:line.index("]")]
                if ":" in header:
                    group_name, qualifier = header.split(":", 1)
                    current_is_vars = qualifier == "vars"
                else:
                    group_name = header
                    current_is_vars = False
                current_group = group_name
                groups.setdefault(current_group, {"hosts": {}, "vars": {}})
            elif current_is_vars and current_group:
                key, _, value = line.partition("=")
                groups[current_group]["vars"][key.strip()] = value.strip()
            elif current_group:
                parts = line.split()
                hostname = parts[0]
                host_vars = {}
                for part in parts[1:]:
                    k, _, v = part.partition("=")
                    host_vars[k] = v
                groups[current_group]["hosts"][hostname] = host_vars

    return groups


# ---------------------------------------------------------------------------
# pgbench / SSH utilities
# ---------------------------------------------------------------------------

def build_pgbench_cmd(clients: int, threads: int, duration: int, conn: str,
                      pgbench_bin: str = "pgbench", password: str | None = None) -> str:
    prefix = f"PGPASSWORD={password} " if password else ""
    return (f"{prefix}{pgbench_bin} --client={clients} --jobs={threads}"
            f" --time={duration} --progress=1 --no-vacuum {conn} 2>&1")


def make_ssh_executor(ssh):
    def exec_fn(cmd: str) -> str:
        _stdin, stdout, _stderr = ssh.exec_command(cmd)
        return stdout.read().decode(errors="replace")
    return exec_fn


def resolve_pgbench_config(global_cfg: dict, testbed_cfg: dict) -> dict:
    defaults = {
        "clients":  "1,2,4,8,16",
        "duration": 60,
        "threads":  None,
        "pg_user":  "postgres",
        "pg_port":  5432,
        "dbname":   "pgcloudbench",
        "scale":    1,
    }
    return {**defaults, **{k: v for k, v in global_cfg.items() if k in defaults},
                         **{k: v for k, v in testbed_cfg.items() if k in defaults}}


def _pgbench_bin(testbed: dict) -> str:
    version = testbed.get("pgbench_version", 18)
    return f"/usr/lib/postgresql/{version}/bin/pgbench"


def _conn_str(cfg: dict, host: str) -> str:
    parts = [f"--host={host}", f"--port={cfg['pg_port']}"]
    if cfg.get("pg_user"):
        parts.append(f"--username={cfg['pg_user']}")
    parts.append(cfg["dbname"])
    return " ".join(parts)


def _connect_pgbench_host(name: str):
    """Open an SSH connection to the pgbench host for a testbed.

    Returns (ssh, exec_fn, pgbench_ip, postgresql_ip, password) or None when
    prerequisites (inventory, db_endpoint) are missing.
    """
    import paramiko

    inventory_path   = os.path.join(OUTPUT_DIR, name, "ansible_inventory")
    db_endpoint_path = os.path.join(OUTPUT_DIR, name, "db_endpoint")

    if not os.path.isfile(inventory_path):
        print(f"Skipping {name}: no inventory at {inventory_path}", file=sys.stderr)
        return None
    if not os.path.isfile(db_endpoint_path):
        print(f"Skipping {name}: no db_endpoint at {db_endpoint_path}", file=sys.stderr)
        return None

    with open(db_endpoint_path) as f:
        postgresql_ip = f.read().strip()

    db_password_path = os.path.join(OUTPUT_DIR, name, "db_password")
    password = None
    if os.path.isfile(db_password_path):
        with open(db_password_path) as f:
            password = f.read().strip()

    inventory     = parse_ansible_inventory(inventory_path)
    pgbench_hosts = inventory.get("pgbench", {}).get("hosts", {})
    if not pgbench_hosts:
        print(f"Skipping {name}: no [pgbench] hosts in inventory", file=sys.stderr)
        return None

    pgbench_entry = next(iter(pgbench_hosts.items()))
    pgbench_ip    = pgbench_entry[1].get("ansible_host", pgbench_entry[0])
    all_vars      = inventory.get("all", {}).get("vars", {})
    ssh_user      = all_vars.get("ansible_user", "root")
    ssh_key       = all_vars.get("ansible_ssh_private_key_file")
    if ssh_key:
        ssh_key = os.path.expanduser(ssh_key)

    print(f"\n=== {name}: connecting to pgbench host {pgbench_ip} ===", file=sys.stderr)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs: dict = {"hostname": pgbench_ip, "port": 22, "username": ssh_user}
    if ssh_key:
        connect_kwargs["key_filename"] = ssh_key
    ssh.connect(**connect_kwargs)

    return ssh, make_ssh_executor(ssh), pgbench_ip, postgresql_ip, password


def _load_config(testbed_name: str | None):
    """Load tests.yaml, validate optional testbed filter, return (testbeds, global_pgbench)."""
    with open(TESTS_YAML) as f:
        config = yaml.safe_load(f)
    testbeds       = config.get("testbeds") or []
    global_pgbench = config.get("pgbench") or {}
    if testbed_name and not any(t["name"] == testbed_name for t in testbeds):
        print(f"ERROR: testbed '{testbed_name}' not found in {TESTS_YAML}", file=sys.stderr)
        sys.exit(1)
    return testbeds, global_pgbench


def _iter_testbeds(testbeds: list[dict], name_filter: str | None):
    for t in testbeds:
        if name_filter and t["name"] != name_filter:
            continue
        yield t


# ---------------------------------------------------------------------------
# provision
# ---------------------------------------------------------------------------

def _run_subprocess(cmd: list[str], cwd: str) -> None:
    print(f"  $ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        print(f"ERROR: command failed with exit code {result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)


def _deploy_testbed(testbed: dict) -> None:
    name      = testbed["name"]
    tf_config = testbed["terraform"]

    tf_dir = os.path.join(TERRAFORM_DIR, name)
    if not os.path.isdir(tf_dir):
        print(f"ERROR: terraform directory not found: {tf_dir}", file=sys.stderr)
        sys.exit(1)

    output_path    = os.path.join(OUTPUT_DIR, name)
    os.makedirs(output_path, exist_ok=True)
    inventory_path = os.path.join(output_path, "ansible_inventory")
    db_address     = (testbed.get("database") or {}).get("address")

    var_args = ["-var", f"output_path={output_path}"]
    if db_address:
        var_args += ["-var", f"db_address={db_address}"]
    for key, value in (tf_config.get("configuration") or {}).items():
        var_args += ["-var", f"{key}={value}"]

    print(f"\n=== {name}: terraform init ===")
    _run_subprocess(["terraform", "init"], cwd=tf_dir)

    print(f"\n=== {name}: terraform apply ===")
    _run_subprocess(["terraform", "apply", "-auto-approve"] + var_args, cwd=tf_dir)

    if db_address:
        with open(os.path.join(output_path, "db_endpoint"), "w") as f:
            f.write(db_address)

    if os.path.isfile(inventory_path):
        db_config    = (testbed.get("database") or {}).get("configuration") or {}
        ev: dict     = {"postgresql_configuration": db_config}
        db_pass_path = os.path.join(output_path, "db_password")
        if os.path.isfile(db_pass_path):
            with open(db_pass_path) as f:
                ev["postgresql_pgbench_password"] = f.read().strip()
        extra_vars = json.dumps(ev)
        print(f"\n=== {name}: ansible-playbook ===")
        _run_subprocess(["ansible-playbook", "-i", inventory_path, ANSIBLE_PLAYBOOK,
                         "-e", extra_vars], cwd=REPO_ROOT)


def cmd_provision(args):
    testbeds, _ = _load_config(args.testbed)
    deployed = 0
    for testbed in _iter_testbeds(testbeds, args.testbed):
        if "terraform" not in testbed:
            print(f"Skipping {testbed['name']} (no terraform config)")
            continue
        _deploy_testbed(testbed)
        deployed += 1
    print(f"\nDone. Deployed {deployed} testbed(s).")


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------

def _initialize_testbed(testbed: dict, global_pgbench: dict) -> None:
    name            = testbed["name"]
    testbed_pgbench = testbed.get("pgbench") or {}
    scale           = testbed_pgbench.get("scale") or global_pgbench.get("scale") or 1
    run_cfg         = resolve_pgbench_config(global_pgbench.get("run") or {},
                                             testbed_pgbench.get("run") or {})

    conn = _connect_pgbench_host(name)
    if conn is None:
        return
    ssh, exec_fn, _pgbench_ip, postgresql_ip, password = conn

    prefix = f"PGPASSWORD={password} " if password else ""
    cmd    = (f"{prefix}{_pgbench_bin(testbed)} --initialize --scale={scale}"
              f" {_conn_str(run_cfg, postgresql_ip)} 2>&1")

    print(f"\n=== {name}: pgbench initialize (scale={scale}) ===", file=sys.stderr)
    print(f"  {cmd}", file=sys.stderr)
    try:
        for line in exec_fn(cmd).splitlines():
            print(f"  {line}", file=sys.stderr)
    finally:
        ssh.close()


def cmd_initialize(args):
    testbeds, global_pgbench = _load_config(args.testbed)
    ran = 0
    for testbed in _iter_testbeds(testbeds, args.testbed):
        _initialize_testbed(testbed, global_pgbench)
        ran += 1
    print(f"\nDone. Initialized {ran} testbed(s).")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def _pgbench_pass(exec_fn, clients_list: list[int], duration: int,
                  threads: int | None, conn: str, pgbench_bin: str,
                  collect: bool, output_dir: str | None,
                  password: str | None = None) -> list[dict]:
    entries = []
    for clients in clients_list:
        t   = threads if threads is not None else clients
        cmd = build_pgbench_cmd(clients, t, duration, conn, pgbench_bin, password)
        print(f"\n[{clients}c/{t}j]  {cmd}", file=sys.stderr)
        raw = exec_fn(cmd)
        for line in raw.splitlines():
            print(f"  {line}", file=sys.stderr)
        if collect and output_dir:
            raw_filename = os.path.join("raw", f"c{clients:04d}_j{t:03d}.txt")
            with open(os.path.join(output_dir, raw_filename), "w") as f:
                f.write(raw)
            entries.append({"clients": clients, "threads": t, "raw_file": raw_filename})
    return entries


def _run_sweep(testbed: dict, run_cfg: dict, warmup_cfg: dict | None) -> None:
    name = testbed["name"]

    conn = _connect_pgbench_host(name)
    if conn is None:
        return
    ssh, exec_fn, pgbench_ip, postgresql_ip, password = conn

    now        = datetime.now(timezone.utc)
    run_dir    = os.path.join(OUTPUT_DIR, name, now.strftime("%Y-%m-%d-%H-%M-%S"))
    os.makedirs(os.path.join(run_dir, "raw"), exist_ok=True)
    pgbench_bin = _pgbench_bin(testbed)

    try:
        if warmup_cfg:
            print(f"\n=== {name}: warmup ===", file=sys.stderr)
            warmup_clients = [int(c.strip()) for c in str(warmup_cfg["clients"]).split(",")]
            _pgbench_pass(exec_fn, warmup_clients, warmup_cfg["duration"],
                          warmup_cfg.get("threads"), _conn_str(warmup_cfg, postgresql_ip),
                          pgbench_bin, collect=False, output_dir=None, password=password)

        print(f"\n=== {name}: run ===", file=sys.stderr)
        run_clients = [int(c.strip()) for c in str(run_cfg["clients"]).split(",")]
        run_entries = _pgbench_pass(exec_fn, run_clients, run_cfg["duration"],
                                    run_cfg.get("threads"), _conn_str(run_cfg, postgresql_ip),
                                    pgbench_bin, collect=True, output_dir=run_dir,
                                    password=password)
    finally:
        ssh.close()

    meta = {
        "testbed":         name,
        "terraform":       testbed.get("terraform", {}),
        "pgbench_host":    pgbench_ip,
        "postgresql_host": postgresql_ip,
        "pgbench_version": testbed.get("pgbench_version", 18),
        "pgbench":         {"warmup": warmup_cfg, "run": run_cfg},
        "timestamp":       now.isoformat(),
        "runs":            run_entries,
    }
    with open(os.path.join(run_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    print(f"\nResults saved to {run_dir}/", file=sys.stderr)


def cmd_run(args):
    testbeds, global_pgbench = _load_config(args.testbed)
    ran = 0
    for testbed in _iter_testbeds(testbeds, args.testbed):
        if "terraform" not in testbed:
            print(f"Skipping {testbed['name']} (no terraform config)")
            continue
        testbed_pgbench = testbed.get("pgbench") or {}
        run_cfg    = resolve_pgbench_config(global_pgbench.get("run") or {},
                                            testbed_pgbench.get("run") or {})
        warmup_cfg = resolve_pgbench_config(global_pgbench.get("warmup") or {},
                                            testbed_pgbench.get("warmup") or {}) \
                     if (global_pgbench.get("warmup") or testbed_pgbench.get("warmup")) \
                     else None
        _run_sweep(testbed, run_cfg, warmup_cfg)
        ran += 1
    print(f"\nDone. Ran sweep for {ran} testbed(s).")


# ---------------------------------------------------------------------------
# plot
# ---------------------------------------------------------------------------

def _latest_run_dir(testbed_name: str) -> str | None:
    base = os.path.join(OUTPUT_DIR, testbed_name)
    if not os.path.isdir(base):
        return None
    candidates = sorted(
        d for d in os.listdir(base)
        if os.path.isfile(os.path.join(base, d, "meta.json"))
    )
    return os.path.join(base, candidates[-1]) if candidates else None


def _load_runs(run_dir: str) -> tuple[dict, list[dict]]:
    with open(os.path.join(run_dir, "meta.json")) as f:
        meta = json.load(f)
    runs = []
    for entry in meta["runs"]:
        with open(os.path.join(run_dir, entry["raw_file"])) as f:
            raw = f.read()
        parsed = parse_pgbench_output(raw)
        runs.append({"clients": entry["clients"], "threads": entry["threads"], **parsed})
    return meta, runs


def _display_label(testbed_name: str, meta: dict) -> str:
    cfg = (meta.get("terraform") or {}).get("configuration") or {}
    if not cfg:
        return testbed_name
    parts = ", ".join(f"{k}={v}" for k, v in cfg.items())
    return f"{testbed_name} ({parts})"


def _make_figure(testbed_name: str, run_dir: str, meta: dict, runs: list[dict], dpi: int) -> str:
    import matplotlib.cm as cm
    import matplotlib.pyplot as plt
    import numpy as np

    run_cfg  = meta.get("pgbench", {}).get("run", {})
    duration = run_cfg.get("duration", "?")
    title    = f"{_display_label(testbed_name, meta)} ({duration}s / run)"
    colors   = cm.tab10(np.linspace(0, 0.9, len(runs)))

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
            if not run["progress"]:
                continue
            label = f"{run['clients']}c"
            xs = [p["elapsed_s"] for p in run["progress"]]
            ax_tps_ts.plot(xs, [p["tps"]        for p in run["progress"]], label=label, color=color)
            ax_lat_ts.plot(xs, [p["lat_avg_ms"] for p in run["progress"]], label=label, color=color)

    ax_tps_ts.set_title("TPS over time");  ax_tps_ts.set_xlabel("Elapsed (s)")
    ax_tps_ts.set_ylabel("Transactions / sec")
    ax_tps_ts.legend(title="clients", fontsize=8);  ax_tps_ts.grid(True, alpha=0.3)

    ax_lat_ts.set_title("Latency over time");  ax_lat_ts.set_xlabel("Elapsed (s)")
    ax_lat_ts.set_ylabel("Avg latency (ms)")
    ax_lat_ts.legend(title="clients", fontsize=8);  ax_lat_ts.grid(True, alpha=0.3)

    x     = np.arange(len(runs))
    bar_w = 0.6
    tps_vals = [r["summary"].get("tps_excl", 0) for r in runs]
    lat_vals = [r["summary"].get("lat_avg_ms", 0) for r in runs]
    labels   = [str(r["clients"]) for r in runs]

    bars = ax_tps_bar.bar(x, tps_vals, width=bar_w, color=colors)
    ax_tps_bar.set_title("TPS vs clients (summary)");  ax_tps_bar.set_xlabel("Client count")
    ax_tps_bar.set_ylabel("TPS (excl. connection setup)")
    ax_tps_bar.set_xticks(x);  ax_tps_bar.set_xticklabels(labels)
    ax_tps_bar.grid(axis="y", alpha=0.3)
    ax_tps_bar.bar_label(bars, fmt="%.0f", padding=3, fontsize=8)

    bars = ax_lat_bar.bar(x, lat_vals, width=bar_w, color=colors)
    ax_lat_bar.set_title("Latency vs clients (summary)");  ax_lat_bar.set_xlabel("Client count")
    ax_lat_bar.set_ylabel("Avg latency (ms)")
    ax_lat_bar.set_xticks(x);  ax_lat_bar.set_xticklabels(labels)
    ax_lat_bar.grid(axis="y", alpha=0.3)
    ax_lat_bar.bar_label(bars, fmt="%.1f ms", padding=3, fontsize=8)

    fig.tight_layout()
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()

    output = os.path.join(run_dir, f"{testbed_name}.png")
    fig.savefig(output, dpi=dpi, bbox_inches="tight")

    for ax, suffix in [
        (ax_tps_ts,  "tps_timeseries"),
        (ax_lat_ts,  "lat_timeseries"),
        (ax_tps_bar, "tps_bar"),
        (ax_lat_bar, "lat_bar"),
    ]:
        bbox = ax.get_tightbbox(renderer).transformed(fig.dpi_scale_trans.inverted())
        path = os.path.join(run_dir, f"{testbed_name}_{suffix}.png")
        fig.savefig(path, dpi=dpi, bbox_inches=bbox.expanded(1.02, 1.08))

    plt.close(fig)
    return output


def _make_combined_bar(all_results: list[tuple[str, list[dict]]], dpi: int,
                       metric: str, ylabel: str, title: str, fmt: str,
                       filename: str, output_dir: str) -> str | None:
    import matplotlib.cm as cm
    import matplotlib.pyplot as plt
    import numpy as np

    if not all_results:
        return None

    all_clients = sorted({r["clients"] for _, runs in all_results for r in runs})
    if not all_clients:
        return None

    n_testbeds = len(all_results)
    colors     = cm.tab10(np.linspace(0, 0.9, n_testbeds))
    bar_w      = 0.8 / n_testbeds
    x          = np.arange(len(all_clients))

    fig, ax = plt.subplots(figsize=(max(10, len(all_clients) * n_testbeds * 0.6), 6))
    ax.set_title(f"{title} — all testbeds", fontsize=13, fontweight="bold")

    for i, (name, runs) in enumerate(all_results):
        vals_by_clients = {r["clients"]: r["summary"].get(metric, 0) for r in runs}
        vals   = [vals_by_clients.get(c, 0) for c in all_clients]
        offset = (i - n_testbeds / 2 + 0.5) * bar_w
        bars   = ax.bar(x + offset, vals, width=bar_w, label=name, color=colors[i])
        ax.bar_label(bars, fmt=fmt, padding=3, fontsize=7, rotation=90)

    ax.set_xlabel("Client count")
    ax.set_ylabel(ylabel)
    ax.set_xticks(x)
    ax.set_xticklabels([str(c) for c in all_clients])
    ax.legend(title="testbed", fontsize=9)
    ax.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    output = os.path.join(output_dir, filename)
    fig.savefig(output, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return output


def _make_combined_tps_bar(all_results, dpi, output_dir):
    return _make_combined_bar(all_results, dpi,
                              metric="tps_excl",
                              ylabel="TPS (excl. connection setup)",
                              title="TPS vs clients",
                              fmt="%.0f",
                              filename="all_tps_bar.png",
                              output_dir=output_dir)


def _make_combined_lat_bar(all_results, dpi, output_dir):
    return _make_combined_bar(all_results, dpi,
                              metric="lat_avg_ms",
                              ylabel="Avg latency (ms)",
                              title="Latency vs clients",
                              fmt="%.1f",
                              filename="all_lat_bar.png",
                              output_dir=output_dir)


def _write_readme(testbeds: list[dict], combined_dir: str | None = None) -> None:
    lines = ["# pgcloudbench results\n"]

    lines.append("## Comparison\n")
    for filename, alt in [("all_tps_bar.png", "TPS — all testbeds"),
                          ("all_lat_bar.png", "Latency — all testbeds")]:
        if combined_dir is None:
            continue
        img = os.path.join(combined_dir, filename)
        if os.path.isfile(img):
            lines.append(f"![{alt}]({filename})\n")

    for testbed in testbeds:
        name    = testbed["name"]
        run_dir = _latest_run_dir(name)
        if run_dir is None:
            continue
        img     = os.path.join(run_dir, f"{name}.png")
        if not os.path.isfile(img):
            continue
        readme_dir = combined_dir or REPO_ROOT
        rel_img = os.path.relpath(img, readme_dir)
        lines.append(f"## {name}\n")
        lines.append(f"![{name}]({rel_img})\n")

    readme_dir = combined_dir or REPO_ROOT
    readme = os.path.join(readme_dir, "README.md")
    with open(readme, "w") as f:
        f.write("\n".join(lines))
    print(f"README written to {readme}")


def cmd_plot(args):
    testbeds, _ = _load_config(args.testbed)
    all_results: list[tuple[str, list[dict]]] = []
    run_dirs: list[str] = []
    plotted = 0
    for testbed in _iter_testbeds(testbeds, args.testbed):
        name    = testbed["name"]
        run_dir = _latest_run_dir(name)
        if run_dir is None:
            print(f"Skipping {name}: no run directories found")
            continue
        meta, runs = _load_runs(run_dir)
        if not runs:
            print(f"Skipping {name}: no runs in meta.json")
            continue
        output = _make_figure(name, run_dir, meta, runs, args.dpi)
        print(f"Plot saved to {output}")
        all_results.append((_display_label(name, meta), runs))
        run_dirs.append(run_dir)
        plotted += 1

    by_ts: dict[str, list] = {}
    for (label, runs), run_dir in zip(all_results, run_dirs):
        by_ts.setdefault(os.path.basename(run_dir), []).append((label, runs))

    combined_dir = None
    for ts, group in sorted(by_ts.items()):
        if len(group) < 2:
            continue
        combined_dir = os.path.join(OUTPUT_DIR, ts)
        os.makedirs(combined_dir, exist_ok=True)
        for fn in (_make_combined_tps_bar, _make_combined_lat_bar):
            path = fn(group, args.dpi, combined_dir)
            if path:
                print(f"Combined plot saved to {path}")

    print(f"\nDone. Plotted {plotted} testbed(s).")
    _write_readme(testbeds, combined_dir)


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------

def cmd_parse(args):
    meta_path = os.path.join(args.input_dir, "meta.json")
    with open(meta_path) as f:
        meta = json.load(f)
    run_entries = meta.pop("runs")
    runs = []
    for entry in run_entries:
        with open(os.path.join(args.input_dir, entry["raw_file"])) as f:
            raw = f.read()
        runs.append({
            "clients": entry["clients"],
            "threads": entry["threads"],
            **parse_pgbench_output(raw),
        })
    result = {"meta": meta, "runs": runs}
    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Parsed results saved to {args.output}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap  = argparse.ArgumentParser(description="pgbench benchmark tool")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("provision", help="Provision testbed infrastructure via Terraform + Ansible")
    p.add_argument("testbed", nargs="?", default=None, help="Testbed name (default: all)")

    p = sub.add_parser("initialize", help="Initialize pgbench schema")
    p.add_argument("testbed", nargs="?", default=None, help="Testbed name (default: all)")

    p = sub.add_parser("run", help="Run pgbench client sweep")
    p.add_argument("testbed", nargs="?", default=None, help="Testbed name (default: all)")

    p = sub.add_parser("plot", help="Plot results from a previous run")
    p.add_argument("testbed", nargs="?", default=None, help="Testbed name (default: all)")
    p.add_argument("--dpi", type=int, default=120)

    p = sub.add_parser("parse", help="Parse raw output into results JSON")
    p.add_argument("--input-dir", default="results", help="Directory containing meta.json and raw/")
    p.add_argument("--output",    default="output/results.json")

    args = ap.parse_args()
    {
        "provision":  cmd_provision,
        "initialize": cmd_initialize,
        "run":        cmd_run,
        "plot":       cmd_plot,
        "parse":      cmd_parse,
    }[args.command](args)


if __name__ == "__main__":
    main()
