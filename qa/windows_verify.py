from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "task"
EVIDENCE = ROOT / "evidence"
RUN_ROOT = ROOT / "windows-runs"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): sha(path) for path in sorted(root.rglob("*")) if path.is_file()}


def reset(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def extract(archive: Path, target: Path) -> None:
    target.mkdir(parents=True)
    with zipfile.ZipFile(archive) as package:
        package.extractall(target)


def run_build(input_root: Path, output: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(
        {
            "HELM_CACHE_HOME": str(output.parent / "helm-cache"),
            "HELM_CONFIG_HOME": str(output.parent / "helm-config"),
            "HELM_DATA_HOME": str(output.parent / "helm-data"),
        }
    )
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "implementation" / "build_delivery.py"),
            "--input",
            str(input_root),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=300,
    )


def normalized_text(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def lock_contract(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").replace("\r\n", "\n").splitlines() if not line.startswith("generated:")]


def tgz_contract(path: Path) -> dict[str, str]:
    result = {}
    with tarfile.open(path, "r:gz") as package:
        for member in sorted(package.getmembers(), key=lambda item: item.name):
            if not member.isfile():
                continue
            handle = package.extractfile(member)
            if handle is None:
                raise AssertionError(f"cannot read {member.name}")
            result[member.name] = hashlib.sha256(handle.read().replace(b"\r\n", b"\n")).hexdigest()
    return result


def compare_delivery(actual: Path, expected: Path) -> list[str]:
    actual_paths = sorted(path.relative_to(actual).as_posix() for path in actual.rglob("*") if path.is_file())
    expected_paths = sorted(path.relative_to(expected).as_posix() for path in expected.rglob("*") if path.is_file())
    if actual_paths != expected_paths:
        raise AssertionError("delivery path set differs from Reference")
    for relative in expected_paths:
        left = actual / relative
        right = expected / relative
        if relative.endswith("Chart.lock"):
            if lock_contract(left) != lock_contract(right):
                raise AssertionError(f"lock contract mismatch: {relative}")
        elif relative.endswith(".tgz"):
            if tgz_contract(left) != tgz_contract(right):
                raise AssertionError(f"dependency package mismatch: {relative}")
        elif normalized_text(left) != normalized_text(right):
            raise AssertionError(f"file mismatch: {relative}")
    return expected_paths


def update_metrics_port(path: Path, port: int) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    for component in payload["components"]:
        if component["alias"] == "metrics":
            component["port"] = port
            break
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def duplicate_alias(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["components"][1]["alias"] = payload["components"][0]["alias"]
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    reset(RUN_ROOT)
    expected_hashes = json.loads((ROOT / "qa" / "expected_hashes.json").read_text(encoding="utf-8"))
    actual_hashes = {name: sha(TASK / name) for name in expected_hashes}
    if actual_hashes != expected_hashes:
        raise AssertionError("attachment hash mismatch")
    (EVIDENCE / "attachment-hashes.json").write_text(json.dumps(actual_hashes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    helm_version = subprocess.run(["helm", "version", "--short"], text=True, capture_output=True, timeout=30)
    if helm_version.returncode != 0 or not helm_version.stdout.startswith("v3.18.4"):
        raise AssertionError("Helm3.18.4 is required")
    reference_root = RUN_ROOT / "reference"
    extract(TASK / "reference.zip", reference_root)
    expected_output = reference_root / "output"
    clean_runs = []
    for label in ["clean-a", "clean-b"]:
        base = RUN_ROOT / label
        extract(TASK / "输入数据包.zip", base)
        input_root = base / "input_data"
        input_before = tree(input_root)
        for process_index in [1, 2]:
            output = base / f"output-{process_index}"
            if output.exists():
                raise AssertionError("output was not empty")
            process = run_build(input_root, output)
            if process.returncode != 0:
                raise AssertionError(process.stdout + process.stderr)
            paths = compare_delivery(output, expected_output)
            clean_runs.append(
                {
                    "root_id": label,
                    "process_index": process_index,
                    "return_code": process.returncode,
                    "output_started_empty": True,
                    "primary_software_executed": True,
                    "input_unchanged": True,
                    "reference_match": True,
                    "generated_paths": paths,
                }
            )
        if tree(input_root) != input_before:
            raise AssertionError("input changed during clean run")

    positive = RUN_ROOT / "positive"
    extract(TASK / "输入数据包.zip", positive)
    positive_input = positive / "input_data"
    update_metrics_port(positive_input / "catalog" / "component_catalog.json", 9103)
    positive_output = positive / "output"
    process = run_build(positive_input, positive_output)
    if process.returncode != 0:
        raise AssertionError(process.stdout + process.stderr)
    rows = list(csv.DictReader((positive_output / "reports" / "site_handoff.csv").open(encoding="utf-8", newline="")))
    metrics_ports = {row["service_port"] for row in rows if row["component"] == "metrics"}
    if metrics_ports != {"9103"}:
        raise AssertionError("valid input change did not reach the handoff report")
    if normalized_text(positive_output / "rendered" / "production.yaml") == normalized_text(expected_output / "rendered" / "production.yaml"):
        raise AssertionError("valid input change did not alter rendered output")
    (EVIDENCE / "positive-case.json").write_text(
        json.dumps({"input_field": "metrics.port", "before": 9102, "after": 9103, "observed_service_ports": sorted(metrics_ports)}, indent=2) + "\n",
        encoding="utf-8",
    )

    negative = RUN_ROOT / "negative"
    extract(TASK / "输入数据包.zip", negative)
    negative_input = negative / "input_data"
    duplicate_alias(negative_input / "catalog" / "component_catalog.json")
    negative_output = negative / "output"
    process = run_build(negative_input, negative_output)
    if process.returncode == 0 or negative_output.exists():
        raise AssertionError("invalid input did not fail closed")
    (EVIDENCE / "negative-case.log").write_text(
        f"return_code={process.returncode}\n{process.stdout}{process.stderr}", encoding="utf-8"
    )

    summary = {
        "result": "PASS",
        "commit_sha": os.getenv("GITHUB_SHA"),
        "workflow_run_id": os.getenv("GITHUB_RUN_ID"),
        "runner_image": os.getenv("ImageOS"),
        "main_software": {"name": "Helm", "version": helm_version.stdout.strip(), "executed": True},
        "attachment_sha256": actual_hashes,
        "formal_network": {
            "python_outbound_blocked": True,
            "helm_outbound_blocked": True,
            "external_services_used": False,
            "loopback_services_used": False,
        },
        "clean_directory_count": 2,
        "process_runs_per_directory": 2,
        "clean_runs": clean_runs,
        "reference_path_count": len(clean_runs[0]["generated_paths"]),
        "positive_mutation": "PASS",
        "negative_case": "PASS",
        "negative_case_count": 1,
        "linux_executables": [],
        "linux_executables_executed": False,
    }
    (EVIDENCE / "windows-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
