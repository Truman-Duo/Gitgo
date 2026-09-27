"""InterfaceContract —— slot 间协作级接口契约。

采用方案 B——冻结当前 dep_graph 签名，不做前瞻性协商。
机制等同 contract.py 的 detect_drift，把作用域从"项目级合约"下沉到"slot 间协作级合约"。

流程：
1. freeze_contract: 从 dep_graph 提取交叉依赖文件的当前签名
2. 注入所有相关 slot 的 ContextSnapshot.needed
3. verify_contract: 执行后检测实际产出是否偏离冻结契约
4. 偏离 → EscalateToParent（父级决策，不是 B1↔B2 私聊）
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.loop.task_slot import TaskSlot


@dataclass
class InterfaceSpec:
    """冻结的单个接口签名。"""

    file: str             # 如 "auth.py"
    symbol: str           # 函数/类名
    signature: str        # 当前签名（从 dep_graph 提取，非预测）
    owner_slot: str       # 谁实现此接口（通常是被依赖方）
    consumers: list[str] = field(default_factory=list)
        # 谁依赖此接口


@dataclass
class InterfaceContract:
    """slot 间协作契约。"""

    contract_id: str
    frozen_at: str = ""         # ISO timestamp
    interfaces: list[InterfaceSpec] = field(default_factory=list)


@dataclass
class ContractViolation:
    """契约验证发现的偏差。"""

    interface: InterfaceSpec
    expected_signature: str
    actual_signature: str | None
    file: str
    violation_type: str         # "signature_changed" | "symbol_removed" | "file_missing"


def normalise_interface_ref(value: str) -> str:
    """Return one safe ``relative/path:symbol`` declaration."""
    raw = str(value or "").strip().replace("\\", "/")
    if ":" not in raw:
        raise ValueError(f"interface must use file:symbol syntax: {raw}")
    file_name, symbol = raw.rsplit(":", 1)
    while file_name.startswith("./"):
        file_name = file_name[2:]
    if (
        not file_name or not symbol
        or Path(file_name).is_absolute()
        or ".." in Path(file_name).parts
        or not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_.$-]*", symbol)
    ):
        raise ValueError(f"invalid interface declaration: {raw}")
    return f"{file_name}:{symbol}"


def capture_declared_contract(workspace_path: str | Path, nodes: list[dict]) -> dict:
    """Freeze declared cross-node interfaces from the admitted task snapshot.

    The result is JSON-safe and can be persisted in every child task contract.
    Unknown languages degrade to a presence check instead of guessing a hard
    signature.  This keeps false hard blocks lower without making the contract
    advisory.
    """
    workspace = Path(workspace_path).resolve()
    owners: dict[str, str] = {}
    consumers: dict[str, list[str]] = {}
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        for raw in node.get("output_interfaces") or []:
            ref = normalise_interface_ref(raw)
            if ref in owners and owners[ref] != node_id:
                raise ValueError(f"interface has multiple owners: {ref}")
            owners[ref] = node_id
        for raw in node.get("input_interfaces") or []:
            ref = normalise_interface_ref(raw)
            consumers.setdefault(ref, []).append(node_id)

    interfaces = []
    for ref in sorted(set(owners) | set(consumers)):
        file_name, symbol = ref.rsplit(":", 1)
        path = (workspace / file_name).resolve()
        try:
            path.relative_to(workspace)
        except ValueError as exc:
            raise ValueError(f"interface file escapes workspace: {file_name}") from exc
        present, signature = _inspect_declared_interface(path, symbol)
        interfaces.append({
            "ref": ref,
            "file": file_name,
            "symbol": symbol,
            "owner_node_id": owners.get(ref, ""),
            "consumer_node_ids": sorted(set(consumers.get(ref, []))),
            "initially_present": present,
            "frozen_signature": signature,
        })
    canonical = json.dumps(interfaces, ensure_ascii=False, sort_keys=True)
    return {
        "contract_id": hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
        "interfaces": interfaces,
    }


def verify_declared_contract(
    contract: dict | None,
    workspace_path: str | Path,
    references: list[str],
) -> list[dict]:
    """Verify required declared interfaces at a Host lifecycle boundary."""
    if not contract or not references:
        return []
    workspace = Path(workspace_path).resolve()
    by_ref = {
        str(item.get("ref") or ""): dict(item)
        for item in (contract.get("interfaces") or [])
    }
    violations = []
    for raw in references:
        ref = normalise_interface_ref(raw)
        item = by_ref.get(ref)
        if item is None:
            violations.append({"ref": ref, "type": "contract_entry_missing"})
            continue
        path = (workspace / str(item.get("file") or "")).resolve()
        try:
            path.relative_to(workspace)
        except ValueError:
            violations.append({"ref": ref, "type": "file_escape"})
            continue
        present, actual = _inspect_declared_interface(
            path, str(item.get("symbol") or "")
        )
        if not present:
            violations.append({
                "ref": ref,
                "type": "file_missing" if not path.is_file() else "symbol_missing",
                "expected": item.get("frozen_signature") or "present",
                "actual": None,
            })
            continue
        frozen = str(item.get("frozen_signature") or "")
        if bool(item.get("initially_present")) and frozen and actual != frozen:
            violations.append({
                "ref": ref,
                "type": "signature_changed",
                "expected": frozen,
                "actual": actual,
            })
    return violations


def _inspect_declared_interface(path: Path, symbol: str) -> tuple[bool, str]:
    if not path.is_file():
        return False, ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, ""
    if path.suffix.lower() in {".py", ".pyi"}:
        signature = _extract_current_signature_from_file(str(path), symbol)
        return signature is not None, signature or ""

    escaped = re.escape(symbol)
    patterns = (
        rf"(?m)^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
        rf"(?:function|class|interface|type|const|let|var)\s+{escaped}\b[^\r\n]*",
        rf"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+{escaped}\b[^\r\n{{]*",
        rf"(?m)^\s*func\s+(?:\([^)]*\)\s*)?{escaped}\b[^\r\n{{]*",
        rf"(?m)^\s*(?:(?:public|private|protected|internal|static|final|abstract|"
        rf"suspend|open|virtual|override)\s+)*(?:class|interface|enum|record|struct)\s+"
        rf"{escaped}\b[^\r\n{{]*",
        rf"(?m)^\s*(?:(?:public|private|protected|internal|static|final|virtual|"
        rf"override|async)\s+)+[^\r\n;{{}}]*\b{escaped}\s*\([^\r\n{{;]*",
    )
    for pattern in patterns:
        match = re.search(pattern, content)
        if match:
            signature = re.sub(r"\s+", " ", match.group(0)).strip()
            return True, signature[:1000]
    # For less common languages/config schemas, exact symbol presence is still
    # a useful hard existence contract.  Do not invent a signature.
    return bool(re.search(rf"(?<![A-Za-z0-9_$]){escaped}(?![A-Za-z0-9_$])", content)), ""


def freeze_contract(
    slots: list["TaskSlot"],
    dep_graph: dict,
) -> InterfaceContract | None:
    """从 dep_graph 提取交叉依赖文件的当前签名作为冻结契约。

    只处理有交叉依赖的 slot 对——即 B1 的 output_interfaces 与 B2 的
    input_interfaces 有交集。不涉及未来预测——只冻结当前事实。

    Returns:
        InterfaceContract 如果有交叉依赖，否则 None。
    """
    import uuid
    from datetime import datetime

    # 收集所有交叉依赖对：B1.output ∩ B2.input ≠ ∅
    interfaces: list[InterfaceSpec] = []
    slot_map = {s.slot_id: s for s in slots}

    for slot in slots:
        for upstream_id in slot.depends_on:
            upstream = slot_map.get(upstream_id)
            if not upstream:
                continue

            # 找到交叉的接口符号
            shared = (
                set(upstream.spec.output_interfaces)
                & set(slot.spec.input_interfaces)
            )
            for symbol_ref in shared:
                # 解析 "auth.py:authenticate" → file, symbol
                parts = symbol_ref.split(":", 1)
                file = parts[0]
                symbol = parts[1] if len(parts) > 1 else ""

                # 从 dep_graph 提取当前签名
                signature = _extract_current_signature(dep_graph, file, symbol)

                interfaces.append(InterfaceSpec(
                    file=file,
                    symbol=symbol,
                    signature=signature,
                    owner_slot=upstream_id,
                    consumers=[slot.slot_id],
                ))

    if not interfaces:
        return None

    return InterfaceContract(
        contract_id=str(uuid.uuid4())[:8],
        frozen_at=datetime.now().isoformat(),
        interfaces=interfaces,
    )


def verify_contract(
    contract: InterfaceContract,
    workspace_path: str,
) -> list[ContractViolation]:
    """检测实际产出是否偏离冻结契约。

    机制等同 contract.py 的 detect_drift——验证当前 workspace 中的
    文件是否仍满足冻结时的接口签名。

    Returns:
        ContractViolation 列表。空列表 = 无偏差。
    """
    from pathlib import Path

    violations: list[ContractViolation] = []
    ws = Path(workspace_path)

    for iface in contract.interfaces:
        file_path = ws / iface.file
        if not file_path.exists():
            violations.append(ContractViolation(
                interface=iface,
                expected_signature=iface.signature,
                actual_signature=None,
                file=iface.file,
                violation_type="file_missing",
            ))
            continue

        try:
            actual = _extract_current_signature_from_file(
                str(file_path), iface.symbol,
            )
        except Exception:
            actual = None

        if actual is None:
            violations.append(ContractViolation(
                interface=iface,
                expected_signature=iface.signature,
                actual_signature=None,
                file=iface.file,
                violation_type="symbol_removed",
            ))
        elif iface.signature != iface.symbol and actual != iface.signature:
            violations.append(ContractViolation(
                interface=iface,
                expected_signature=iface.signature,
                actual_signature=actual,
                file=iface.file,
                violation_type="signature_changed",
            ))

    return violations


def _extract_current_signature(
    dep_graph: dict,
    file: str,
    symbol: str,
) -> str:
    """从已缓存的 dep_graph 中提取指定符号的当前签名。

    dep_graph 结构（来自 contract.build_function_graph）：
    {filename: {defines: [...], called_by: {func: [...caller_refs]}}}
    """
    entry = dep_graph.get(file, {})
    defines = entry.get("defines", [])
    for d in defines:
        # defines 格式: "func_name(args)" 或 "func_name"
        if d.startswith(symbol):
            return d
    return symbol  # fallback: 只返回符号名（签名不可用）


def _extract_current_signature_from_file(
    file_path: str,
    symbol: str,
) -> str | None:
    """从实际文件中提取指定符号的当前签名（AST 解析）。

    用于 verify_contract——对比冻结签名 vs 实际文件。
    """
    import ast
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol:
                prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                returns = (
                    f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
                )
                return f"{prefix} {node.name}({ast.unparse(node.args)}){returns}"
            if isinstance(node, ast.ClassDef) and node.name == symbol:
                bases = ", ".join(ast.unparse(item) for item in node.bases)
                return f"class {node.name}" + (f"({bases})" if bases else "")
        return None
    except Exception:
        return None
