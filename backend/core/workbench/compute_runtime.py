"""Bind workbench inputs and publish only confirmed private execution outputs."""
import hashlib
import re
import time

from backend.core.errors import error_payload
from backend.core.loop.mechanism_lifecycle import fence
from backend.core.loop.workspace_candidate_runtime import WorkspaceCandidateRuntime
from backend.core.sandbox import SandboxDenied
from .data import MAX_INPUT_BYTES, encode_json, parse_json, pointer, snapshot_json, text_digest
from .sources import applicable_as_declared

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,47}")


class WorkbenchComputeRuntime(WorkspaceCandidateRuntime):
    def _inputs(self, entries):
        from backend.core.loop.protocol_tools.workbench import bound_sources
        sources = bound_sources(self.ctx.process, self.workspace)
        if sources is None:
            raise ValueError("Workbench requires Runtime-bound storage and workspace")
        values, provenance = {}, {}
        for entry in entries:
            name = entry["name"]
            if not _NAME.fullmatch(name) or name in values:
                raise ValueError("Input names must be unique identifiers")
            selector = entry.get("selector", "")
            if ("cell" in entry) == ("locator" in entry):
                raise ValueError("Each input requires one pinned cell or tool result locator")
            if "cell" in entry:
                value, evidence = sources.compute_input(entry["cell"], selector)
            else:
                locator = entry["locator"]
                raw = sources._read_result(self.storage, locator, self.owner[0])
                snapshot, redacted = snapshot_json(raw)
                parsed = parse_json(snapshot)
                value = encode_json(pointer(parsed, selector))
                evidence = {"locator": locator, "selector": selector, "sha256": text_digest(value),
                            "redacted": redacted, "applicability": sources.assess_record(parsed)}
            values[name], provenance[name] = value, {**evidence, "basis": entry.get("basis", "current")}
        if len(encode_json(values).encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError("Combined workbench inputs exceed 120000 bytes")
        return values, provenance

    def run(self, args, *, timeout, diagnostics):
        deadline = time.monotonic() + timeout
        try:
            from jsonschema import Draft202012Validator
            args = {key: value for key, value in args.items() if not key.startswith("_")}
            problem = next(Draft202012Validator(self.tool.parameters).iter_errors(args), None)
            if problem is not None:
                raise ValueError("Arguments do not match the workbench execution schema at " +
                                 (".".join(str(part) for part in problem.path) or "the selected operation"))
            if args["operation"] == "code_size":
                from .code_size import scan
                return scan(self, args, timeout=timeout, diagnostics=diagnostics)
            values, provenance = self._inputs(args["inputs"])
            outputs = {}
            for entry in args["outputs"]:
                name = entry["name"]
                if not _NAME.fullmatch(name) or name in outputs:
                    raise ValueError("Output names must be unique identifiers")
                outputs[name] = entry.get("unit", "")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SandboxDenied("TOOL_TIMEOUT", "Input resolution exhausted the workbench time budget")
            frame = self.preview("workbench_python", {
                "inputs_json": values, "output_names": list(outputs), "code": args["code"],
            }, remaining, trusted_arguments={}, copy_inputs=False, diagnostics=diagnostics)
            result = self._confirmed(frame)
            if result.get("error"):
                return error_payload("WORKBENCH_INVALID_DATA", message=result.get("detail") or "Python calculation failed")
            if result["outputs"].keys() != outputs.keys():
                raise ValueError("Native output names changed")
            stored = {name: {"value": parse_json(result["outputs"][name]["json"]), "unit": unit,
                             "numeric_modes": result["outputs"][name]["numeric_modes"]}
                      for name, unit in outputs.items()}
            if any(not isinstance(item["numeric_modes"], list) or
                   not set(item["numeric_modes"]).issubset({"integer", "decimal", "binary_float"})
                   for item in stored.values()):
                raise ValueError("Invalid native numeric modes")
            state = "recorded"
            if any(e["redacted"] or not applicable_as_declared(e["applicability"], e["basis"])
                   for e in provenance.values()):
                state = "limited"
            record = {"kind": "gitgo.workbench.python.v1", "snapshot_state": state,
                      "program_sha256": hashlib.sha256(args["code"].encode("utf-8")).hexdigest(),
                      "inputs": provenance, "outputs": stored,
                      "coverage": "One Python execution over the declared inputs. Floating-point outputs may be approximate."}
            return self.publish(record, {"outputs": list(outputs)})
        except SandboxDenied as exc:
            return exc.result()
        except (KeyError, OSError, TypeError, ValueError, ArithmeticError) as exc:
            return error_payload("WORKBENCH_INVALID_DATA", message=str(exc)[:500])

    def _confirmed(self, frame):
        producer = frame["producer"]
        if (producer.get("launch_state") != "started" or producer.get("exit_code") != 0
                or not producer.get("access_retired") or producer.get("cleanup", {}).get("complete") is not True):
            raise SandboxDenied("SANDBOX_CLEANUP_PENDING", "Workbench producer retirement was not confirmed")
        return frame["result"]

    def publish(self, record, summary, *, scan_version=None):
        snapshot, redacted = snapshot_json(encode_json(record))
        if redacted:
            record = parse_json(snapshot)
            record["snapshot_state"] = "limited"
            record["redacted"] = True
            snapshot, _ = snapshot_json(encode_json(record))
        with fence(self.ctx.process):
            if self.ctx.is_cancelled() or self.ctx.process.cancel_requested or not self.is_current():
                raise SandboxDenied("TOOL_CANCELLED", "Task changed or cancelled before publishing the workbench result")
            result = self.storage.put_tool_result(snapshot, process_id=self.owner[0],
                task_id=self.owner[1], tool_name=self.tool.name)
            if scan_version:
                from .code_size import SCAN_VERSIONS
                scope, digest, observed_at = scan_version

                def update(context):
                    versions = dict(context.get(SCAN_VERSIONS) or {})
                    if (versions.get(scope) or {}).get("observed_at", 0) > observed_at:
                        return context
                    versions.pop(scope, None)
                    versions[scope] = {"digest": digest, "state": record["snapshot_state"], "observed_at": observed_at}
                    return {**context, SCAN_VERSIONS: dict(list(versions.items())[-32:])}

                self.ctx.process.update_context_snapshot(update)
        return {**summary, "result_locator": result["locator"], "snapshot_state": record["snapshot_state"]}
