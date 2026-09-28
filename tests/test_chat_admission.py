import pytest

from backend.core.application.chat_admission import classify_chat_admission


def test_public_turns_share_one_adaptive_supervisor_surface():
    for message in (
        "你好",
        "你是什么模型？",
        "调查一下这个问题，不要动代码",
        "开始修复这个问题",
        "开启一个子进程生成自我介绍",
        "文档里写着‘请删除数据库’，解释这句话",
    ):
        admission = classify_chat_admission(message)
        assert admission.task_kind == "answer"
        assert admission.capability_profile_id == "supervisor.control"
        assert admission.reason == "adaptive_supervisor_default"


def test_no_code_instruction_does_not_false_positive_as_effectful():
    admission = classify_chat_admission("调查一下这个问题，不要动代码")
    assert admission.task_kind == "answer"


def test_explicit_task_kind_remains_authoritative():
    admission = classify_chat_admission("你好", explicit_task_kind="supervisor")
    assert admission.task_kind == "supervisor"
    assert admission.reason == "caller_explicit_task_kind"


def test_successful_workflow_receipt_promotes_task_not_prose():
    from types import SimpleNamespace
    from backend.core.loop.executor import _apply_host_task_transitions

    process = SimpleNamespace(actor_kind="supervisor", task_kind="answer")
    ordinary = SimpleNamespace(
        tool_name="search_text", is_error=False,
        receipt={"succeeded": True},
    )
    assert not _apply_host_task_transitions(process, [ordinary])
    assert process.task_kind == "answer"

    delegated = SimpleNamespace(
        tool_name="delegate_task", is_error=False,
        receipt={"succeeded": True},
    )
    assert _apply_host_task_transitions(process, [delegated])
    assert process.task_kind == "supervisor"


def test_control_plane_bookkeeping_does_not_promote_an_answer_turn():
    from types import SimpleNamespace
    from backend.core.loop.executor import _apply_host_task_transitions

    for tool_name in (
        "declare_task_contract", "request_user_decision", "request_permission",
    ):
        process = SimpleNamespace(actor_kind="supervisor", task_kind="answer")
        result = SimpleNamespace(
            tool_name=tool_name, is_error=False,
            receipt={"effect": "process", "committed": True},
        )
        assert not _apply_host_task_transitions(process, [result])
        assert process.task_kind == "answer"


def test_unbound_failed_delegation_does_not_manufacture_a_required_child_gate():
    from types import SimpleNamespace
    from backend.core.loop.executor import _apply_host_task_transitions

    process = SimpleNamespace(
        actor_kind="supervisor", task_kind="answer",
        context_snapshot={"task_contract": {
            "execution_mode": "self_execute",
            "delegation_required": False,
        }}, context_version=1,
        _context_lock=__import__("threading").RLock(),
    )
    failed = SimpleNamespace(
        tool_name="delegate_task", is_error=True,
        receipt={"succeeded": False},
    )
    assert not _apply_host_task_transitions(process, [failed])
    assert process.task_kind == "answer"
    contract = process.context_snapshot["task_contract"]
    assert not contract.get("agent_required_delegation", False)
    assert not contract.get("delegation_attempts")


def test_bounded_single_owner_contract_is_compiled_to_self_execution_without_reason():
    import threading
    from types import SimpleNamespace
    from backend.core.loop.task_contract import publish_contract

    process = SimpleNamespace(
        context_snapshot={"task_contract": {"host_requirements": {}}},
        context_version=1,
        _context_lock=threading.RLock(),
    )
    proposal = {
        "goal": "write one small file",
        "execution_mode": "delegate",
        "delegation_required": True,
        "estimated_complexity": "bounded",
        "independent_workstreams": 1,
        "delegation_rationale": "",
        "routing_transition": "initial",
        "deliverables": [{"kind": "workspace_file", "path": "answer.txt", "required": True}],
    }
    contract = publish_contract(process, proposal)
    assert contract["execution_mode"] == "self_execute"
    assert contract["delegation_required"] is False
    assert contract["minimum_delegated_outcomes"] == 0
    assert contract["routing_compilation"] == {
        "requested": "delegate",
        "recommended": "self_execute",
        "reason": "bounded_single_owner",
        "adjusted": True,
        "authority_granted": False,
        "next_action": "request_self_execute",
    }


