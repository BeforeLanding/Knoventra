import type { HelpItem } from './api'

export const HELP_STATUS: Record<string, string> = {
  OPEN: '待派单', ASSIGNED: '已派单', IN_PROGRESS: '处理中', RESOLVED: '已解决', CLOSED: '已关闭', CANCELLED: '已撤销',
}

/** Buttons follow the server's rules; the server still checks every action. */
export function allowedActions(item: HelpItem): string[] {
  const roles = new Set(item.my_roles)
  const out: string[] = []
  if (roles.has('assignee')) {
    if (item.status === 'ASSIGNED') out.push('start')
    if (item.status === 'IN_PROGRESS') out.push('progress', 'resolve')
  }
  if (roles.has('requester') && item.status === 'RESOLVED') out.push('close', 'reopen')
  if (roles.has('admin') && item.status === 'OPEN') out.push('assign')
  if (roles.has('admin') && item.status === 'ASSIGNED') out.push('unassign')
  if (roles.has('admin') && item.status === 'RESOLVED' && !out.includes('close')) out.push('close')
  if ((roles.has('requester') || roles.has('admin')) && ['OPEN', 'ASSIGNED', 'IN_PROGRESS'].includes(item.status)) {
    out.push('cancel')
  }
  return out
}
