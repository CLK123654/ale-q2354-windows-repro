from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def run(command: list[str], cwd: Path | None = None) -> str:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=120)
    if completed.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(command)}\n{completed.stdout}\n{completed.stderr}")
    return completed.stdout


def yaml_scalar(value: str) -> str:
    return json.dumps(value)


def vendor_version(chart_yaml: Path) -> str:
    match = re.search(r"^version:\s*(\S+)\s*$", chart_yaml.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise ValueError("vendor Chart version is missing")
    return match.group(1).strip('"\'')


def validate_inputs(catalog: dict, contract: dict) -> tuple[list[dict], list[str]]:
    components = catalog.get("components", [])
    aliases = [item.get("alias") for item in components]
    if not aliases or len(set(aliases)) != len(aliases):
        raise ValueError("component aliases must be nonempty and unique")
    declared = contract.get("dependency_aliases", [])
    if aliases != declared:
        raise ValueError("catalog and dependency aliases do not match")
    registry = catalog.get("registry", "")
    if not registry:
        raise ValueError("catalog registry is missing")
    for item in components:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get("digest", ""))):
            raise ValueError(f"invalid digest for {item.get('alias')}")
        port = item.get("port")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"invalid port for {item.get('alias')}")
    profile_names = []
    allowed = set(aliases)
    for profile in contract.get("profiles", []):
        name = profile.get("name")
        enabled = profile.get("enabled", [])
        if not name or name in profile_names or len(enabled) != len(set(enabled)) or not set(enabled) <= allowed:
            raise ValueError("invalid profile contract")
        profile_names.append(name)
    if not profile_names:
        raise ValueError("profile contract is empty")
    return components, aliases


