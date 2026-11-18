<template>
  <section class="page" data-module="dwd_refresh_status">
    <header class="page-head">
      <div>
        <h2>明细刷新状态视图</h2>
        <p class="page-desc">
          聚合定时刷新、补数与质量稽核事件，按水位展示待刷新与失败分区；同分区仅保留一条当前状态，点击可复核水位与稽核范围。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="toggle-boundary" type="button" @click="toggleBoundary">
          {{ boundary === 'wide' ? '切换较窄边界' : '切换较宽边界' }}
        </button>
        <button class="btn primary" data-testid="open-record" type="button" @click="openRecord">登记刷新事件</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card" :data-testid="`card-${card.label}`">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>表名称</span>
        <input v-model="filters.table_name" data-testid="filter-table" placeholder="按明细表名称检索" />
      </label>
      <label class="filter-item">
        <span>分区值</span>
        <input v-model="filters.keyword" data-testid="filter-keyword" placeholder="按分区值检索" />
      </label>
      <label class="filter-item">
        <span>场景</span>
        <select v-model="filters.status" data-testid="filter-status">
          <option value="">全部场景</option>
          <option v-for="option in statusOptions" :key="option" :value="option">{{ option }}</option>
        </select>
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <div class="refresh-sections">
      <section v-for="scene in scenes" :key="scene.status" class="refresh-section" :data-testid="`section-${scene.status}`">
        <header class="section-head">
          <strong>{{ scene.label }}</strong>
          <span class="section-count">{{ grouped[scene.status]?.length ?? 0 }} 个分区</span>
        </header>
        <table class="data-table">
          <thead>
            <tr>
              <th v-for="column in columns" :key="column">{{ column }}</th>
              <th>明细</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in grouped[scene.status] ?? []"
              :key="`${row.表名称}-${row.分区值}`"
              :data-testid="`status-row-${row.表名称}-${row.分区值}`"
              :data-status="String(row.当前状态)"
            >
              <td v-for="column in columns" :key="column">{{ display(row, column) }}</td>
              <td class="row-actions">
                <button class="link" type="button" :data-testid="`detail-${row.表名称}-${row.分区值}`" @click="openDetail(row)">
                  查看状态
                </button>
              </td>
            </tr>
            <tr v-if="!(grouped[scene.status]?.length)">
              <td :colspan="columns.length + 1" class="empty-state">当前场景暂无分区</td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>

    <div v-if="showRecord" class="modal-backdrop" data-testid="record-modal">
      <form class="modal" @submit.prevent="submitRecord">
        <h3>登记刷新事件（同批次提交）</h3>
        <label class="create-field">
          <span>批次编号</span>
          <input v-model="recordForm.批次编号" data-testid="record-batch" required />
        </label>
        <label class="create-field">
          <span>表名称</span>
          <input v-model="recordForm.表名称" data-testid="record-table" required />
        </label>
        <label class="create-field">
          <span>分区值</span>
          <input v-model="recordForm.分区值" data-testid="record-partition" placeholder="如 2026-11-10" required />
        </label>
        <label class="create-field">
          <span>事件类型</span>
          <select v-model="recordForm.事件类型" data-testid="record-type">
            <option v-for="option in eventTypes" :key="option" :value="option">{{ option }}</option>
          </select>
        </label>
        <label class="create-field">
          <span>状态</span>
          <select v-model="recordForm.状态" data-testid="record-status">
            <option v-for="option in statusOptions" :key="option" :value="option">{{ option }}</option>
          </select>
        </label>
        <label class="create-field">
          <span>重复写入次数（验证视图去重）</span>
          <input v-model.number="recordForm.重复次数" data-testid="record-repeat" type="number" min="1" max="9" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showRecord = false">取消</button>
          <button class="btn primary" data-testid="submit-record" type="submit" :disabled="submitting">
            {{ submitting ? '提交中…' : '确认提交' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="detail" class="modal-backdrop" data-testid="detail-modal" @click.self="detail = null">
      <div class="modal">
        <h3>分区状态详情</h3>
        <dl class="dag-detail">
          <template v-for="item in detailItems" :key="item.label">
            <dt>{{ item.label }}</dt>
            <dd :data-testid="`detail-field-${item.label}`">{{ item.value || '—' }}</dd>
          </template>
        </dl>
        <h4>失败重试链 / 修复批次</h4>
        <table class="data-table">
          <thead>
            <tr><th>事件类型</th><th>状态</th><th>批次编号</th><th>事件时间</th><th>失败原因</th></tr>
          </thead>
          <tbody>
            <tr v-for="(chain, index) in retryChain" :key="index">
              <td>{{ chain.事件类型 }}</td>
              <td>{{ chain.状态 }}</td>
              <td>{{ chain.批次编号 }}</td>
              <td>{{ chain.事件时间 }}</td>
              <td>{{ chain.失败原因 || '—' }}</td>
            </tr>
            <tr v-if="!retryChain.length">
              <td colspan="5" class="empty-state">暂无失败或修复记录</td>
            </tr>
          </tbody>
        </table>
        <h4>原始刷新事件（含重复写入）</h4>
        <p class="page-desc">共 {{ detail.events.length }} 条事件，历史视图记录 {{ detail.history.length }} 条均可回查。</p>
        <div class="page-actions">
          <button class="btn primary" type="button" data-testid="close-detail" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 个分区当前状态（刷新页面后同分区仅一条）</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type StatusRow = Record<string, string | number>
type Dashboard = {
  cards: { label: string; value: number }[]
  count_by_status: Record<string, number>
  tables: Record<string, unknown>[]
  failed_partitions: Record<string, unknown>[]
}
type Detail = {
  current: StatusRow
  events: Record<string, unknown>[]
  history: Record<string, unknown>[]
}

const ENDPOINT = '/api/dwd_refresh_status'
const statusOptions = ['待刷新', '刷新成功', '刷新失败', '稽核失败', '已修复']
const eventTypes = ['定时刷新', '补数事件', '质量稽核']
const scenes = [
  { status: '待刷新', label: '场景一：待刷新分区（按水位）' },
  { status: '刷新失败', label: '场景二：刷新失败分区' },
  { status: '稽核失败', label: '场景三：质量稽核失败分区' },
  { status: '已修复', label: '场景四：补数已修复分区' },
  { status: '刷新成功', label: '场景五：刷新成功分区' },
]

const rows = ref<StatusRow[]>([])
const total = ref(0)
const cards = ref<Dashboard['cards']>([])
const boundary = ref<'wide' | 'narrow'>('wide')
const errorMessage = ref('')
const showRecord = ref(false)
const submitting = ref(false)
const detail = ref<Detail | null>(null)
const filters = reactive<Record<string, string>>({ table_name: '', keyword: '', status: '' })
const recordForm = reactive({
  批次编号: `DWD-MANUAL-${new Date().toISOString().slice(0, 10).replace(/-/g, '')}`,
  表名称: 'dwd_order_detail',
  分区值: new Date().toISOString().slice(0, 10),
  事件类型: '定时刷新',
  状态: '待刷新',
  重复次数: 1,
})

const columns = computed(() => boundary.value === 'narrow'
  ? ['表名称', '分区值', '刷新水位', '当前状态', '完成时间', '批次编号']
  : ['表名称', '分区值', '刷新水位', '当前状态', '完成时间', '事件类型', '修复批次', '重试次数', '稽核范围', '批次编号'])

const grouped = computed(() => {
  const bucket: Record<string, StatusRow[]> = {}
  for (const row of rows.value) {
    const key = String(row.当前状态)
    ;(bucket[key] ??= []).push(row)
  }
  return bucket
})

const retryChain = computed(() => {
  const chain = detail.value?.current?.['失败重试链']
  return Array.isArray(chain) ? chain as Record<string, string>[] : []
})

const detailItems = computed(() => {
  const current = detail.value?.current ?? {}
  return [
    { label: '表名称', value: current['表名称'] },
    { label: '分区值', value: current['分区值'] },
    { label: '刷新水位', value: current['刷新水位'] },
    { label: '当前状态', value: current['当前状态'] },
    { label: '事件类型', value: current['事件类型'] },
    { label: '完成时间', value: current['完成时间'] },
    { label: '修复批次', value: current['修复批次'] },
    { label: '稽核范围', value: current['稽核范围'] },
    { label: '批次编号', value: current['批次编号'] },
  ]
})

function display(row: StatusRow, column: string): string {
  const value = row[column]
  if (value === undefined || value === null || value === '') return '—'
  return String(value)
}

function resetFilters() {
  filters.table_name = ''
  filters.keyword = ''
  filters.status = ''
  void reload()
}

function toggleBoundary() {
  boundary.value = boundary.value === 'wide' ? 'narrow' : 'wide'
  void reload()
}

function openRecord() {
  errorMessage.value = ''
  showRecord.value = true
}

async function submitRecord() {
  submitting.value = true
  errorMessage.value = ''
  try {
    const repeat = Math.max(1, Number(recordForm.重复次数) || 1)
    const events = Array.from({ length: repeat }, (_, index) => ({
      批次编号: recordForm.批次编号,
      事件类型: recordForm.事件类型,
      表名称: recordForm.表名称,
      分区值: recordForm.分区值,
      刷新水位: recordForm.分区值,
      状态: recordForm.状态,
      事件序号: index + 1,
    }))
    const response = await request(`${ENDPOINT}/events`, {
      method: 'POST',
      body: JSON.stringify({ 批次编号: recordForm.批次编号, events }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '刷新事件提交失败')
    showRecord.value = false
    await Promise.all([reload(), reloadDashboard()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '刷新事件提交失败'
  } finally {
    submitting.value = false
  }
}

async function openDetail(row: StatusRow) {
  errorMessage.value = ''
  try {
    const params = new URLSearchParams({
      table_name: String(row.表名称),
      partition_value: String(row.分区值),
    })
    detail.value = await fetchJson<Detail>(`${ENDPOINT}/detail?${params.toString()}`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分区状态详情读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams({ boundary: boundary.value })
  if (filters.table_name) params.set('table_name', filters.table_name)
  if (filters.keyword) params.set('keyword', filters.keyword)
  if (filters.status) params.set('status', filters.status)
  try {
    const payload = await fetchJson<{ items: StatusRow[]; total: number }>(`${ENDPOINT}/view?${params.toString()}`)
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '明细刷新状态视图读取失败'
  }
}

async function reloadDashboard() {
  try {
    const payload = await fetchJson<Dashboard>(`${ENDPOINT}/dashboard`)
    cards.value = payload.cards ?? []
  } catch {
    cards.value = []
  }
}

onMounted(() => {
  void reload()
  void reloadDashboard()
})
</script>

<style scoped>
.refresh-sections {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.refresh-section {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
}
.section-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.section-count {
  color: var(--muted);
  font-size: 12px;
}
.refresh-section h4 {
  margin: 12px 0 6px;
}
</style>
