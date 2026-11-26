import assert from 'node:assert/strict';
import test from 'node:test';
import { asTask, canDeleteTask, taskResource } from '../src/workbench-tasks.ts';

const archive = { id: 'task/1', created_at: '2026-01-02T16:05:00', status: 'completed', selection: {}, summary: {} };

test('历史扫描在北京时间显示队列进度，后台撰写期间不可删除', () => {
  const task = asTask({ ...archive, runtime: { provider: 'openai', model: 'deepseek-flash' },
    auto_reports: { status: 'running', completed: 3, total: 8, items: [] } }, 'risk_scan');
  assert.equal(task.metadata, '01-03 00:05 · DeepSeek · 生成中 3/8');
  assert.equal(canDeleteTask(task), false);
});

test('成功、失败重试保留的旧报告与手动报告按行社去重', () => {
  const task = asTask({ ...archive, mode: 'pi-demo', letter_drafts: { a: {}, b: {} },
    auto_reports: { status: 'partial', items: [
      { org_id: 'a', status: 'completed', revision: 1 },
      { org_id: 'c', status: 'failed', revision: 2 },
      { org_id: 'd', status: 'failed', revision: null },
    ] } }, 'risk_scan');
  assert.equal(task.metadata, '01-03 00:05 · 演示 · 3 份报告');
  assert.equal(canDeleteTask(task), true);
  assert.equal(taskResource(task), '/api/risk-scans/task%2F1');
});

test('其他任务按归档模式显示，无报告字段时不虚报份数', () => {
  const code = asTask({ ...archive, selection: { code_result: { runtime: { provider: 'openai' } } } }, 'code');
  assert.equal(code.metadata, '01-03 00:05 · OpenAI');
  assert.equal(taskResource(code), '/api/code-runs/task%2F1');
  const monitor = asTask({ ...archive, status: 'running' }, 'monitor');
  assert.equal(monitor.metadata, '01-03 00:05');
  assert.equal(canDeleteTask(monitor), false);
  assert.equal(taskResource(monitor), '/api/runs/task%2F1');
});
