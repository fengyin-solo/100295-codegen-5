<template>
  <section class="page" data-module="rental">
    <header class="page-head">
      <div>
        <h2>租赁结算台账</h2>
        <p class="page-desc">
          按设备与承租方登记租期、租金与押金；同一份租赁单重复提交只入账一次。一批设备同日退租，结算按设备逐台出月租，
          只结已退租、未退租留下一批。到期押金按登记时约定的算法退；中途改约定，已出账单保留原值、差额出调整账单。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记租赁单</button>
        <button class="btn" type="button" @click="exportRows">导出台账</button>
      </div>
    </header>

    <!-- 汇总：与 /api/rental/summary、运营概览同一份口径 -->
    <div class="stat-row">
      <article v-for="card in summaryCards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>租赁单号 / 承租方</span>
        <input v-model="filters.keyword" placeholder="按关键字检索" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th>租赁单号</th><th>承租方</th><th>租期</th><th>状态</th>
          <th>设备（在租/总数）</th><th>应收租金</th><th>已收租金</th><th>未收租金</th>
          <th>已收押金</th><th>应退押金</th><th>已退押金</th><th>变更</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="{ 'row-active': detail?.lease.id === row.id }">
          <td><button class="link" type="button" @click="openDetail(row.id)">{{ row.租赁单号 }}</button></td>
          <td>{{ row.承租方 }}</td>
          <td>{{ row.起租日 }} ~ {{ row.约定到期日 }}</td>
          <td><span :class="['tag', statusTag(row.status)]">{{ row.status }}</span><span v-if="row.abnormal" class="tag warn" title="有未收租金或未退押金">待核销</span></td>
          <td>{{ row.在租台数 }} / {{ row.设备总台数 }}</td>
          <td class="num">{{ money(row.应收租金) }}</td>
          <td class="num">{{ money(row.已收租金) }}</td>
          <td class="num" :class="{ 'text-warn': row.未收租金 > 0 }">{{ money(row.未收租金) }}</td>
          <td class="num">{{ money(row.已收押金) }}</td>
          <td class="num">{{ money(row.应退押金) }}</td>
          <td class="num">{{ money(row.已退押金) }}</td>
          <td>{{ row.变更次数 }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row.id)">台账</button>
            <button class="link" type="button" @click="openAction(row, 'return')">退租</button>
            <button class="link" type="button" @click="openAction(row, 'settle')">结算</button>
            <button class="link" type="button" @click="openAction(row, 'amend')">变更</button>
            <button class="link" type="button" @click="openAction(row, 'payment')">收退款</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="13" class="empty-state">暂无租赁单，点击右上角「登记租赁单」开始建账</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 张租赁单 · 应收/已收/在租台数与运营概览同源</span>
      <span v-if="message" :class="messageOk ? 'ok-text' : 'error-text'">{{ message }}</span>
    </footer>

    <!-- 登记租赁单（按设备逐台录租金与押金，单据号天然幂等） -->
    <div v-if="showCreate" class="modal-mask" @click.self="showCreate = false">
      <div class="modal modal-lg">
        <h3>登记租赁单</h3>
        <div class="form-grid">
          <label><span>租赁单号 *</span><input v-model="createForm.租赁单号" placeholder="如 RENT-2026-004，重复提交自动幂等" /></label>
          <label><span>承租方 *</span><input v-model="createForm.承租方" /></label>
          <label><span>起租日 *</span><input v-model="createForm.起租日" type="date" /></label>
          <label><span>约定到期日 *</span><input v-model="createForm.约定到期日" type="date" /></label>
          <label><span>押金退还约定 *</span>
            <select v-model="createForm.押金规则">
              <option v-for="rule in depositRules" :key="rule.name" :value="rule.name">{{ rule.name }}</option>
            </select>
          </label>
          <label class="check-line"><input v-model="createForm.立即收押金" type="checkbox" /> 登记同时收取押金（按台入账）</label>
        </div>
        <p class="hint">{{ depositRuleHint }}</p>
        <div class="device-list">
          <div v-for="(device, index) in createForm.devices" :key="index" class="device-row">
            <input v-model="device.设备编号" placeholder="设备编号 *" />
            <input v-model="device.设备名称" placeholder="设备名称 *" />
            <input v-model.number="device.月租金" type="number" min="0" step="0.01" placeholder="月租金 *" />
            <input v-model.number="device.押金" type="number" min="0" step="0.01" placeholder="押金 *" />
            <button class="btn ghost" type="button" @click="createForm.devices.splice(index, 1)">删除</button>
          </div>
          <button class="btn" type="button" @click="addDevice">+ 增加一台设备</button>
        </div>
        <div class="modal-foot">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" type="button" :disabled="submitting" @click="submitCreate">提交登记</button>
        </div>
      </div>
    </div>

    <!-- 动作弹窗：退租 / 结算 / 变更 / 收退款 -->
    <div v-if="action.kind" class="modal-mask" @click.self="action.kind = ''">
      <div class="modal modal-lg">
        <h3>{{ actionTitle }} · {{ action.row?.租赁单号 }}</h3>

        <template v-if="action.kind === 'return'">
          <p class="hint">勾选本批同日退租的设备；不勾的设备继续在租，留到下一批再结。</p>
          <label class="filter-item"><span>退租日期</span><input v-model="action.returnDate" type="date" /></label>
          <table class="data-table sub-table">
            <thead><tr><th>选择</th><th>设备编号</th><th>设备名称</th><th>月租金</th><th>押金</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="line in action.lines" :key="line.id">
                <td><input v-if="!line.实退日期" v-model="action.checked" type="checkbox" :value="line.id" /></td>
                <td>{{ line.设备编号 }}</td><td>{{ line.设备名称 }}</td>
                <td class="num">{{ money(line.月租金) }}</td><td class="num">{{ money(line.押金) }}</td>
                <td>{{ line.实退日期 ? `已于 ${line.实退日期} 退租` : '在租' }}</td>
              </tr>
            </tbody>
          </table>
        </template>

        <template v-else-if="action.kind === 'settle'">
          <p class="hint">按设备逐台生成月租并只对已退租设备入账；已入账账单不会重复生成。满月按月租，零头按天（月租/30）折算；押金按约定算法结算为应退。</p>
          <label class="filter-item"><span>结算日期（留空取今天）</span><input v-model="action.date" type="date" /></label>
          <table class="data-table sub-table">
            <thead><tr><th>设备编号</th><th>实退日期</th><th>是否本批入账</th></tr></thead>
            <tbody>
              <tr v-for="line in action.lines" :key="line.id">
                <td>{{ line.设备编号 }}</td><td>{{ line.实退日期 ?? '—' }}</td>
                <td>{{ line.settled ? '已入账，跳过' : line.实退日期 ? '将入账' : '未退租，留下一批' }}</td>
              </tr>
            </tbody>
          </table>
        </template>

        <template v-else-if="action.kind === 'amend'">
          <p class="hint">新约定对后续生效；已出账的设备会按最后一次约定重算，<b>原账单保留原值</b>，差额以调整账单入账。</p>
          <div class="form-grid">
            <label><span>变更日</span><input v-model="action.date" type="date" /></label>
            <label><span>新起租日（不改留空）</span><input v-model="action.起租日" type="date" /></label>
            <label><span>新约定到期日（不改留空）</span><input v-model="action.约定到期日" type="date" /></label>
            <label><span>新押金退还约定（不改留空）</span>
              <select v-model="action.押金规则">
                <option value="">不改</option>
                <option v-for="rule in depositRules" :key="rule.name" :value="rule.name">{{ rule.name }}</option>
              </select>
            </label>
            <label class="span-2"><span>变更说明</span><input v-model="action.说明" placeholder="如：续租谈判月租降至 …" /></label>
          </div>
          <table class="data-table sub-table">
            <thead><tr><th>设备编号</th><th>当前月租金</th><th>新月租金（留空不改）</th><th>当前押金</th><th>新押金（留空不改）</th></tr></thead>
            <tbody>
              <tr v-for="line in action.lines" :key="line.id">
                <td>{{ line.设备编号 }}</td>
                <td class="num">{{ money(line.月租金) }}</td>
                <td><input v-model.number="action.rates[line.id]" type="number" step="0.01" placeholder="不改" /></td>
                <td class="num">{{ money(line.押金) }}</td>
                <td><input v-model.number="action.deposits[line.id]" type="number" step="0.01" placeholder="不改" /></td>
              </tr>
            </tbody>
          </table>
        </template>

        <template v-else-if="action.kind === 'payment'">
          <div class="form-grid">
            <label><span>种类</span>
              <select v-model="action.payKind">
                <option value="rent">收租金</option>
                <option value="deposit">收押金</option>
                <option value="refund">退押金</option>
              </select>
            </label>
            <label><span>日期</span><input v-model="action.date" type="date" /></label>
            <label class="span-2"><span>凭证号 / 幂等号（同号重复提交只入账一次）</span><input v-model="action.token" placeholder="如 VOUCHER-20260920-01" /></label>
          </div>
          <table class="data-table sub-table">
            <thead><tr><th>设备编号</th><th>未收租金</th><th>应退未退押金</th><th>本次金额</th></tr></thead>
            <tbody>
              <tr v-for="line in action.lines" :key="line.id">
                <td>{{ line.设备编号 }}</td>
                <td class="num">{{ money(lineUnpaid(line.id)) }}</td>
                <td class="num">{{ money(lineRefundDue(line.id)) }}</td>
                <td><input v-model.number="action.amounts[line.id]" type="number" min="0" step="0.01" placeholder="0.00" /></td>
              </tr>
            </tbody>
          </table>
        </template>

        <div class="modal-foot">
          <button class="btn" type="button" @click="action.kind = ''">取消</button>
          <button class="btn primary" type="button" :disabled="submitting" @click="submitAction">确认</button>
        </div>
      </div>
    </div>

    <!-- 单张租赁单台账：设备明细 / 账单流水（原值 + 调整留痕）/ 收退款 / 变更记录 -->
    <div v-if="detail" class="detail-panel">
      <div class="detail-head">
        <h3>台账明细 · {{ detail.lease.租赁单号 }}（{{ detail.lease.承租方 }}）</h3>
        <button class="btn ghost" type="button" @click="detail = null">关闭</button>
      </div>

      <div class="stat-row">
        <article class="stat-card"><span class="stat-label">应收租金</span><strong class="stat-value">{{ money(detail.summary.应收租金) }}</strong></article>
        <article class="stat-card"><span class="stat-label">已收租金</span><strong class="stat-value">{{ money(detail.summary.已收租金) }}</strong></article>
        <article class="stat-card"><span class="stat-label">已收押金</span><strong class="stat-value">{{ money(detail.summary.已收押金) }}</strong></article>
        <article class="stat-card"><span class="stat-label">应退押金</span><strong class="stat-value">{{ money(detail.summary.应退押金) }}</strong></article>
        <article class="stat-card"><span class="stat-label">已退押金</span><strong class="stat-value">{{ money(detail.summary.已退押金) }}</strong></article>
        <article class="stat-card"><span class="stat-label">在租台数</span><strong class="stat-value">{{ detail.summary.在租台数 }}</strong></article>
      </div>

      <div class="tab-bar">
        <button v-for="tab in tabs" :key="tab.key" type="button" :class="['tab', { active: activeTab === tab.key }]" @click="activeTab = tab.key">{{ tab.label }}</button>
      </div>

      <table v-if="activeTab === 'lines'" class="data-table">
        <thead><tr><th>行</th><th>设备编号</th><th>设备名称</th><th>月租金</th><th>押金</th><th>起租日</th><th>约定到期日</th><th>押金约定</th><th>实退日期</th><th>账单状态</th></tr></thead>
        <tbody>
          <tr v-for="line in detail.lines" :key="line.id">
            <td>{{ line.行号 }}</td><td>{{ line.设备编号 }}</td><td>{{ line.设备名称 }}</td>
            <td class="num">{{ money(line.月租金) }}</td><td class="num">{{ money(line.押金) }}</td>
            <td>{{ line.起租日 }}</td><td>{{ line.约定到期日 }}</td>
            <td>{{ line.押金规则 }}（{{ Math.round(line.退还比例 * 100) }}%）</td>
            <td>{{ line.实退日期 ?? '在租' }}</td>
            <td>{{ line.settled ? '已入账' : line.实退日期 ? '待结算' : '未退租' }}</td>
          </tr>
        </tbody>
      </table>

      <table v-else-if="activeTab === 'bills'" class="data-table">
        <thead><tr><th>账单号</th><th>设备</th><th>类型</th><th>账期</th><th>金额</th><th>方向</th><th>来源批次</th><th>约定版本</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="bill in detail.bills" :key="String(bill.id)" :class="{ 'row-adjust': String(bill.类型).includes('调整') }">
            <td>{{ bill.单据编号 }}</td><td>{{ bill.设备编号 }}</td><td>{{ bill.类型 }}</td>
            <td>{{ bill.账期起 }} ~ {{ bill.账期止 }}</td>
            <td class="num">{{ money(Number(bill.金额)) }}</td><td>{{ bill.方向 }}</td>
            <td>{{ bill.来源批次 }}</td><td>v{{ bill.版本 }}</td><td>{{ bill.说明 }}</td>
          </tr>
        </tbody>
      </table>

      <table v-else-if="activeTab === 'payments'" class="data-table">
        <thead><tr><th>单据号</th><th>设备行</th><th>种类</th><th>金额</th><th>日期</th><th>凭证号</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="pay in detail.payments" :key="String(pay.id)">
            <td>{{ pay.单据编号 }}</td><td>#{{ pay.line_id }}</td><td>{{ pay.种类 }}</td>
            <td class="num">{{ money(Number(pay.金额)) }}</td><td>{{ pay.日期 }}</td>
            <td>{{ pay.凭证号 }}</td><td>{{ pay.说明 }}</td>
          </tr>
        </tbody>
      </table>

      <table v-else class="data-table">
        <thead><tr><th>版本</th><th>变更日</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="(amend, index) in detail.amendments" :key="index">
            <td>v{{ amend.版本 }}</td><td>{{ amend.变更日 }}</td><td>{{ amend.说明 || '—' }}</td>
          </tr>
          <tr v-if="!detail.amendments.length"><td colspan="3" class="empty-state">暂无中途变更</td></tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import {
  amendLease,
  createLease,
  fetchDepositRules,
  fetchLeaseDetail,
  fetchLeases,
  fetchSummary,
  registerPayment,
  registerReturns,
  settleLease,
  type DepositRule,
  type LeaseDetail,
  type LeaseLine,
  type LeaseRow,
  type Summary,
} from '@/api/rental'

