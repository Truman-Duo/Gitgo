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
        elif actual != iface.signature:
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
            if isinstance(node, ast.FunctionDef) and node.name == symbol:
                args = [a.arg for a in node.args.args]
                return f"{node.name}({', '.join(args)})"
            if isinstance(node, ast.ClassDef) and node.name == symbol:
                return f"class {node.name}"
        return None
    except Exception:
        return None
