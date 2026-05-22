#!/usr/bin/env python3
"""
bench_run.py — pgbench sweep runner and result parser.

Sub-commands:
  sweep  Iterate tests/tests.yaml, SSH into each pgbench host, run a client
         sweep against the matching postgresql host, save raw output + meta.json
         to output/<testbed-name>/
  run    Run pgbench (local or SSH), saving raw output files + meta.json
  parse  Parse saved raw output into a results JSON file
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import yaml


REPO_ROOT   = os.path.dirname(os.path.abspath(__file__))
TESTS_YAML  = os.path.join(REPO_ROOT, "tests", "tests.yaml")
OUTPUT_DIR  = os.path.join(REPO_ROOT, "output")


_PROGRESS_RE = re.compile(
    r"progress:\s+([\d.]+)\s+s,\s+([\d.]+)\s+tps,\s+lat\s+([\d.]+)\s+ms\s+stddev\s+([\d.]+)"
)
# pgbench <17: "(excluding/including connections establishing)"
# pgbench 17+: "(without/with initial connection time)"
_TPS_EXCL_RE = re.compile(r"tps\s*=\s*([\d.]+)\s+\((?:excluding connections|without initial connection time)\)")
_TPS_INCL_RE = re.compile(r"tps\s*=\s*([\d.]+)\s+\((?:including connections|with initial connection time)\)")
_LAT_AVG_RE  = re.compile(r"latency average\s*=\s*([\d.]+)\s+ms")
_LAT_STD_RE  = re.compile(r"latency stddev\s*=\s*([\d.]+)\s+ms")
_TXN_RE      = re.compile(r"number of transactions actually processed:\s+(\d+)")


def parse_pgbench_output(text: str) -> dict:
    progress = []
    summary = {}
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


def build_pgbench_cmd(clients: int, threads: int, duration: int, conn: str,
                      pgbench_bin: str = "pgbench", password: str | None = None) -> str:
    prefix = f"PGPASSWORD={password} " if password else ""
    # 2>&1 merges stderr (progress lines) and stdout (summary) into a single stream
    return f"{prefix}{pgbench_bin} --client={clients} --jobs={threads} --time={duration} --progress=1 --no-vacuum {conn} 2>&1"


def make_local_executor():
    def exec_fn(cmd: str) -> str:
        proc = subprocess.run(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace",
        )
        return proc.stdout
    return exec_fn


def make_ssh_executor(ssh):
    def exec_fn(cmd: str) -> str:
        _stdin, stdout, _stderr = ssh.exec_command(cmd)
        return stdout.read().decode(errors="replace")
    return exec_fn


def parse_ansible_inventory(path: str) -> dict:
    """Parse a simple INI-style Ansible inventory.

    Returns a dict keyed by group name, each containing:
      "hosts": {hostname: {var: value, ...}}
      "vars":  {var: value, ...}
    """
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


def _pgbench_pass(exec_fn, clients_list: list[int], duration: int,
                  threads: int | None, conn_str: str, pgbench_bin: str,
                  collect: bool, output_dir: str | None,
                  password: str | None = None) -> list[dict]:
    """Run one pgbench pass over clients_list. Returns run entries when collect=True."""
    entries = []
    for clients in clients_list:
        t = threads if threads is not None else clients
        cmd = build_pgbench_cmd(clients, t, duration, conn_str, pgbench_bin, password)
        print(f"\n[{clients}c/{t}j]  {cmd}", file=sys.stderr)
        raw = exec_fn(cmd)
        for line in raw.splitlines():
            print(f"  {line}", file=sys.stderr)
        if collect and output_dir:
            raw_filename = os.path.join("raw", f"c{clients:03d}_j{t:03d}.txt")
            with open(os.path.join(output_dir, raw_filename), "w") as f:
                f.write(raw)
            entries.append({"clients": clients, "threads": t, "raw_file": raw_filename})
    return entries


def run_sweep(testbed: dict, run_cfg: dict, warmup_cfg: dict | None) -> None:
    import paramiko

    name = testbed["name"]
    inventory_path   = os.path.join(OUTPUT_DIR, name, "ansible_inventory")
    db_endpoint_path = os.path.join(OUTPUT_DIR, name, "db_endpoint")

    if not os.path.isfile(inventory_path):
        print(f"Skipping {name}: no inventory at {inventory_path}", file=sys.stderr)
        return
    if not os.path.isfile(db_endpoint_path):
        print(f"Skipping {name}: no db_endpoint file at {db_endpoint_path}", file=sys.stderr)
        return

    with open(db_endpoint_path) as f:
        postgresql_ip = f.read().strip()

    db_password_path = os.path.join(OUTPUT_DIR, name, "db_password")
    password = None
    if os.path.isfile(db_password_path):
        with open(db_password_path) as f:
            password = f.read().strip()

    inventory = parse_ansible_inventory(inventory_path)
    pgbench_hosts = inventory.get("pgbench", {}).get("hosts", {})

    if not pgbench_hosts:
        print(f"Skipping {name}: no [pgbench] hosts in inventory", file=sys.stderr)
        return

    pgbench_entry = next(iter(pgbench_hosts.items()))
    pgbench_ip    = pgbench_entry[1].get("ansible_host", pgbench_entry[0])

    all_vars = inventory.get("all", {}).get("vars", {})
    ssh_user = all_vars.get("ansible_user", "root")
    ssh_key  = all_vars.get("ansible_ssh_private_key_file")
    if ssh_key:
        ssh_key = os.path.expanduser(ssh_key)

    print(f"\n=== {name}: connecting to pgbench host {pgbench_ip} ===", file=sys.stderr)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs: dict = {"hostname": pgbench_ip, "port": 22, "username": ssh_user}
    if ssh_key:
        connect_kwargs["key_filename"] = ssh_key
    ssh.connect(**connect_kwargs)
    exec_fn = make_ssh_executor(ssh)

    output_dir = os.path.join(OUTPUT_DIR, name)
    raw_dir    = os.path.join(output_dir, "raw")
    os.makedirs(raw_dir, exist_ok=True)

    pgbench_version = testbed.get("pgbench_version", 18)
    pgbench_bin = f"/usr/lib/postgresql/{pgbench_version}/bin/pgbench"

    def conn_str(cfg: dict) -> str:
        parts = [f"--host={postgresql_ip}", f"--port={cfg['pg_port']}"]
        if cfg.get("pg_user"):
            parts.append(f"--username={cfg['pg_user']}")
        parts.append(cfg["dbname"])
        return " ".join(parts)

    try:
        if warmup_cfg:
            print(f"\n=== {name}: warmup ===", file=sys.stderr)
            warmup_clients = [int(c.strip()) for c in str(warmup_cfg["clients"]).split(",")]
            _pgbench_pass(exec_fn, warmup_clients, warmup_cfg["duration"],
                          warmup_cfg.get("threads"), conn_str(warmup_cfg),
                          pgbench_bin, collect=False, output_dir=None, password=password)

        print(f"\n=== {name}: run ===", file=sys.stderr)
        run_clients = [int(c.strip()) for c in str(run_cfg["clients"]).split(",")]
        run_entries = _pgbench_pass(exec_fn, run_clients, run_cfg["duration"],
                                    run_cfg.get("threads"), conn_str(run_cfg),
                                    pgbench_bin, collect=True, output_dir=output_dir,
                                    password=password)
    finally:
        ssh.close()

    meta = {
        "testbed":         name,
        "terraform":       testbed.get("terraform", {}),
        "pgbench_host":    pgbench_ip,
        "postgresql_host": postgresql_ip,
        "pgbench_version": pgbench_version,
        "pgbench": {
            "warmup": warmup_cfg,
            "run":    run_cfg,
        },
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "runs":            run_entries,
    }
    meta_path = os.path.join(output_dir, "meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"\nResults saved to {output_dir}/", file=sys.stderr)


def resolve_pgbench_config(global_cfg: dict, testbed_cfg: dict) -> dict:
    """Merge pgbench config: built-in defaults < global YAML < per-testbed YAML."""
    defaults = {
        "clients":  "1,2,4,8,16",
        "duration": 60,
        "threads":  None,
        "pg_user":  "postgres",
        "pg_port":  5432,
        "dbname":   "pgbench",
        "scale":    1,
    }
    return {**defaults, **{k: v for k, v in global_cfg.items() if k in defaults},
                         **{k: v for k, v in testbed_cfg.items() if k in defaults}}


def _initialize_testbed(testbed: dict, global_pgbench: dict) -> None:
    import paramiko

    name            = testbed["name"]
    testbed_pgbench = testbed.get("pgbench") or {}
    scale           = testbed_pgbench.get("scale") or global_pgbench.get("scale") or 1
    run_cfg         = resolve_pgbench_config(global_pgbench.get("run") or {},
                                             testbed_pgbench.get("run") or {})

    inventory_path   = os.path.join(OUTPUT_DIR, name, "ansible_inventory")
    db_endpoint_path = os.path.join(OUTPUT_DIR, name, "db_endpoint")

    if not os.path.isfile(inventory_path):
        print(f"Skipping {name}: no inventory at {inventory_path}", file=sys.stderr)
        return
    if not os.path.isfile(db_endpoint_path):
        print(f"Skipping {name}: no db_endpoint at {db_endpoint_path}", file=sys.stderr)
        return

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
        return

    pgbench_entry = next(iter(pgbench_hosts.items()))
    pgbench_ip    = pgbench_entry[1].get("ansible_host", pgbench_entry[0])
    all_vars      = inventory.get("all", {}).get("vars", {})
    ssh_user      = all_vars.get("ansible_user", "root")
    ssh_key       = all_vars.get("ansible_ssh_private_key_file")
    if ssh_key:
        ssh_key = os.path.expanduser(ssh_key)

    pgbench_version = testbed.get("pgbench_version", 18)
    pgbench_bin     = f"/usr/lib/postgresql/{pgbench_version}/bin/pgbench"

    conn_parts = [f"--host={postgresql_ip}", f"--port={run_cfg['pg_port']}"]
    if run_cfg.get("pg_user"):
        conn_parts.append(f"--username={run_cfg['pg_user']}")
    conn_parts.append(run_cfg["dbname"])
    conn = " ".join(conn_parts)

    prefix = f"PGPASSWORD={password} " if password else ""
    cmd    = f"{prefix}{pgbench_bin} --initialize --scale={scale} {conn} 2>&1"

    print(f"\n=== {name}: pgbench initialize (scale={scale}) ===", file=sys.stderr)
    print(f"  {cmd}", file=sys.stderr)

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs: dict = {"hostname": pgbench_ip, "port": 22, "username": ssh_user}
    if ssh_key:
        connect_kwargs["key_filename"] = ssh_key
    ssh.connect(**connect_kwargs)
    try:
        output = make_ssh_executor(ssh)(cmd)
        for line in output.splitlines():
            print(f"  {line}", file=sys.stderr)
    finally:
        ssh.close()


def cmd_initialize(args):
    with open(TESTS_YAML) as f:
        config = yaml.safe_load(f)

    testbeds       = config.get("testbeds") or []
    global_pgbench = config.get("pgbench") or {}

    if args.testbed and not any(t["name"] == args.testbed for t in testbeds):
        print(f"ERROR: testbed '{args.testbed}' not found in {TESTS_YAML}", file=sys.stderr)
        sys.exit(1)

    ran = 0
    for testbed in testbeds:
        if args.testbed and testbed["name"] != args.testbed:
            continue
        _initialize_testbed(testbed, global_pgbench)
        ran += 1

    print(f"\nDone. Initialized {ran} testbed(s).")


def cmd_sweep(args):
    with open(TESTS_YAML) as f:
        config = yaml.safe_load(f)

    testbeds = config.get("testbeds") or []
    global_pgbench = config.get("pgbench") or {}

    if args.testbed and not any(t["name"] == args.testbed for t in testbeds):
        print(f"ERROR: testbed '{args.testbed}' not found in {TESTS_YAML}", file=sys.stderr)
        sys.exit(1)

    ran = 0
    for testbed in testbeds:
        if args.testbed and testbed["name"] != args.testbed:
            continue
        if "terraform" not in testbed:
            print(f"Skipping {testbed['name']} (no terraform config)")
            continue
        testbed_pgbench = testbed.get("pgbench") or {}
        run_cfg     = resolve_pgbench_config(global_pgbench.get("run") or {},
                                             testbed_pgbench.get("run") or {})
        warmup_cfg  = resolve_pgbench_config(global_pgbench.get("warmup") or {},
                                             testbed_pgbench.get("warmup") or {}) \
                      if (global_pgbench.get("warmup") or testbed_pgbench.get("warmup")) \
                      else None
        run_sweep(testbed, run_cfg, warmup_cfg)
        ran += 1

    print(f"\nDone. Ran sweep for {ran} testbed(s).")


def cmd_parse(args):
    meta_path = os.path.join(args.input_dir, "meta.json")
    with open(meta_path) as f:
        meta = json.load(f)

    run_entries = meta.pop("runs")
    runs = []
    for entry in run_entries:
        raw_path = os.path.join(args.input_dir, entry["raw_file"])
        with open(raw_path) as f:
            raw = f.read()
        runs.append({
            "clients":  entry["clients"],
            "threads":  entry["threads"],
            "duration": meta["duration"],
            **parse_pgbench_output(raw),
        })

    result = {"meta": meta, "runs": runs}
    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Parsed results saved to {args.output}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="pgbench sweep runner and parser")
    sub = ap.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("initialize", help="Initialize pgbench schema for all testbeds or a single one")
    init_p.add_argument("testbed", nargs="?", default=None, help="Testbed name (default: all)")

    sweep_p = sub.add_parser("run", help="Run pgbench sweep for all testbeds in tests/tests.yaml")
    sweep_p.add_argument("testbed", nargs="?", default=None, help="Run only this testbed (default: all)")

    parse_p = sub.add_parser("parse", help="Parse raw output into results JSON")
    parse_p.add_argument("--input-dir", default="results",         help="Directory containing meta.json and raw/")
    parse_p.add_argument("--output",    default="output/results.json")

    args = ap.parse_args()

    if args.command == "initialize":
        cmd_initialize(args)
    elif args.command == "run":
        cmd_sweep(args)
    else:
        cmd_parse(args)


if __name__ == "__main__":
    main()
