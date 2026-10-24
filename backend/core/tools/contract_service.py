"""Project-bound contract/scan capabilities; no native execution permission."""
from pathlib import Path
from backend.core.loop.agent_tool import AgentTool
from backend.core.loop.execution_contract import data_broker, HOST_COMPUTE
from backend.core.prompt_templates import load_prompt


def build_contract_tools(workspace):
    from backend.core.loop import tool_wrappers as handlers
    root = Path(workspace).absolute()
    def prepare(args):
        values = dict(args or {})
        values["_workspace"] = str(root)
        values["workspace_path"] = str(root)
        return values
    def bound(handler):
        def execute(args):
            if Path(args["_workspace"]).absolute() != root:
                return {"error": "GOVERNANCE_WORKSPACE_MISMATCH", "query_state": "failed"}
            return handler(args)
        return execute
    paths = {"workspace_path": {"type": "string", "description": "Compatibility field; owning workspace is supplied by runtime."}}
    definitions = {
        "contract_detect_drift": ("Detect bounded lexical project-contract drift; unavailable facts are not a clean check.",
            {**paths, "changed_files": {"type": "array", "maxItems": 512, "items": {"type": "string"}},
             "contract_path": {"type": "string"}}, [], data_broker("governance.contract_drift")),
        "contract_get_impact": (load_prompt("tools/contract_get_impact"),
            {**paths, "file_path": {"type": "string"}, "func_name": {"type": "string"}},
            ["file_path"], data_broker("governance.contract_impact")),
        "privacy_scan": ("Scan selected files for privacy risks using the publication scanner. A scan is evidence, not publication authorization.",
            {**paths, "file_list": {"type": "array", "maxItems": 512, "items": {"type": "string"}},
             "level": {"type": "integer", "minimum": 1, "maximum": 3}, "deep_scan": {"type": "boolean"}},
            ["file_list"], data_broker("governance.privacy_scan")),
        "contract_get_changed_symbols": (load_prompt("tools/contract_get_changed_symbols"),
            {"file_path": {"type": "string"}, "old_content": {"type": "string"}, "new_content": {"type": "string"}},
            ["file_path", "old_content", "new_content"], HOST_COMPUTE),
    }
    return {name: AgentTool(name=name, description=description,
                parameters={"type": "object", "properties": properties, "required": required},
                execute=bound(getattr(handlers, name)) if name != "contract_get_changed_symbols" else handlers.contract_get_changed_symbols,
                prepare_args=prepare if name != "contract_get_changed_symbols" else None,
                read_only=True, execution_contract=contract)
            for name, (description, properties, required, contract) in definitions.items()}