const statuses = ['在租', '部分退租', '已退租', '已结清']
const tabs = [
  { key: 'lines', label: '设备明细' },
  { key: 'bills', label: '账单流水（原值+调整）' },
  { key: 'payments', label: '收退款流水' },
  { key: 'amendments', label: '变更记录' },
] as const

const summary = ref<Summary | null>(null)
const rows = ref<LeaseRow[]>([])
const total = ref(0)
const filters = reactive<{ keyword: string; status: string }>({ keyword: '', status: '' })
const message = ref('')
const messageOk = ref(true)
const submitting = ref(false)
const depositRules = ref<DepositRule[]>([])
const detail = ref<LeaseDetail | null>(null)
const activeTab = ref<(typeof tabs)[number]['key']>('lines')
const showCreate = ref(false)

const createForm = reactive({
  租赁单号: '',
  承租方: '',
  起租日: '',
  约定到期日: '',
  押金规则: '全额退还',
  立即收押金: true,
  devices: [{ 设备编号: '', 设备名称: '', 月租金: null as number | null, 押金: null as number | null }],
})

const action = reactive<{
  kind: '' | 'return' | 'settle' | 'amend' | 'payment'
  row: LeaseRow | null
  lines: LeaseLine[]
  returnDate: string
  date: string
  checked: number[]
  起租日: string
  约定到期日: string
  押金规则: string
  说明: string
  payKind: 'rent' | 'deposit' | 'refund'
  token: string
  rates: Record<number, number | null>
  deposits: Record<number, number | null>
  amounts: Record<number, number | null>
}>({
  kind: '',
  row: null,
  lines: [],
  returnDate: '',
  date: '',
  checked: [],
  起租日: '',
  约定到期日: '',
  押金规则: '',
  说明: '',
  payKind: 'rent',
  token: '',
  rates: {},
  deposits: {},
  amounts: {},
})

