<template>
  <section class="page" data-module="rental">
    <header class="page-head">
      <div>
        <h2>租赁结算台账</h2>
        <p class="page-desc">
          按设备与承租方登记租期、月租与押金；同批退租按设备逐台出账，未退租留下一批；
          改租后原账单保留原值、按最后一次约定重算；应收、已收与在租台数全部取自同一份账单。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openRegister">登记租赁单</button>
      </div>
    </header>

    <!-- 概览卡片：与台账区同一份后端汇总 -->
    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ formatValue(card.value) }}</strong>
      </article>
    </div>

    <p v-if="message" :class="['result-line', lastOk ? 'ok-text' : 'error-text']">{{ message }}</p>

    <div class="tabs">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        :class="['tab-btn', { active: activeTab === tab.key }]"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </div>

    <!-- ============ 租赁单与设备明细 ============ -->
    <template v-if="activeTab === 'leases'">
      <form class="filter-bar" @submit.prevent="reloadLeases">
        <label class="filter-item">
          <span>租赁单号 / 承租方</span>
          <input v-model="keyword" placeholder="按租赁单号或承租方检索" />
        </label>
        <button class="btn" type="submit">查询</button>
      </form>

      <div v-for="lease in leases" :key="lease.id" class="lease-block">
        <div class="lease-head" @click="toggleLease(lease.id)">
          <div class="lease-title">
            <span class="caret">{{ expanded[lease.id] ? '▾' : '▸' }}</span>
            <strong>{{ lease.租赁单号 }}</strong>
            <span class="muted">{{ lease.承租方 }}</span>
            <span class="muted">{{ lease.登记时间 }}</span>
          </div>
          <div class="lease-sum">
            <span>设备 {{ lease.设备台数 }} 台 · 在租 {{ lease.在租台数 }} 台</span>
            <span>租金应收 ¥{{ formatMoney(lease.租金应收) }}（已收 ¥{{ formatMoney(lease.租金已收) }}）</span>
            <span>押金待退 ¥{{ formatMoney(lease.待退押金) }}</span>
          </div>
        </div>

        <table v-if="expanded[lease.id]" class="data-table">
          <thead>
            <tr>
              <th>选择</th>
              <th>设备编号</th>
              <th>设备名称</th>
              <th>起租日期</th>
              <th>到期日期</th>
              <th>月租</th>
              <th>押金/算法</th>
              <th>实际退租</th>
              <th>状态</th>
              <th>租金应收/已收/未收</th>
              <th>应退押金</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in lease.设备明细" :key="item.id" :class="{ returned: item.实际退租日期, superseded: false }">
              <td>
                <input
                  type="checkbox"
                  :value="item.id"
                  v-model="selectedItems"
                  :aria-label="`选择设备 ${item.设备编号}`"
                />
              </td>
              <td>{{ item.设备编号 }}</td>
              <td>{{ item.设备名称 }}</td>
              <td>{{ item.起租日期 }}</td>
              <td>{{ item.到期日期 }}</td>
              <td>¥{{ formatMoney(item.月租金额) }}</td>
              <td>
                ¥{{ formatMoney(item.押金金额) }}
                <span class="muted">/{{ item.押金算法 }}</span>
              </td>
              <td>{{ item.实际退租日期 ?? '—' }}</td>
              <td>
                <span :class="['status-pill', statusClass(item.状态)]">{{ item.状态 }}</span>
              </td>
              <td>
                ¥{{ formatMoney(item.租金应收) }} / ¥{{ formatMoney(item.租金已收) }} /
                <strong v-if="item.租金预收 > 0" class="ok-text">预收 ¥{{ formatMoney(item.租金预收) }}</strong>
                <strong v-else :class="{ 'error-text': item.租金未收 > 0 }">¥{{ formatMoney(item.租金未收) }}</strong>
              </td>
              <td>
                ¥{{ formatMoney(item.应退押金) }}
                <span v-if="item.押金测算" class="muted">{{ item.押金测算.计算说明 }}</span>
              </td>
              <td class="row-actions">
                <button class="link" type="button" @click="viewBills(item.id)">账单</button>
                <button class="link" type="button" @click="openChange(item)">改租期</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 批次操作条 -->
      <div class="batch-bar">
        <label class="filter-item">
          <span>本批退租日期 / 结算日期</span>
          <input v-model="batchDate" type="date" />
        </label>
        <button class="btn" type="button" :disabled="!canBatch" @click="registerReturn">
          登记所选设备退租（{{ selectedItems.length }} 台）
        </button>
        <button class="btn primary" type="button" :disabled="!canBatch" @click="settleBatch">
          本批结算（已退租才入账，未退租留下一批）
        </button>
        <button class="btn ghost" type="button" @click="selectedItems = []">清空选择</button>
        <span class="muted">已选 {{ selectedItems.length }} 台</span>
      </div>
    </template>

    <!-- ============ 账单台账 ============ -->
    <template v-else>
      <div class="filter-bar">
        <label class="filter-item">
          <span>账单类型</span>
          <select v-model="billTypeFilter">
            <option value="">全部</option>
            <option value="租金">租金</option>
            <option value="租金重算">租金重算</option>
            <option value="押金">押金</option>
            <option value="押金退还">押金退还</option>
          </select>
        </label>
        <label class="filter-item">
          <span>历史冲销单</span>
          <select v-model="includeSuperseded">
            <option value="true">保留显示（原值可追溯）</option>
            <option value="false">只看有效账单</option>
          </select>
        </label>
      </div>

      <table class="data-table ledger-table">
        <thead>
          <tr>
            <th>账单ID</th>
            <th>设备明细ID</th>
            <th>类型</th>
            <th>账期</th>
            <th>起止</th>
            <th>应收/应退</th>
            <th>已收/已退</th>
            <th>未结</th>
            <th>预收</th>
            <th>状态/版本</th>
            <th>批次</th>
            <th>备注</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="bill in filteredBills" :key="bill.id" :class="{ voided: bill.状态 === '已冲销' }">
            <td>{{ bill.id }}</td>
            <td>{{ bill.设备明细ID }}</td>
            <td>{{ typeLabel(bill.账单类型) }}</td>
            <td>{{ bill.账期月份 }}</td>
            <td class="muted">{{ bill.起始日期 }} ~ {{ bill.结束日期 }}</td>
            <td>{{ signedMoney(bill.金额, bill.账单类型) }}</td>
            <td>{{ signedMoney(bill.已收金额, bill.账单类型) }}</td>
            <td :class="{ 'error-text': bill.未结金额 > 0 }">¥{{ formatMoney(bill.未结金额) }}</td>
            <td :class="{ 'ok-text': bill.预收金额 > 0 }">
              {{ bill.预收金额 > 0 ? `¥${formatMoney(bill.预收金额)}` : '—' }}
            </td>
            <td>
              <span :class="['status-pill', bill.状态 === '有效' ? 'ok' : 'void']">{{ bill.状态 }}</span>
              v{{ bill.版本号 }}
            </td>
            <td class="muted">{{ bill.批次号 }}</td>
            <td class="muted remark-cell">{{ bill.备注 }}</td>
            <td>
              <button
                v-if="bill.状态 === '有效' && bill.未结金额 > 0"
                class="link"
                type="button"
                @click="openPay(bill)"
              >
                {{ bill.账单类型 === '押金退还' ? '登记退款' : '登记收款' }}
              </button>
              <span v-else class="muted">{{ bill.未结金额 === 0 ? '已结清' : '—' }}</span>
            </td>
          </tr>
          <tr v-if="!filteredBills.length">
            <td colspan="13" class="empty-state">没有符合条件的账单</td>
          </tr>
        </tbody>
      </table>
    </template>

    <!-- 登记租赁单弹层 -->
    <div v-if="registerOpen" class="modal-mask" @click.self="registerOpen = false">
      <div class="modal">
        <h3>登记租赁单</h3>
        <p class="muted">租赁单号是重复提交的去重依据：同一份单子重复提交只入账一次。</p>
        <div class="form-grid">
          <label><span>租赁单号 *</span><input v-model="form.租赁单号" placeholder="如 ZL-2026-1001" /></label>
          <label><span>承租方 *</span><input v-model="form.承租方" /></label>
          <label><span>联系电话</span><input v-model="form.联系电话" /></label>
        </div>

        <div v-for="(dev, idx) in form.设备明细" :key="idx" class="device-form">
          <div class="device-form-head">
            <strong>设备 {{ idx + 1 }}</strong>
            <button v-if="form.设备明细.length > 1" class="link danger" type="button" @click="form.设备明细.splice(idx, 1)">
              移除
            </button>
          </div>
          <div class="form-grid">
            <label><span>设备编号 *</span><input v-model="dev.设备编号" /></label>
            <label><span>设备名称 *</span><input v-model="dev.设备名称" /></label>
            <label><span>起租日期 *</span><input v-model="dev.起租日期" type="date" /></label>
            <label><span>到期日期 *</span><input v-model="dev.到期日期" type="date" /></label>
            <label><span>月租金额 *</span><input v-model="dev.月租金额" type="number" min="0" step="0.01" /></label>
            <label><span>押金金额 *</span><input v-model="dev.押金金额" type="number" min="0" step="0.01" /></label>
            <label>
              <span>押金退还算法 *</span>
              <select v-model="dev.押金算法">
                <option v-for="algo in depositAlgorithms" :key="algo" :value="algo">{{ algo }}</option>
              </select>
            </label>
            <label>
              <span>违约金比例（月租倍数）</span>
              <input v-model="dev.违约金比例" type="number" min="0" step="0.1" />
            </label>
          </div>
        </div>
        <button class="btn" type="button" @click="addDevice">+ 增加一台设备</button>

        <div class="modal-foot">
          <button class="btn ghost" type="button" @click="registerOpen = false">取消</button>
          <button class="btn primary" type="button" :disabled="submitting" @click="submitRegister">
            {{ submitting ? '提交中…' : '提交登记' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 改租期弹层 -->
    <div v-if="changeTarget" class="modal-mask" @click.self="changeTarget = null">
      <div class="modal">
        <h3>变更租期：{{ changeTarget.设备编号 }} {{ changeTarget.设备名称 }}</h3>
        <p class="muted">
          提交后已出账的账单原值保留（标记已冲销），按最后一次约定重开同账期账单；
          押金退还按最新算法重算。
        </p>
        <div class="form-grid">
          <label><span>新起租日期 *</span><input v-model="changeForm.新起租日期" type="date" /></label>
          <label><span>新到期日期 *</span><input v-model="changeForm.新到期日期" type="date" /></label>
          <label><span>新月租金额 *</span><input v-model="changeForm.新月租金额" type="number" min="0" step="0.01" /></label>
          <label>
            <span>新押金算法</span>
            <select v-model="changeForm.新押金算法">
              <option value="">（不变更）</option>
              <option v-for="algo in depositAlgorithms" :key="algo" :value="algo">{{ algo }}</option>
            </select>
          </label>
          <label><span>新违约金比例</span><input v-model="changeForm.新违约金比例" type="number" min="0" step="0.1" /></label>
          <label class="span-2"><span>变更原因</span><input v-model="changeForm.变更原因" /></label>
        </div>
        <div class="modal-foot">
          <button class="btn ghost" type="button" @click="changeTarget = null">取消</button>
          <button class="btn primary" type="button" :disabled="submitting" @click="submitChange">确认变更并重算</button>
        </div>
      </div>
    </div>

    <!-- 收款/退款弹层 -->
    <div v-if="payTarget" class="modal-mask" @click.self="payTarget = null">
      <div class="modal small">
        <h3>{{ payTarget.账单类型 === '押金退还' ? '登记退款' : '登记收款' }}（账单 #{{ payTarget.id }}）</h3>
        <p class="muted">
          {{ payTarget.账期月份 }} · 未结
          ¥{{ formatMoney(payTarget.未结金额) }}（不允许超过未结金额）
        </p>
        <div class="form-grid">
          <label>
            <span>金额 *</span>
            <input v-model="payForm.金额" type="number" min="0" :max="payTarget.未结金额" step="0.01" />
          </label>
        </div>
        <div class="modal-foot">
          <button class="btn ghost" type="button" @click="payTarget = null">取消</button>
          <button class="btn primary" type="button" :disabled="submitting" @click="submitPay">确认</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import {
  rentalApi,
  type Bill,
  type Lease,
  type LeaseItem,
  type OverviewCard,
} from '@/api/rental'

const depositAlgorithms = ['全额退', '扣欠费', '扣欠费及违约金']

const tabs = [
  { key: 'leases', label: '租赁单与设备明细' },
  { key: 'ledger', label: '账单台账' },
] as const

const activeTab = ref<(typeof tabs)[number]['key']>('leases')
const cards = ref<OverviewCard[]>([])
const leases = ref<Lease[]>([])
const keyword = ref('')
const expanded = reactive<Record<number, boolean>>({ 1: true, 2: true, 3: true })
const selectedItems = ref<number[]>([])
const batchDate = ref(new Date().toISOString().slice(0, 10))

const message = ref('')
const lastOk = ref(true)
const submitting = ref(false)

const billTypeFilter = ref('')
const includeSuperseded = ref('true')
const allBills = ref<Bill[]>([])

function showMessage(msg: string, ok = true) {
  message.value = msg
  lastOk.value = ok
}

function formatMoney(value: number): string {
  return Number(value || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function formatValue(value: number): string {
  return Number.isInteger(value) ? String(value) : formatMoney(value)
}

function signedMoney(value: number, type: string): string {
  const prefix = type === '押金退还' ? '退 ¥' : '¥'
  return `${prefix}${formatMoney(Math.abs(value))}`
}

function typeLabel(type: string): string {
  return { 租金: '月租', 租金重算: '月租(重算)', 押金: '押金', 押金退还: '退押金' }[type] ?? type
}

function statusClass(status: string): string {
  if (status === '已退租') return 'ok'
  if (status === '即将到期' || status === '已逾期') return 'warn'
  return ''
}

const canBatch = computed(() => selectedItems.value.length > 0 && !!batchDate.value)

const filteredBills = computed(() => allBills.value.filter((bill) => {
  if (billTypeFilter.value && bill.账单类型 !== billTypeFilter.value) return false
  if (includeSuperseded.value === 'false' && bill.状态 !== '有效') return false
  return true
}))

async function reloadAll() {
  const [overview, ledgerResult] = await Promise.all([
    rentalApi.overview(),
    rentalApi.ledger(),
  ])
  cards.value = overview.cards
  leases.value = overview.租赁单
  allBills.value = ledgerResult.账单
}

async function reloadLeases() {
  const result = await rentalApi.leases(keyword.value.trim())
  leases.value = result.items
}

function toggleLease(id: number) {
  expanded[id] = !expanded[id]
}

function viewBills(itemId: number) {
  billTypeFilter.value = ''
  includeSuperseded.value = 'true'
  activeTab.value = 'ledger'
  // 切到台账后直接用接口过滤
  void rentalApi.bills({ item_id: itemId }).then((res) => {
    allBills.value = res.items
  })
}

// ---- 登记 ----------------------------------------------------------------
const registerOpen = ref(false)

type DeviceDraft = {
  设备编号: string
  设备名称: string
  起租日期: string
  到期日期: string
  月租金额: string
  押金金额: string
  押金算法: string
  违约金比例: string
}

function emptyDevice(): DeviceDraft {
  return {
    设备编号: '', 设备名称: '', 起租日期: '', 到期日期: '',
    月租金额: '', 押金金额: '', 押金算法: '全额退', 违约金比例: '0',
  }
}

const form = reactive({
  租赁单号: '',
  承租方: '',
  联系电话: '',
  设备明细: [emptyDevice()],
})

function openRegister() {
  form.租赁单号 = ''
  form.承租方 = ''
  form.联系电话 = ''
  form.设备明细 = [emptyDevice()]
  registerOpen.value = true
}

function addDevice() {
  form.设备明细.push(emptyDevice())
}

async function submitRegister() {
  submitting.value = true
  try {
    const res = await rentalApi.register({
      租赁单号: form.租赁单号.trim(),
      承租方: form.承租方.trim(),
      联系电话: form.联系电话.trim(),
      设备明细: form.设备明细.map((dev) => ({ ...dev })),
    })
    showMessage(res.message, res.ok)
    if (res.ok) {
      registerOpen.value = false
      await reloadAll()
    }
  } catch (error) {
    showMessage(error instanceof Error ? error.message : '登记失败', false)
  } finally {
    submitting.value = false
  }
}

// ---- 退租 / 结算 ----------------------------------------------------------
async function registerReturn() {
  submitting.value = true
  try {
    const res = await rentalApi.registerReturn({
      设备明细IDs: selectedItems.value,
      退租日期: batchDate.value,
    })
    showMessage(res.message, res.ok)
    selectedItems.value = []
    await reloadAll()
  } catch (error) {
    showMessage(error instanceof Error ? error.message : '退租登记失败', false)
  } finally {
    submitting.value = false
  }
}

async function settleBatch() {
  submitting.value = true
  try {
    const res = await rentalApi.settle({
      设备明细IDs: selectedItems.value,
      结算日期: batchDate.value,
    })
    showMessage(res.message, res.ok)
    selectedItems.value = []
    await reloadAll()
  } catch (error) {
    showMessage(error instanceof Error ? error.message : '批次结算失败', false)
  } finally {
    submitting.value = false
  }
}

// ---- 改租期 ---------------------------------------------------------------
const changeTarget = ref<LeaseItem | null>(null)
const changeForm = reactive({
  新起租日期: '',
  新到期日期: '',
  新月租金额: '',
  新押金算法: '',
  新违约金比例: '',
  变更原因: '',
})

function openChange(item: LeaseItem) {
  changeTarget.value = item
  changeForm.新起租日期 = item.起租日期
  changeForm.新到期日期 = item.到期日期
  changeForm.新月租金额 = String(item.月租金额)
  changeForm.新押金算法 = ''
  changeForm.新违约金比例 = ''
  changeForm.变更原因 = ''
}

async function submitChange() {
  if (!changeTarget.value) return
  submitting.value = true
  try {
    const values: Record<string, unknown> = {
      新起租日期: changeForm.新起租日期,
      新到期日期: changeForm.新到期日期,
      新月租金额: changeForm.新月租金额,
      变更原因: changeForm.变更原因,
    }
    if (changeForm.新押金算法) values.新押金算法 = changeForm.新押金算法
    if (changeForm.新违约金比例) values.新违约金比例 = changeForm.新违约金比例
    const res = await rentalApi.changeTerm(changeTarget.value.id, values)
    showMessage(res.message, res.ok)
    changeTarget.value = null
    await reloadAll()
  } catch (error) {
    showMessage(error instanceof Error ? error.message : '租期变更失败', false)
  } finally {
    submitting.value = false
  }
}

// ---- 收款 -----------------------------------------------------------------
const payTarget = ref<Bill | null>(null)
const payForm = reactive({ 金额: '' })

function openPay(bill: Bill) {
  payTarget.value = bill
  payForm.金额 = String(bill.未结金额.toFixed(2))
}

async function submitPay() {
  if (!payTarget.value) return
  submitting.value = true
  try {
    const res = await rentalApi.pay(payTarget.value.id, { 金额: payForm.金额 })
    showMessage(res.message, res.ok)
    payTarget.value = null
    await reloadAll()
  } catch (error) {
    showMessage(error instanceof Error ? error.message : '登记失败', false)
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  reloadAll().catch((error: unknown) => {
    showMessage(error instanceof Error ? error.message : '租赁数据加载失败', false)
  })
})
</script>

<style scoped>
.tabs { display: flex; gap: 4px; margin: 10px 0; border-bottom: 1px solid var(--border); }
.tab-btn { border: none; background: none; padding: 8px 14px; cursor: pointer; font-size: 13px; color: var(--muted); border-bottom: 2px solid transparent; }
.tab-btn.active { color: var(--brand); border-bottom-color: var(--brand); font-weight: 600; }
.result-line { font-size: 13px; margin: 6px 0; }
.ok-text { color: #067647; }
.lease-block { background: #fff; border: 1px solid var(--border); border-radius: 8px; margin-bottom: 10px; overflow: hidden; }
.lease-head { display: flex; justify-content: space-between; align-items: center; padding: 10px 12px; cursor: pointer; gap: 12px; flex-wrap: wrap; }
.lease-title { display: flex; gap: 10px; align-items: center; font-size: 14px; }
.lease-sum { display: flex; gap: 14px; font-size: 12px; color: var(--muted); }
.caret { width: 12px; color: var(--muted); }
.muted { color: var(--muted); font-weight: normal; font-size: 12px; }
.batch-bar { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; background: #fff; border: 1px dashed var(--border); border-radius: 8px; padding: 10px 12px; margin-top: 10px; }
.status-pill { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; background: #eef2f7; }
.status-pill.ok { background: #dcfae6; color: #067647; }
.status-pill.warn { background: #fef0c7; color: #b54708; }
.status-pill.void { background: #f2f4f7; color: var(--muted); text-decoration: line-through; }
tr.returned { background: #fafbfc; }
tr.voided { color: var(--muted); background: #fafbfc; }
tr.voided td { text-decoration: none; }
.ledger-table th, .ledger-table td { font-size: 12px; }
.remark-cell { max-width: 240px; }
.modal-mask { position: fixed; inset: 0; background: rgba(16, 24, 40, 0.45); display: flex; align-items: center; justify-content: center; z-index: 20; }
.modal { background: #fff; border-radius: 10px; padding: 18px 20px; width: 760px; max-width: 92vw; max-height: 88vh; overflow-y: auto; }
.modal.small { width: 420px; }
.modal h3 { margin: 0 0 6px; font-size: 16px; }
.form-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin: 10px 0; }
.form-grid label span, .device-form span { display: block; font-size: 12px; color: var(--muted); margin-bottom: 2px; }
.form-grid input, .form-grid select, .device-form input, .device-form select { width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.form-grid .span-2 { grid-column: span 2; }
.device-form { border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; margin: 8px 0; }
.device-form-head { display: flex; justify-content: space-between; align-items: center; }
.device-form .form-grid { margin: 6px 0 0; }
.modal-foot { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
.link.danger { color: #b42318; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
