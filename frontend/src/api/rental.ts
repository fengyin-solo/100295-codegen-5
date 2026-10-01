/** 租赁结算接口封装：台账、退租、结算、变更、收退款共用同一份后端口径。 */
import { request } from './client'

const BASE = '/api/rental'

export type ActionResponse = {
  ok: boolean
  message: string
  entry?: LeaseRow | null
}

export type Summary = {
  应收租金: number
  已收租金: number
  未收租金: number
  已收押金: number
  应退押金: number
  已退押金: number
  未退押金: number
  在租台数: number
  租赁单数: number
  设备总台数: number
}

export type LeaseRow = {
  id: number
  租赁单号: string
  承租方: string
  起租日: string
  约定到期日: string
  押金规则: string
  押金规则说明?: string
  退还比例: number
  变更次数: number
  status: string
  pending: boolean
  abnormal: boolean
  应收租金: number
  已收租金: number
  未收租金: number
  已收押金: number
  应退押金: number
  已退押金: number
  未退押金: number
  设备总台数: number
  已退租台数: number
  在租台数: number
}

export type LeaseLine = {
  id: number
  lease_id: number
  行号: number
  设备编号: string
  设备名称: string
  月租金: number
  押金: number
  起租日: string
  约定到期日: string
  押金规则: string
  退还比例: number
  实退日期: string | null
  settled: boolean
}

export type BillRow = Record<string, string | number | null>

export type PaymentRow = Record<string, string | number | null>

export type AmendmentRow = Record<string, unknown>

export type LeaseDetail = {
  lease: LeaseRow
  lines: LeaseLine[]
  bills: BillRow[]
  payments: PaymentRow[]
  amendments: AmendmentRow[]
  summary: Record<string, number>
}

export type DepositRule = { name: string; ratio: number; desc: string }

async function postJson(path: string, body: unknown): Promise<ActionResponse> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，操作未生效`)
  }
  return (await response.json()) as ActionResponse
}

export async function fetchSummary(): Promise<Summary> {
  const response = await request(`${BASE}/summary`)
  if (!response.ok) throw new Error('租赁汇总读取失败')
  return (await response.json()) as Summary
}

export async function fetchDepositRules(): Promise<{ rules: DepositRule[]; overdue_daily_rate: number }> {
  const response = await request(`${BASE}/rules`)
  if (!response.ok) throw new Error('押金规则读取失败')
  return await response.json()
}

export async function fetchLeases(params: { keyword?: string; status?: string }): Promise<{ items: LeaseRow[]; total: number }> {
  const query = new URLSearchParams()
  if (params.keyword) query.set('keyword', params.keyword)
  if (params.status) query.set('status', params.status)
  query.set('size', '100')
  const response = await request(`${BASE}/leases?${query.toString()}`)
  if (!response.ok) throw new Error('租赁台账读取失败')
  return await response.json()
}

export async function fetchLeaseDetail(id: number): Promise<LeaseDetail> {
  const response = await request(`${BASE}/leases/${id}`)
  if (!response.ok) throw new Error('租赁单明细读取失败')
  return await response.json()
}

export function createLease(values: Record<string, unknown>): Promise<ActionResponse> {
  return postJson(`${BASE}/leases`, { values })
}

export function registerReturns(leaseId: number, values: Record<string, unknown>): Promise<ActionResponse> {
  return postJson(`${BASE}/leases/${leaseId}/returns`, { values })
}

export function settleLease(leaseId: number, values: Record<string, unknown>): Promise<ActionResponse> {
  return postJson(`${BASE}/leases/${leaseId}/settle`, { values })
}

export function amendLease(leaseId: number, values: Record<string, unknown>): Promise<ActionResponse> {
  return postJson(`${BASE}/leases/${leaseId}/amend`, { values })
}

export function registerPayment(leaseId: number, values: Record<string, unknown>): Promise<ActionResponse> {
  return postJson(`${BASE}/leases/${leaseId}/payments`, { values })
}