const summaryCards = computed(() => {
  const s = summary.value
  if (!s) return []
  return [
    { label: '在租台数', value: s.在租台数 },
    { label: '租赁单数', value: s.租赁单数 },
    { label: '应收租金（元）', value: money(s.应收租金) },
    { label: '已收租金（元）', value: money(s.已收租金) },
    { label: '未收租金（元）', value: money(s.未收租金) },
    { label: '已收押金（元）', value: money(s.已收押金) },
    { label: '应退押金（元）', value: money(s.应退押金) },
    { label: '已退押金（元）', value: money(s.已退押金) },
  ]
})

const actionTitle = computed(() => ({
  return: '登记退租',
  settle: '生成结算账单',
  amend: '中途变更约定',
  payment: '登记收退款',
} as const)[action.kind as 'return' | 'settle' | 'amend' | 'payment'])

const depositRuleHint = computed(() => {
  const rule = depositRules.value.find((item) => item.name === createForm.押金规则)
  return rule ? `押金算法：${rule.desc}` : ''
})

function money(value: number | null | undefined): string {
  return (Number(value ?? 0)).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function statusTag(status: string): string {
  return { 在租: 'info', 部分退租: 'warn', 已退租: 'muted', 已结清: 'ok' }[status] ?? 'muted'
}

function lineUnpaid(lineId: number): number {
  if (!detail.value && !action.row) return 0
  const source = detail.value
  if (!source) return 0
  const billed = source.bills
    .filter((bill) => Number(bill.line_id) === lineId && ['月租', '尾期租金', '租金调整'].includes(String(bill.类型)))
    .reduce((sum, bill) => sum + Number(bill.金额), 0)
  const paid = source.payments
    .filter((pay) => Number(pay.line_id) === lineId && pay.种类 === '租金')
    .reduce((sum, pay) => sum + Number(pay.金额), 0)
  return Math.max(0, billed - paid)
}

function lineRefundDue(lineId: number): number {
  if (!detail.value) return 0
  const refundable = -detail.value.bills
    .filter((bill) => Number(bill.line_id) === lineId && ['押金结算', '押金调整'].includes(String(bill.类型)))
    .reduce((sum, bill) => sum + Number(bill.金额), 0)
  const refunded = detail.value.payments
    .filter((pay) => Number(pay.line_id) === lineId && pay.种类 === '押金退款')
    .reduce((sum, pay) => sum + Number(pay.金额), 0)
  return Math.max(0, refundable - refunded)
}

function flash(ok: boolean, text: string) {
  messageOk.value = ok
  message.value = text
}

function resetFilters() {
  filters.keyword = ''
  filters.status = ''
  void reload()
}

function exportRows() {
  window.open('/api/rental/export', '_blank')
}

function openCreate() {
  Object.assign(createForm, {
    租赁单号: '',
    承租方: '',
    起租日: '',
    约定到期日: '',
    押金规则: depositRules.value[0]?.name ?? '全额退还',
    立即收押金: true,
  })
  createForm.devices = [{ 设备编号: '', 设备名称: '', 月租金: null, 押金: null }]
  showCreate.value = true
}

function addDevice() {
  createForm.devices.push({ 设备编号: '', 设备名称: '', 月租金: null, 押金: null })
}

async function submitCreate() {
  submitting.value = true
  try {
    const result = await createLease({ ...createForm })
    flash(result.ok, result.message)
    if (result.ok) {
      showCreate.value = false
      await reload()
    }
  } catch (error) {
    flash(false, error instanceof Error ? error.message : '登记失败')
  } finally {
    submitting.value = false
  }
}

async function openDetail(id: number) {
  try {
    detail.value = await fetchLeaseDetail(id)
    activeTab.value = 'lines'
  } catch (error) {
    flash(false, error instanceof Error ? error.message : '明细读取失败')
  }
}

async function openAction(row: LeaseRow, kind: 'return' | 'settle' | 'amend' | 'payment') {
  await openDetail(row.id)
  action.kind = kind
  action.row = row
  action.lines = detail.value?.lines ?? []
  action.returnDate = ''
  action.date = ''
  action.checked = []
  action.起租日 = ''
  action.约定到期日 = ''
  action.押金规则 = ''
  action.说明 = ''
  action.payKind = 'rent'
  action.token = ''
  action.rates = {}
  action.deposits = {}
  action.amounts = {}
}

async function submitAction() {
  if (!action.row) return
  const id = action.row.id
  submitting.value = true
  try {
    let result: { ok: boolean; message: string }
    if (action.kind === 'return') {
      result = await registerReturns(id, { 退租日期: action.returnDate, items: action.checked.map((lineId) => ({ line_id: lineId })) })
    } else if (action.kind === 'settle') {
      result = await settleLease(id, { 结算日期: action.date || null })
    } else if (action.kind === 'amend') {
      const 明细 = action.lines
        .filter((line) => action.rates[line.id] != null || action.deposits[line.id] != null)
        .map((line) => ({
          line_id: line.id,
          ...(action.rates[line.id] != null ? { 月租金: action.rates[line.id] } : {}),
          ...(action.deposits[line.id] != null ? { 押金: action.deposits[line.id] } : {}),
        }))
      result = await amendLease(id, {
        变更日: action.date || null,
        起租日: action.起租日 || null,
        约定到期日: action.约定到期日 || null,
        押金规则: action.押金规则 || null,
        说明: action.说明 || null,
        明细,
        client_token: action.token || `ui-amend-${id}-${Date.now()}`,
      })
    } else {
      const 明细 = action.lines
        .filter((line) => Number(action.amounts[line.id] ?? 0) > 0)
        .map((line) => ({ line_id: line.id, 金额: action.amounts[line.id] }))
      result = await registerPayment(id, {
        种类: action.payKind,
        日期: action.date || null,
        凭证号: action.token || null,
        明细,
        client_token: action.token || `ui-pay-${id}-${Date.now()}`,
      })
    }
    flash(result.ok, result.message)
    if (result.ok) {
      action.kind = ''
      await reload()
      await openDetail(id)
    }
  } catch (error) {
    flash(false, error instanceof Error ? error.message : '操作失败')
  } finally {
    submitting.value = false
  }
}

async function loadSummary() {
  try {
    summary.value = await fetchSummary()
  } catch {
    // 看板读不到时保留空态，不阻塞台账
  }
}

async function reload() {
  try {
    const payload = await fetchLeases({ keyword: filters.keyword.trim(), status: filters.status })
    rows.value = payload.items
    total.value = payload.total
    if (detail.value) {
      detail.value = await fetchLeaseDetail(detail.value.lease.id)
    }
  } catch (error) {
    flash(false, error instanceof Error ? error.message : '租赁台账读取失败')
  }
  await loadSummary()
}

onMounted(async () => {
  try {
    const rulePayload = await fetchDepositRules()
    depositRules.value = rulePayload.rules
  } catch {
    depositRules.value = []
  }
  await reload()
})
</script>
