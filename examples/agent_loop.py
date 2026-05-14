#!/usr/bin/env python
"""Gitgo Reference Agent — suggest → confirm → execute 完整循环。

不依赖任何 LLM 库，不 import gitgo 模块。
所有调用通过 subprocess CLI，证明协议不依赖 Python 集成。
每个决策点有 input() 等待人工确认，体现 Human-in-the-Loop 约束。

用法:
    python examples/agent_loop.py <project_name>
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _gitgo(*args: str) -> subprocess.CompletedProcess:
    """调用 gitgo CLI，返回 CompletedProcess。"""
    cmd = [sys.executable, "-m", "gitgo"] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True)


def _gitgo_json(*args: str) -> dict | list:
    """调用 gitgo CLI 并解析 JSON 输出。"""
    proc = _gitgo(*args, "--json")
    if proc.returncode != 0:
        print(f"[ERROR] gitgo {' '.join(args)} 失败 (exit {proc.returncode})")
        if proc.stderr:
            print(f"  stderr: {proc.stderr.strip()}")
        sys.exit(1)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(f"[ERROR] 无法解析 gitgo 输出为 JSON:")
        print(proc.stdout)
        sys.exit(1)


def _gitgo_list() -> list[str]:
    """列出所有项目名。"""
    projects = _gitgo_json("--mode", "list")
    return [p["name"] for p in projects]


def human_triage_decision(incoming_changes: list[dict]) -> dict[int, str]:
    """展示 trial incoming changes，等待人对每条做三叉决策。"""
    print(f"\n{'='*60}")
    print(f"  TRIAGE — {len(incoming_changes)} 个 incoming changes 待审查")
    print(f"{'='*60}")

    decisions: dict[int, str] = {}
    for ic in incoming_changes:
        print(f"\n  [{ic['index']}] {ic.get('hash', '')[:12]}  {ic.get('message', '')[:80]}")
        print(f"      作者: {ic.get('author', '?')}  日期: {ic.get('date', '?')}")
        while True:
            choice = input("      决策 (a=accept / p=promote / d=discard): ").strip().lower()
            if choice in ("a", "accept"):
                decisions[ic["index"]] = "accept"
                break
            elif choice in ("p", "promote"):
                decisions[ic["index"]] = "promote"
                break
            elif choice in ("d", "discard"):
                decisions[ic["index"]] = "discard"
                break
            print("      请输入 a/p/d")
    return decisions


def human_grouping_decision(commits: list[dict]) -> list[dict]:
    """展示 workspace commits，等待人做分组决策。"""
    print(f"\n{'='*60}")
    print(f"  FORMALIZE — {len(commits)} 个 workspace commits 待分组")
    print(f"{'='*60}")

    for c in commits:
        files = c.get("files_changed", [])
        print(f"\n  [{c['index']}] {c.get('hash', '')[:12]} {c.get('type', '')}: {c.get('subject', '')[:80]}")
        for f in files[:5]:
            print(f"       {f.get('status', '?'):8s} {f.get('path', '?')}")
        if len(files) > 5:
            print(f"       ... 还有 {len(files) - 5} 个文件")

    print(f"\n  输入分组方案。每行一组: indices message")
    print(f"  示例: 0,1 feat: add login and dashboard")
    print(f"  或留空自动每个 commit 单独一组")

    groups = []
    group_num = 1
    while True:
        line = input(f"  Group {group_num} (空行结束): ").strip()
        if not line:
            break
        parts = line.split(" ", 1)
        if len(parts) < 2:
            print("  格式: indices message")
            continue
        try:
            indices = [int(x.strip()) for x in parts[0].split(",")]
        except ValueError:
            print("  indices 格式错误，应为逗号分隔的数字")
            continue
        groups.append({"indices": indices, "message": parts[1]})
        group_num += 1

    # 如果没有手动输入，默认每个 commit 单独一组
    if not groups:
        for c in commits:
            groups.append({
                "indices": [c["index"]],
                "message": f"{c.get('type', 'chore')}: {c.get('subject', '')}",
            })

    return groups


def run(project: str):
    """Gitgo reference agent — suggest → confirm → execute 完整循环。"""

    # Step 1: 获取当前状态
    print(f"\n{'─'*60}")
    print(f"  Gitgo Reference Agent — {project}")
    print(f"{'─'*60}")

    status = _gitgo_json("--mode", "status", "--project", project)
    print(f"  状态 : {status.get('stage', '?')}")
    ws = status.get("workspace", {})
    print(f"  工作区: {ws.get('entries_changed', 0)}/{ws.get('entries_total', 0)} 变更")

    next_action = status.get("semantic", {}).get("suggested_next_action", "idle")
    print(f"  建议: {next_action}")

    if next_action == "idle":
        print("\n  无事可做。")
        return

    # Step 2: 按建议执行
    if next_action in ("triage", "formalize"):
        suggest_type = "triage" if next_action == "triage" else "formalize"
        context = _gitgo_json("--mode", "suggest", "--suggest-type", suggest_type,
                              "--project", project)
        ctx = context.get("context", {})

        if suggest_type == "triage":
            incoming = ctx.get("incoming_changes", [])
            if not incoming:
                print("  无待审查的 incoming changes。")
            else:
                decisions = human_triage_decision(incoming)
                for idx, action in decisions.items():
                    print(f"  执行: trial --index {idx} --trial-action {action}")
                    result = _gitgo_json("--mode", "trial", "--project", project,
                                         "--index", str(idx), "--trial-action", action)
                    if result.get("result") == "ok":
                        print(f"    ✓ ok")
                    else:
                        print(f"    ✗ fail: {result}")

        elif suggest_type == "formalize":
            commits = ctx.get("commits", [])
            if not commits:
                print("  无待分组的 workspace commits。")
            else:
                groups = human_grouping_decision(commits)
                for g in groups:
                    indices_str = ",".join(str(i) for i in g["indices"])
                    print(f"  执行: formalize --indices {indices_str} --message \"{g['message']}\"")
                    result = _gitgo_json("--mode", "formalize", "--project", project,
                                         "--indices", indices_str, "--message", g["message"])
                    if result.get("result") == "ok":
                        print(f"    ✓ {result.get('commit', {}).get('message', '')}")
                    else:
                        print(f"    ✗ fail: {result}")

    elif next_action == "push":
        pass  # 落到 Step 3

    # Step 3: 执行 sync + push
    print(f"\n  ── Sync ──")
    sync_result = _gitgo_json("--mode", "sync", "--project", project)
    if sync_result.get("result") == "ok":
        print(f"  ✓ sync 完成")

        print(f"\n  ── Push ──")
        push_result = _gitgo_json("--mode", "push", "--project", project)
        if push_result.get("result") == "ok":
            print(f"  ✓ push 完成")
        else:
            warnings = push_result.get("warnings", [])
            if warnings:
                print(f"  ⚠ 安全检查警告:")
                for w in warnings:
                    print(f"    {w}")
                confirm = input("  仍要推送? (y/N): ").strip().lower()
                if confirm == "y":
                    push_result = _gitgo_json("--mode", "push", "--project", project,
                                              "--skip-security")
                    if push_result.get("result") == "ok":
                        print(f"  ✓ push 完成 (跳过安全检查)")
                    else:
                        print(f"  ✗ push 失败: {push_result}")
    else:
        print(f"  ✗ sync 失败: {sync_result}")

    print(f"\n{'─'*60}")
    print(f"  完成。")
    print(f"{'─'*60}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        # 无参数：列出项目并让用户选择
        try:
            projects = _gitgo_list()
        except SystemExit:
            sys.exit(1)

        if not projects:
            print("错误: 没有配置的项目。请先 gitgo config")
            sys.exit(1)

        print("可用项目:")
        for i, p in enumerate(projects):
            print(f"  [{i+1}] {p}")
        try:
            choice = input(f"选择项目 (1-{len(projects)}): ").strip()
            idx = int(choice) - 1
            if idx < 0 or idx >= len(projects):
                print("无效选择")
                sys.exit(1)
            project_name = projects[idx]
        except (ValueError, EOFError):
            print("取消")
            sys.exit(1)
    else:
        project_name = sys.argv[1]

    run(project_name)
