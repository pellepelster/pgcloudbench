#!/usr/bin/env python3
"""
bench_deploy.py — provision testbed infrastructure via Terraform.

Iterates all testbeds defined in tests/tests.yaml. For each testbed that has
a `terraform` key, runs `terraform init` then `terraform apply` in the
corresponding terraform/<testbed-name>/ directory, passing key/value pairs
from `terraform.configuration` as -var arguments.
"""

import json
import os
import subprocess
import sys

import yaml


REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
TESTS_YAML = os.path.join(REPO_ROOT, "tests", "tests.yaml")
TERRAFORM_DIR = os.path.join(REPO_ROOT, "terraform")
OUTPUT_DIR = os.path.join(REPO_ROOT, "output")
ANSIBLE_PLAYBOOK = os.path.join(REPO_ROOT, "ansible", "playbook.yml")


def run(cmd: list[str], cwd: str) -> None:
    print(f"  $ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        print(f"ERROR: command failed with exit code {result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)


def deploy_testbed(testbed: dict) -> None:
    name      = testbed["name"]
    tf_config = testbed["terraform"]

    tf_dir = os.path.join(TERRAFORM_DIR, name)
    if not os.path.isdir(tf_dir):
        print(f"ERROR: terraform directory not found: {tf_dir}", file=sys.stderr)
        sys.exit(1)

    output_path = os.path.join(OUTPUT_DIR, name)
    os.makedirs(output_path, exist_ok=True)
    inventory_path = os.path.join(output_path, "ansible_inventory")
    db_address = (testbed.get("database") or {}).get("address")
    var_args = ["-var", f"output_path={output_path}"]
    if db_address:
        var_args += ["-var", f"db_address={db_address}"]
    for key, value in (tf_config.get("configuration") or {}).items():
        var_args += ["-var", f"{key}={value}"]

    print(f"\n=== {name}: terraform init ===")
    run(["terraform", "init"], cwd=tf_dir)

    print(f"\n=== {name}: terraform apply ===")
    run(["terraform", "apply", "-auto-approve"] + var_args, cwd=tf_dir)

    if db_address:
        with open(os.path.join(output_path, "db_endpoint"), "w") as f:
            f.write(db_address)

    if os.path.isfile(inventory_path):
        db_config = (testbed.get("database") or {}).get("configuration") or {}
        extra_vars = json.dumps({"postgresql_configuration": db_config})
        print(f"\n=== {name}: ansible-playbook ===")
        run(["ansible-playbook", "-i", inventory_path, ANSIBLE_PLAYBOOK,
             "-e", extra_vars], cwd=REPO_ROOT)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Provision testbed infrastructure via Terraform")
    ap.add_argument("testbed", nargs="?", default=None, help="Deploy only this testbed (default: all)")
    args = ap.parse_args()

    with open(TESTS_YAML) as f:
        config = yaml.safe_load(f)

    testbeds = config.get("testbeds") or []

    if args.testbed and not any(t["name"] == args.testbed for t in testbeds):
        print(f"ERROR: testbed '{args.testbed}' not found in {TESTS_YAML}", file=sys.stderr)
        sys.exit(1)

    deployed = 0
    for testbed in testbeds:
        name = testbed["name"]
        if args.testbed and name != args.testbed:
            continue
        if "terraform" not in testbed:
            print(f"Skipping {name} (no terraform config)")
            continue
        deploy_testbed(testbed)
        deployed += 1

    print(f"\nDone. Deployed {deployed} testbed(s).")


if __name__ == "__main__":
    main()
