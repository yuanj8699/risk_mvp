import assert from 'node:assert/strict';
import test from 'node:test';
import { BusinessMcpClient } from '../dist/mcp-business.js';
import { runRequest, toolStream } from '../dist/runtime.js';

const metric = { indicator_id: 'liquid_share', indicator_name: '流动性资产占比', dimension_id: 'liquidity_risk', dimension_title: '流动性风险',
  evidence_id: 'liquid-obs', value: '23.126', value_display: '23.126%', formal_display: '23.13%', unit: '%', data_date: '2026-09-30', rank: 77, population: 82 };
const asset = { ...metric, indicator_id: 'assets', indicator_name: '总资产', evidence_id: 'asset-obs', value: '23', value_display: '23亿元', formal_display: '23亿元', unit: '亿元', rank: null };
const clue = { ...metric, label: '流动性资产占比偏低', check_suggestion: '核对资产到期结构，逐月测算流动性缺口。', related_metrics: [] };
const request = { operation: 'draft_risk_letter', budget_ms: 120000, brief: { run_id: 'scan-a', org_id: 'org-a', org_name: '甲农商银行', region_name: '甲地',
  period_label: '2026年9月末', data_date: metric.data_date, issue_date: '2026年10月1日', business_overview: [asset], available_dimensions: [{dimension_id: metric.dimension_id, dimension_title: metric.dimension_title}],
  signal_overview: { p0_count: 1 }, mandatory_p0_evidence_ids: [metric.evidence_id], allowed_evidence_ids: [metric.evidence_id, asset.evidence_id, 'unread-obs', ...Array.from({length: 500}, (_, i) => 'history-' + i)] } };
const env = { RISK_PYTHON_EXECUTABLE: process.execPath, RISK_PROJECT_ROOT: process.cwd() };

// MCP 进程由 Python 联测覆盖。此处仅替代传输，Pi 工具派发、执行轨迹及提交校验按生产逻辑运行。
function fixture(t, floors = [clue], signals = []) {
  t.mock.method(BusinessMcpClient.prototype, 'connect', async () => {});
  const closed = t.mock.method(BusinessMcpClient.prototype, 'close', async () => {});
  t.mock.method(BusinessMcpClient.prototype, 'definitionsFor', names => names.map(name => ({name, inputSchema: {type: 'object', properties: {}, additionalProperties: true}})));
  t.mock.method(BusinessMcpClient.prototype, 'call', async (name, args) => {
    if (name === 'check_risk_rules') return { mandatory_p0_clues: floors, scan_signals: signals, cautions: [{message: '盈利同比正增长', forbidden_patterns: ['净利润下滑']}], evidence_ids: [clue.evidence_id] };
    if (name === 'fetch_org_metrics') return {metrics: [metric], evidence_ids: [metric.evidence_id]};
    if (name === 'run_verified_operator' && args.operator === 'stress_test') return {status: 'ok', result_type: 'simulated', rows: [{simulated_value: '99', evidence_ids: ['history-0']}], evidence_ids: ['history-0']};
    if (name === 'run_verified_operator') return {status: 'ok', result_type: 'observed_derived', summary: {indicator_id: metric.indicator_id, baseline_method: 'median', baseline_value: '31', unit: '%'},
      rows: [{evidence_id: metric.evidence_id}], evidence_ids: [metric.evidence_id, 'other-org']};
    assert.fail('未知工具 ' + name);
  });
  return closed;
}

test('demo 用真实 Pi 循环处理新增维度，记录三个只读工具及聚焦行社的证据', async t => {
  const closed = fixture(t);
  const result = await runRequest(request, { env });
  assert.match(result.letter.risk_sections[0].body, /23\.13%（全辖82家第77名）/);
  assert.match(result.letter.risk_sections[0].body, /中位数为31%/);
  assert.equal(result.letter.suggestions[0].body, clue.check_suggestion);
  assert.deepEqual(result.read_evidence_ids.sort(), [asset.evidence_id, metric.evidence_id]);
  const tools = result.trace.filter(e => e.event === 'tool');
  assert.deepEqual(tools.map(e => e.tool), ['check_risk_rules', 'fetch_org_metrics', 'run_verified_operator', 'submit_risk_letter']);
  assert.deepEqual(tools.slice(0, 3).map(e => [e.transport, e.evidence_ids]), Array.from({length: 3}, () => ['mcp', [metric.evidence_id]]));
  assert.equal(closed.mock.callCount(), 1);
});

