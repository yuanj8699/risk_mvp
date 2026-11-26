import assert from 'node:assert/strict';
import test from 'node:test';
import { reportState } from '../src/report-state.ts';

test('报告状态与修订、审核和重新生成权限一致', () => {
  assert.deepEqual(reportState('draft'), {
    label: '草稿待审', editable: true, approvable: true, regenerable: true,
  });
  assert.deepEqual(reportState('approved'), {
    label: '已审核定稿', editable: true, approvable: false, regenerable: false,
  });
  assert.deepEqual(reportState('simulated_sent'), {
    label: '已模拟下发', editable: false, approvable: false, regenerable: false,
  });
});