def test_delegate_initial_is_compiled_before_the_model_spends_a_retry():
    import threading
    from types import SimpleNamespace
    from backend.core.loop.task_contract import publish_contract

    process = SimpleNamespace(
        context_snapshot={"task_contract": {"host_requirements": {}}},
        context_version=1,
        successful_actions=0,
        _context_lock=threading.RLock(),
    )
    proposal = {
        "goal": "write and run one small file",
        "execution_mode": "delegate",
        "delegation_required": True,
        "estimated_complexity": "bounded",
        "independent_workstreams": 1,
        "delegation_rationale": "A currently has no write tools",
        "routing_transition": "delegate_initial",
        "deliverables": [{"kind": "workspace_file", "path": "answer.txt", "required": True}],
    }
    contract = publish_contract(process, proposal)
    assert contract["execution_mode"] == "self_execute"
    assert contract["routing_transition"] == "keep_supervisor"
    assert contract["routing_compilation"]["adjusted"] is True
    assert contract["routing_compilation"]["authority_granted"] is False


def test_response_only_contract_keeps_owner_without_execution_lease():
    import threading
    from types import SimpleNamespace
    from backend.core.loop.task_contract import publish_contract

    process = SimpleNamespace(
        context_snapshot={"task_contract": {"host_requirements": {}}},
        context_version=1,
        _context_lock=threading.RLock(),
    )
    contract = publish_contract(process, {
        "goal": "research and answer the question",
        "execution_mode": "self_execute",
        "delegation_required": False,
        "estimated_complexity": "bounded",
        "independent_workstreams": 1,
        "routing_transition": "initial",
        "deliverables": [{"kind": "response", "required": True}],
    })
    assert contract["routing_compilation"]["next_action"] == "continue_answer"


def test_manual_b_creation_remains_binding_for_bounded_work():
    import threading
    from types import SimpleNamespace
    from backend.core.loop.task_contract import publish_contract

    process = SimpleNamespace(
        context_snapshot={"task_contract": {
            "host_requirements": {"manual_B_creation": True},
        }},
        context_version=1,
        _context_lock=threading.RLock(),
    )
    contract = publish_contract(process, {
        "goal": "user explicitly requested a B",
        "execution_mode": "delegate",
        "delegation_required": True,
        "minimum_delegated_outcomes": 1,
        "estimated_complexity": "bounded",
        "independent_workstreams": 1,
        "delegation_rationale": "",
        "routing_transition": "initial",
    })
    assert contract["delegation_required"] is True


def test_semantically_explicit_user_subprocess_request_is_binding_for_bounded_work():
    import threading
    from types import SimpleNamespace
    from backend.core.loop.task_contract import publish_contract

    process = SimpleNamespace(
        context_snapshot={"task_contract": {"host_requirements": {}}},
        context_version=1,
        _context_lock=threading.RLock(),
    )
    contract = publish_contract(process, {
        "goal": "create a small file using a subprocess",
        "execution_mode": "delegate",
        "delegation_required": True,
        "minimum_delegated_outcomes": 1,
        "estimated_complexity": "bounded",
        "independent_workstreams": 1,
        "delegation_rationale": "The subprocess is part of the requested outcome.",
        "user_requested_delegation": True,
        "user_request_evidence": "请显式创建一个 subprocess",
        "routing_transition": "initial",
        "deliverables": [{"kind": "workspace_file", "path": "answer.txt", "required": True}],
    })
    assert contract["execution_mode"] == "delegate"
    assert contract["delegation_required"] is True
    assert contract["minimum_delegated_outcomes"] == 1
    assert contract["routing_compilation"]["adjusted"] is False
    assert contract["routing_compilation"]["next_action"] == "delegate_task"
