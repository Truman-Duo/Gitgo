"""Check PR identity/format without executing PR-supplied shell text."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import subprocess


def check_title(title: str, config: dict, *, formal: bool = False) -> list[str]:
    pattern = r"(?:\[" + re.escape(config["projectPrefix"]) + r"-(\d+)\] )?([a-z]+)\(([a-z0-9_-]+)\): (.+)"
    match = re.fullmatch(pattern, title)
    if not match:
        return ["Title must use type(scope): subject (formal integration also requires its allocated prefix)."]
    number, kind, scope, subject = match.groups()
    errors = []
    if formal and not number:
        errors.append("Formal integration commit requires an allocated project number.")
    if kind not in config["validTypes"] or scope not in config["validScopes"]:
        errors.append("Commit type/scope is outside commit-config.json.")
    if len(subject) > config["maxSubjectLength"] or subject.endswith((".", "。")):
        errors.append("Subject exceeds the limit or ends with a period.")
    return errors


def field(body: str, names: tuple[str, ...]) -> str:
    label = "|".join(re.escape(name) for name in names)
    match = re.search(r"(?im)^\s*[-*]?\s*(?:" + label + r")\s*[:：]\s*([^\n]*)", body)
    return match.group(1).strip().strip("` ") if match else ""


def validate_pr(pr: dict, config: dict) -> list[str]:
    errors = check_title(pr.get("title", ""), config)
    body = re.sub(r"<!--.*?-->", "", pr.get("body") or "", flags=re.S)
    head = field(body, ("候选 head SHA", "候选 head", "Candidate head SHA", "Candidate head"))
    base = field(body, ("验证时的目标 base SHA", "验证基线", "Observed upstream SHA", "Target base SHA"))
    if head != pr["head"]["sha"]:
        errors.append("Candidate head field must match this PR head SHA; old evidence is historical only.")
    if base != pr["base"]["sha"]:
        errors.append("Validation base field must match the observed target SHA; align and update evidence.")
    source = field(body, ("源仓库与功能分支", "源", "Source repository and branch"))
    target = field(body, ("目标仓库与分支（通常为 Truman-Duo/Gitgo:master）", "目标仓库与分支", "目标", "Target repository and branch"))
    for value, info, label in ((source, pr["head"], "source"), (target, pr["base"], "target")):
        expected = info["repo"]["full_name"] + ":" + info["ref"]
        if value.replace("refs/heads/", "") != expected:
            errors.append(f"PR {label} field must name the actual repository and branch.")
    sections = (
        ("问题与目标", "行为与可信边界", "Problem and goal"),
        ("设计与权威边界", "行为与可信边界", "Authority and design"),
        ("验证", "Validation"),
        ("兼容、迁移与回滚", "部署与兼容", "Compatibility and rollback"),
        ("上下游与生命周期", "Alignment and lifecycle"),
        ("未解决风险", "未完成边界", "Remaining risks"),
    )
    parts = re.split(r"(?m)^##\s+", body)
    populated = {}
    for part in parts[1:]:
        heading, _, content = part.partition("\n")
        # Check presence of explanations, not checkbox truth or test counts.
        populated[heading.strip()] = bool(re.sub(r"[-\s\[\]xX]", "", content))
    for aliases in sections:
        if not any(populated.get(name) for name in aliases):
            errors.append("PR needs a populated section: " + aliases[0])
    return errors


def native_required(root: Path, base_root: Path) -> bool:
    marker = "backend/core/sandbox.py"
    required = (marker, "scripts/run_sandbox_acceptance.py", "scripts/run_linux_sandbox_scope.py",
                "tests/test_native_sandbox.py", "tests/test_windows_acl_provisioning.py")
    enabled = (root / marker).exists() or (base_root / marker).exists()
    if enabled:
        missing = [name for name in required if not (root / name).is_file()]
        if missing:
            raise ValueError("Native sandbox requires its real acceptance entrypoints: " + ", ".join(missing))
    return enabled


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--base-root", type=Path, required=True)
    args = parser.parse_args()
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    config = json.loads((args.base_root / "commit-config.json").read_text(encoding="utf-8"))
    errors = []
    if os.environ["GITHUB_EVENT_NAME"] == "pull_request":
        pr = event["pull_request"]
        errors += validate_pr(pr, config)
        checked = subprocess.run(["git", "merge-base", "--is-ancestor", pr["base"]["sha"], pr["head"]["sha"]], cwd=args.root)
        if checked.returncode:
            errors.append("Contributor head must incorporate the observed upstream before requesting review.")
    elif os.environ["GITHUB_EVENT_NAME"] == "push":
        title = subprocess.check_output(["git", "log", "-1", "--format=%s"], cwd=args.root, text=True).strip()
        errors += check_title(title, config, formal=True)
    try:
        enabled = native_required(args.root, args.base_root)
    except ValueError as exc:
        errors.append(str(exc))
        enabled = True
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"native_required={'true' if enabled else 'false'}\n")
    for error in errors:
        print(error)
    if errors:
        raise SystemExit(1)
    print("PR contract/current upstream checks passed; design approval remains a reviewer decision.")


if __name__ == "__main__":
    main()
