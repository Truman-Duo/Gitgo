import {expect, test} from 'bun:test';
import {outcomeError} from './sendChat.js';

test('failed task displays the retained leaf cause and actual failing stage', () => {
  const error = (code: string, message: string, nested = true) => ({error: {
    code: 'ALIGNMENT_REVIEW_FAILED', message,
    details: nested ? {details: {failure: {code}}} : {failure: {code}},
  }});
  expect(outcomeError(error('PROVIDER_DNS_FAILED', '需求检查未完成：域名解析失败。')))
    .toBe('[PROVIDER_DNS_FAILED] 需求检查未完成：域名解析失败。');
  expect(outcomeError(error('ALIGNMENT_RESPONSE_JSON_INVALID', '需求检查返回的 JSON 无效。', false)))
    .toBe('[ALIGNMENT_RESPONSE_JSON_INVALID] 需求检查返回的 JSON 无效。');
  expect(outcomeError({error: {code: 'LLM_STREAM_FAILED', message: '旧记录原因未确认'}}))
    .toBe('[LLM_STREAM_FAILED] 旧记录原因未确认');
  expect(outcomeError({error:{code:'TASK_TREE_DEADLINE_EXCEEDED',message:'任务时间已用尽',
    details:{details:{provider_failure:{code:'PROVIDER_TIMEOUT'}}}}}))
    .toBe('[TASK_TREE_DEADLINE_EXCEEDED] 任务时间已用尽');
});