test('撰函默认工具额度允许第 24 次调用提交终稿', async t => {
  fixture(t);
  const { letter } = await runRequest(request, { env });
  // 模拟模型每轮调用一个工具，单独放宽请求数，以验证默认工具额度。
  const streamFn = (model, context) => {
    const completed = context.messages.filter(message => message.role === 'toolResult').length;
    if (completed === 0) return toolStream(model, 'check_risk_rules', {});
    if (completed < 22) return toolStream(model, 'fetch_org_metrics', { indicator_ids: [metric.indicator_id] });
    if (completed === 22) return toolStream(model, 'run_verified_operator', { operator: 'benchmark_deviation', params: { indicator_id: metric.indicator_id, baseline: 'median' } });
    return toolStream(model, 'submit_risk_letter', letter);
  };
  const result = await runRequest(request, { env: { ...env, RISK_PI_MAX_REQUESTS: '24' }, streamFn });
  assert.equal(result.runtime.tool_calls, 24);
  assert.deepEqual(result.letter, letter);
});

test('普通筛查标签根据实际指标拟题，正文保留实测与排名', async t => {
  fixture(t, [], [{...clue, label: '内置参考筛查口径：变化方向需关注'}]);
  const { letter } = await runRequest({...request, brief: {...request.brief, mandatory_p0_evidence_ids: []}}, {env});
  assert.match(letter.risk_sections[0].heading, /流动性资产占比列全辖第77名/);
  assert.match(letter.risk_sections[0].body, /流动性资产占比为23\.13%/);
  assert.ok(!letter.risk_sections[0].body.includes(letter.risk_sections[0].heading));
  assert.doesNotMatch(JSON.stringify(letter), /内置参考筛查口径|变化方向需关注/);
});

test('model 契约允许未知维度及独立段数，并依据提交反馈纠正越界、未读、P0漏报及脏文案', async t => {
  fixture(t);
  const { letter } = await runRequest(request, { env });
  const corrected = { ...letter, suggestions: [...letter.suggestions, {heading: '跟踪整改进度', body: '每月复核期限匹配情况。'}] };
  const modelEnv = { ...env, RISK_PI_MODE: 'model', RISK_PI_PROVIDER: 'openai', RISK_PI_MODEL: 'gpt-4o', RISK_PI_API_KEY: 'test-only' };
  const cases = [
    [{ ...letter, appendix_evidence_ids: ['unread-obs'] }, /未实查/],
    [{ ...letter, risk_sections: [{...letter.risk_sections[0], evidence_ids: ['other-org']}] }, /越界/],
    [{ ...letter, appendix_evidence_ids: ['simulated-value'] }, /未实查/],
    [{ ...letter, risk_sections: [{...letter.risk_sections[0], evidence_ids: [asset.evidence_id]}] }, /漏报 P0/],
    [{ ...letter, closing_requirement: '测试' + 'a'.repeat(32) }, /哈希/],
    [{ ...letter, opening: '净利润下滑' }, /防误报/],
    [{ ...letter, overview: '证据：未脱敏' }, /禁用标签/],
    [{ ...letter, opening: '检查params中的参数' }, /内部指标码/],
    [{ ...letter, suggestions: Array(6).fill(letter.suggestions[0]) }, /./],
    [{ ...letter, risk_sections: [] }, /./],
  ];
  for (const [invalid, expected] of cases) {
    const streamFn = (model, context) => {
      const results = context.messages.filter(message => message.role === 'toolResult');
      if (!results.length) {
        assert.ok(Buffer.byteLength(context.messages.at(-1).content) < 2048);
        assert.doesNotMatch(context.messages.at(-1).content, /unread-obs/);
        return toolStream(model, 'check_risk_rules', {});
      }
      if (results.length === 1) return toolStream(model, 'run_verified_operator', { operator: 'stress_test', params: {} });
      if (results.length === 2) return toolStream(model, 'submit_risk_letter', invalid);
      assert.match(results.at(-1).content[0].text, expected);
      return toolStream(model, 'submit_risk_letter', corrected);
    };
    const result = await runRequest(request, { env: modelEnv, streamFn });
    assert.deepEqual(result.letter, corrected);
    assert.ok(result.read_evidence_ids.includes('history-0'));
    assert.ok(!result.read_evidence_ids.includes('99'));
    assert.deepEqual(result.trace.filter(e => e.tool === 'submit_risk_letter').map(e => e.status), ['error', 'ok']);
  }
});
