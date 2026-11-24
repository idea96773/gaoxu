export async function readFrozenReprint(api, {job, delivery}) {
  const report = await api('jobs/' + job.id + '/reprint-check')
  if (report.job !== job.id || report.revision !== job.revision || report.draft !== delivery.draft_id ||
      report.platform !== job.platform || report.account !== job.account || report.sha !== delivery.sha ||
      report.planned !== job.slot) {
    throw Error('任务信息已变化，请重新打开任务详情再核对。')
  }
  return report
}
