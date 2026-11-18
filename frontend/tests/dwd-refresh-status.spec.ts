import { expect, test } from '@playwright/test'

/**
 * 明细刷新状态视图验收（需求：视图按水位展示待刷新与失败分区；同分区只有一条当前状态；
 * 点击状态可看水位与稽核范围；重复事件不膨胀；宽窄边界；明细层管理入口进入面板复核）。
 */
test('明细刷新状态视图：看板、场景分区、去重、边界切换、详情复核', async ({ page }) => {
  test.setTimeout(120_000)
  const browserErrors: string[] = []
  page.on('pageerror', (e) => browserErrors.push(e.message))
  page.on('response', (r) => {
    if (r.url().includes('/api/') && r.status() >= 400) browserErrors.push(`${r.status()} ${r.url()}`)
  })

  // 1. 明细层管理入口打开并提交刷新构建，再进入明细刷新状态视图面板复核
  await page.goto('/dwd_layer')
  await expect(page.locator('tbody tr[data-testid^="row-"]')).toHaveCount(3)
  await page.getByTestId('goto-refresh-status').click()
  await expect(page).toHaveURL(/dwd_refresh_status/)
  await expect(page.locator('[data-module="dwd_refresh_status"]')).toBeVisible()

  // 2. 看板摘要渲染出场景卡片
  const cards = page.locator('.stat-card .stat-value')
  await expect(cards.first()).toBeVisible()
  await expect(page.getByTestId('card-待刷新分区')).toBeVisible()
  await expect(page.getByTestId('card-失败分区')).toBeVisible()

  // 3. 视图按场景分段且每段都有分区行（示例引导覆盖全部场景中的多个）
  await expect(page.locator('[data-testid^="section-"]')).toHaveCount(5)
  const statusRows = page.locator('tbody tr[data-testid^="status-row-"]')
  await expect(statusRows.first()).toBeVisible()
  const initialCount = await statusRows.count()
  expect(initialCount).toBeGreaterThan(0)

  // 4. 较宽边界包含修复批次/重试链列，切换到较窄边界后这些列消失
  await expect(page.getByRole('columnheader', { name: '修复批次' }).first()).toBeVisible()
  await page.getByTestId('toggle-boundary').click()
  await expect(page.getByRole('columnheader', { name: '修复批次' })).toHaveCount(0)
  await expect(page.getByRole('columnheader', { name: '当前状态' }).first()).toBeVisible()
  // 切回较宽边界以便看详情
  await page.getByTestId('toggle-boundary').click()

  // 5. 刷新页面后同一分区只出现一条当前状态（取第一行分区做唯一性校验）
  const firstRow = statusRows.first()
  const tableName = (await firstRow.getByRole('cell').nth(0).innerText()).trim()
  const partition = (await firstRow.getByRole('cell').nth(1).innerText()).trim()
  await page.reload()
  await expect(page.locator(`[data-testid="status-row-${tableName}-${partition}"]`)).toHaveCount(1)

  // 6. 点击状态查看详情：能看到对应水位和稽核范围
  await page.locator(`[data-testid="detail-${tableName}-${partition}"]`).click()
  const detailModal = page.getByTestId('detail-modal')
  await expect(detailModal).toBeVisible()
  await expect(detailModal.getByTestId('detail-field-刷新水位')).toContainText(partition)
  await expect(detailModal.getByTestId('detail-field-表名称')).toContainText(tableName)
  await expect(detailModal.getByTestId('detail-field-稽核范围')).toBeVisible()

  // 7. 登记同一分区的重复刷新事件（3 次），视图该分区仍然只有一条
  await page.getByTestId('close-detail').click()
  await page.getByTestId('open-record').click()
  const modal = page.getByTestId('record-modal')
  await expect(modal).toBeVisible()
  await page.getByTestId('record-table').fill('dwd_order_detail')
  await page.getByTestId('record-partition').fill('2030-01-01')
  await page.getByTestId('record-repeat').fill('3')
  await page.getByTestId('submit-record').click()
  await expect(modal).toHaveCount(0)
  await expect(page.locator('[data-testid="status-row-dwd_order_detail-2030-01-01"]')).toHaveCount(1)

  // 8. 用分区关键字过滤，只看到该分区
  await page.getByTestId('filter-keyword').fill('2030-01-01')
  await page.getByTestId('query').click()
  await expect(page.locator('tbody tr[data-testid^="status-row-"]')).toHaveCount(1)

  expect(browserErrors).toEqual([])
})