def baseline_field(path: Path, field: str) -> str:
    match = re.search(rf"^{re.escape(field)}:\s*[\"']?([^\"'\n]+)[\"']?\s*$", path.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise ValueError(f"baseline {field} is missing")
    return match.group(1).strip()


def inspect_baseline(input_root: Path) -> tuple[str, str]:
    chart_yaml = input_root / "baseline" / "starter" / "Chart.yaml"
    values_text = (input_root / "baseline" / "starter" / "values.yaml").read_text(encoding="utf-8")
    template_text = (input_root / "baseline" / "starter" / "templates" / "deployment.yaml").read_text(encoding="utf-8")
    if "docker.io" not in values_text or ":latest" not in values_text or "all-in-one" not in template_text:
        raise ValueError("baseline does not contain the stated offline release issue")
    return baseline_field(chart_yaml, "name"), baseline_field(chart_yaml, "appVersion")


def write_chart(input_root: Path, chart: Path, catalog: dict, contract: dict, components: list[dict], aliases: list[str], chart_name: str, app_version: str) -> None:
    vendor_source = input_root / "vendor_sources" / "edge-component"
    if not (vendor_source / "Chart.yaml").is_file():
        raise ValueError("local vendor source is missing")
    version = vendor_version(vendor_source / "Chart.yaml")
    (chart / "vendor_sources").mkdir(parents=True)
    shutil.copytree(vendor_source, chart / "vendor_sources" / "edge-component")
    dependencies = "\n".join(
        "\n".join(
            [
                "  - name: edge-component",
                f"    alias: {alias}",
                f"    version: {version}",
                "    repository: file://vendor_sources/edge-component",
                f"    condition: components.{alias}.enabled",
            ]
        )
        for alias in aliases
    )
    (chart / "Chart.yaml").write_text(
        "\n".join(
            [
                "apiVersion: v2",
                f"name: {chart_name}",
                "description: Offline package for port edge stations",
                "type: application",
                "version: 1.0.0",
                f"appVersion: {yaml_scalar(app_version)}",
                "dependencies:",
                dependencies,
                "",
            ]
        ),
        encoding="utf-8",
    )
    values = ["components:"]
    for alias in aliases:
        values.extend([f"  {alias}:", "    enabled: false"])
    for item in components:
        alias = item["alias"]
        values.extend(
            [
                f"{alias}:",
                f"  nameOverride: {alias}",
                "  image:",
                f"    repository: {catalog['registry']}/{item['repository']}",
                f"    digest: {item['digest']}",
                "  service:",
                f"    port: {item['port']}",
            ]
        )
    (chart / "values.yaml").write_text("\n".join(values) + "\n", encoding="utf-8")
    toggle = {
        "type": "object",
        "additionalProperties": False,
        "required": ["enabled"],
        "properties": {"enabled": {"type": "boolean"}},
    }
    component = {
        "type": "object",
        "additionalProperties": False,
        "required": ["nameOverride", "image", "service"],
        "properties": {
            "nameOverride": {"type": "string", "minLength": 1},
            "image": {
                "type": "object",
                "additionalProperties": False,
                "required": ["repository", "digest"],
                "properties": {
                    "repository": {
                        "type": "string",
                        "pattern": "^" + re.escape(catalog["registry"] + "/") + "[a-z0-9][a-z0-9/._-]*$",
                    },
                    "digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                },
            },
            "service": {
                "type": "object",
                "additionalProperties": False,
                "required": ["port"],
                "properties": {"port": {"type": "integer", "minimum": 1, "maximum": 65535}},
            },
            "resources": {"type": "object"},
            "global": {"type": "object"},
        },
    }
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "required": ["components", *aliases],
        "properties": {
            "components": {
                "type": "object",
                "additionalProperties": False,
                "required": aliases,
                "properties": {alias: {"$ref": "#/definitions/toggle"} for alias in aliases},
            },
            **{alias: {"$ref": "#/definitions/component"} for alias in aliases},
        },
        "definitions": {"toggle": toggle, "component": component},
    }
    (chart / "values.schema.json").write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    profiles_dir = chart / "profiles"
    profiles_dir.mkdir()
    for profile in contract["profiles"]:
        enabled = set(profile["enabled"])
        lines = ["components:"]
        for alias in aliases:
            lines.extend([f"  {alias}:", f"    enabled: {'true' if alias in enabled else 'false'}"])
        (profiles_dir / f"{profile['name']}.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_rendered(path: Path) -> list[dict]:
    documents = []
    for raw in re.split(r"^---\s*$", path.read_text(encoding="utf-8"), flags=re.MULTILINE):
        kind_match = re.search(r"^kind:\s*(\S+)\s*$", raw, re.MULTILINE)
        if not kind_match:
            continue
        kind = kind_match.group(1)
        component_match = re.search(r"^\s+app\.kubernetes\.io/name:\s*(\S+)\s*$", raw, re.MULTILINE)
        name_match = re.search(r"^metadata:\s*\n\s+name:\s*(\S+)\s*$", raw, re.MULTILINE)
        image_match = re.search(r"^\s+image:\s*[\"']?([^\"'\s]+)[\"']?\s*$", raw, re.MULTILINE)
        port_match = re.search(r"^\s+(?:containerPort|port):\s*(\d+)\s*$", raw, re.MULTILINE)
        documents.append(
            {
                "kind": kind,
                "component": component_match.group(1) if component_match else "",
                "name": name_match.group(1) if name_match else "",
                "image": image_match.group(1) if image_match else "",
                "port": int(port_match.group(1)) if port_match else None,
            }
        )
    return documents


def write_handoff(output: Path, catalog: dict, contract: dict) -> None:
    components = {item["alias"]: item for item in catalog["components"]}
    rows = []
    for profile in contract["profiles"]:
        documents = parse_rendered(output / "rendered" / f"{profile['name']}.yaml")
        by_key = {(item["kind"], item["component"]): item for item in documents}
        for alias in profile["enabled"]:
            deployment = by_key.get(("Deployment", alias))
            service = by_key.get(("Service", alias))
            expected = components[alias]
            image = f"{catalog['registry']}/{expected['repository']}@{expected['digest']}"
            if not deployment or not service:
                raise ValueError(f"rendered workload pair missing for {profile['name']} {alias}")
            if deployment["image"] != image or deployment["port"] != expected["port"] or service["port"] != expected["port"]:
                raise ValueError(f"rendered values do not match catalog for {profile['name']} {alias}")
            rows.append(
                [
                    profile["name"],
                    alias,
                    deployment["name"],
                    service["name"],
                    deployment["image"],
                    service["port"],
                ]
            )
    reports = output / "reports"
    reports.mkdir()
    with (reports / "site_handoff.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["profile", "component", "deployment", "service", "image", "service_port"])
        writer.writerows(rows)


def build(input_root: Path, output: Path, helm: str) -> None:
    required = [
        input_root / "catalog" / "component_catalog.json",
        input_root / "profiles" / "profile_contract.json",
        input_root / "baseline" / "starter" / "Chart.yaml",
        input_root / "baseline" / "starter" / "values.yaml",
        input_root / "baseline" / "starter" / "templates" / "deployment.yaml",
        input_root / "vendor_sources" / "edge-component" / "Chart.yaml",
    ]
    if not all(path.is_file() for path in required):
        raise ValueError("required input material is missing")
    catalog = read_json(required[0])
    contract = read_json(required[1])
    components, aliases = validate_inputs(catalog, contract)
    chart_name, app_version = inspect_baseline(input_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix="delivery-") as temporary:
        stage = Path(temporary) / "output"
        chart = stage / "chart" / "harbor-edge-stack"
        chart.mkdir(parents=True)
        write_chart(input_root, chart, catalog, contract, components, aliases, chart_name, app_version)
        run([helm, "dependency", "build", str(chart)])
        rendered = stage / "rendered"
        rendered.mkdir()
        release = contract["render_release_name"]
        namespace = contract["render_namespace"]
        for profile in contract["profiles"]:
            values = chart / "profiles" / f"{profile['name']}.yaml"
            run([helm, "lint", str(chart), "--strict", "-f", str(values)])
            manifest = run([helm, "template", release, str(chart), "--namespace", namespace, "-f", str(values)])
            (rendered / f"{profile['name']}.yaml").write_text(manifest, encoding="utf-8", newline="\n")
        write_handoff(stage, catalog, contract)
        if output.exists():
            shutil.rmtree(output)
        shutil.move(str(stage), output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--helm", default="helm")
    args = parser.parse_args()
    build(args.input.resolve(), args.output.resolve(), args.helm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
