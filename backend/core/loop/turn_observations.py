"""Adapt local observations to the canonical task observer, retaining origin IDs.

Persistent facts still commit through StorageRuntime/outbox. This adapter does
not acknowledge commits or catch failures from authoritative task barriers.
"""


class TurnObservations:
    def __init__(self, process, frontend):
        self.process, self.frontend = process, frontend

    def publish(self, event, *, detail=None):
        if self.frontend is None:
            return
        payload = dict(event)
        payload.setdefault('process_id', self.process.process_id)
        payload.setdefault('task_id', self.process.active_task_id or self.process.process_id)
        payload.setdefault('step', self.process.steps_used + 1)
        if detail is not None:
            payload['_trace_detail'] = detail
        self.frontend(payload)

    def failed(self, failure):
        from .engineering_workflow import EngineeringWorkflow
        EngineeringWorkflow(self.process, self.frontend).notice('EVENT_DELIVERY_FAILED',
            '内部事件的一位订阅者处理失败；其他订阅者继续运行，失败已记录。', **failure)

    def wire(self, bus, transcript):
        def completed(event):
            for result in event.results or []:
                transcript.append_tool_call(self.process.steps_used, result.tool_name, {},
                    {'allowed': result.allowed, 'error': result.error}, result.duration_ms)

        def composite(event):
            self.publish({'event': 'composite_step_result', 'execution_id': event.execution_id,
                          'tool_name': event.tool_name, **dict(event.data or {}),
                          'origin_signal': dict(event.signal)})

        def notice(event):
            self.publish({'event': 'progress_summary', 'phase': 'tool_notice',
                          **dict(event.data or {}), 'origin_signal': dict(event.signal)})

        bus.subscribe('ExecutionCompleted', completed)
        bus.subscribe('CompositeStepCompleted', composite)
        bus.subscribe('ToolNotice', notice)
