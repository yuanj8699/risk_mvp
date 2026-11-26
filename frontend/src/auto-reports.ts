/** 将扫描归档的逐行社生成结果映射为总览文案，保留实际入选底线。 */
import type { AutoReportItem, AutoReports } from './scan-types';

export function reportBanner(progress: AutoReports | undefined, eligible: boolean) {
  if (!progress || progress.status === 'idle') {
    return eligible ? { text: '', action: '自动生成重点报告' } : null;
  }
  const { completed, failed, total, items } = progress;
  if (progress.status === 'running') {
    const names = items.filter(item => item.status === 'running').map(item => item.org_name);
    const writing = names.length ? names : progress.running_orgs ?? [];
    return { text: `风险报告自动生成中 · ${completed} / ${total} 家` +
      (writing.length ? `（正在撰写：${writing.join('、')}）` : ''), action: '' };
  }
  if (progress.status === 'completed') {
    const minimum = Math.min(...items.map(item => item.p0_count));
    const basis = items.length ? minimum >= 3 ? '（≥3 项监管底线）' :
      minimum >= 1 ? '（≥1 项监管底线）' : '（有效扫描信号）' : '';
    return { text: `已自动生成 ${completed} 份重点高风险行社风险报告${basis}`, action: '' };
  }
  return { text: `风险报告已生成 ${completed} 份 · 失败 ${failed} 家`, action: '重试失败项' };
}

export function reportMarker(item: AutoReportItem | undefined, hasReport: boolean) {
  if (item?.status === 'running') return '生成中…';
  if (item?.status === 'queued') return '排队中';
  if (item?.status === 'failed') return '失败';
  return hasReport || item?.status === 'completed' ? '已有报告' : '';
}
