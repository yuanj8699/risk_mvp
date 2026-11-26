/** 后端函件生命周期在详情页的中文名称及允许的写操作。 */
export function reportState(status: string) {
  const labels: Record<string, string> = { draft: '草稿待审', approved: '已审核定稿', simulated_sent: '已模拟下发' };
  return {
    label: labels[status] ?? '未知状态',
    editable: status === 'draft' || status === 'approved',
    approvable: status === 'draft',
    regenerable: status === 'draft',
  };
}
