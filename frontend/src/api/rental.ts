/** 租赁结算接口封装：登记（幂等）、退租、批次结算、改租、收款、台账概览。 */
import { fetchJson, request } from '@/api/client'

export type Bill = {
  id: number
  租赁单ID: number
  设备明细ID: number
  账单类型: '租金' | '租金重算' | '押金' | '押金退还'
  起始日期: string
  结束日期: string
  账期月份: string
  金额: number
  已收金额: number
  未结金额: number
  预收金额: number
  状态: '有效' | '已冲销'
  版本号: number
  变更序号: number | null
  批次号: string
  生成时间: string
  关联账单ID: number | null
  备注: string
  最近收款时间?: string
}

export type DepositPreview = {
  押金金额: number
  欠费金额: number
  预收租金: number
  违约金: number
  应退押金: number
  押金算法: string
  计算说明: string
}

export type LeaseItem = {
  id: number
  租赁单ID: number
  设备编号: string
  设备名称: string
  起租日期: string
  到期日期: string
  月租金额: number
  押金金额: number
  押金算法: string
  违约金比例: string
  实际退租日期: string | null
  状态: string
  变更记录: Array<Record<string, string | number>>
  租金应收: number
  租金已收: number
  租金未收: number
  租金预收: number
  押金已收: number
  应退押金: number
  已退押金: number
  待退押金: number
  押金测算: DepositPreview
  账单?: Bill[]
}

export type Lease = {
  id: number
  租赁单号: string
  承租方: string
  联系电话: string
  登记时间: string
  备注: string
  设备台数: number
  在租台数: number
  租金应收: number
  租金已收: number
  租金未收: number
  租金预收: number
  押金已收: number
  应退押金: number
  已退押金: number
  待退押金: number
  设备明细: LeaseItem[]
}

export type LedgerSummary = {
  租金应收: number
  租金已收: number
  租金未收: number
  租金预收: number
  押金已收: number
  应退押金: number
  已退押金: number
  待退押金: number
}

export type Ledger = {
  账单: Bill[]
  汇总: LedgerSummary
  在租台数: number
  设备总台数: number
  已退租台数: number
  待结算台数: number
  当前日期: string
}

export type OverviewCard = { label: string; value: number }

type ActionResponse = {
  ok: boolean
  message: string
  entry?: Record<string, unknown>
}

async function postAction(path: string, values: Record<string, unknown>): Promise<ActionResponse> {
  const response = await request(path, { method: 'POST', body: JSON.stringify({ values }) })
  const payload = (await response.json()) as ActionResponse
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，操作未生效`)
  }
  return payload
}

export const rentalApi = {
  overview: () => fetchJson<{ cards: OverviewCard[]; 租赁单: Lease[] }>('/api/rental/overview'),
  ledger: () => fetchJson<Ledger>('/api/rental/ledger'),
  leases: (keyword = '') =>
    fetchJson<{ items: Lease[] }>(`/api/rental/leases?${new URLSearchParams({ keyword })}`),
  lease: (id: number) => fetchJson<Lease & { 设备明细: LeaseItem[] }>(`/api/rental/leases/${id}`),
  bills: (params: Record<string, string | number | boolean>) =>
    fetchJson<{ items: Bill[] }>(`/api/rental/bills?${new URLSearchParams(String(params))}`),

  register: (values: Record<string, unknown>) => postAction('/api/rental/leases', values),
  registerReturn: (values: Record<string, unknown>) => postAction('/api/rental/returns', values),
  settle: (values: Record<string, unknown>) => postAction('/api/rental/settle', values),
  changeTerm: (itemId: number, values: Record<string, unknown>) =>
    postAction(`/api/rental/items/${itemId}/change-term`, values),
  pay: (billId: number, values: Record<string, unknown>) =>
    postAction(`/api/rental/bills/${billId}/payments`, values),
}
