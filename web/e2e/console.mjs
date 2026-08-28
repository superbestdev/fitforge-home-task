import { chromium } from 'playwright'

const SHOTS = process.argv[2] || './shots'
const PDF = process.argv[3]

const errors = []
const failures = []

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1500, height: 980 } })
const page = await ctx.newPage()

page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', (e) => errors.push(`PAGEERROR: ${e.message}`))
page.on('requestfailed', (r) =>
  failures.push(`${r.method()} ${r.url()} :: ${r.failure()?.errorText}`))

const step = async (name, fn) => {
  process.stdout.write(`  ${name} ... `)
  try { await fn(); console.log('ok') }
  catch (e) {
    console.log(`FAILED: ${e.message.split('\n')[0]}`)
    await page.screenshot({ path: `${SHOTS}/FAIL-${name}.png` })
    throw e
  }
}

console.log('\n=== AGENT CONSOLE (localhost:5173/console) ===')

await step('load', async () => {
  await page.goto('http://localhost:5173/console/', { waitUntil: 'networkidle' })
  await page.waitForSelector('.brand strong', { timeout: 15000 })
})

await step('metrics-render', async () => {
  await page.waitForSelector('.stats .stat', { timeout: 15000 })
})

await step('queue-populated', async () => {
  await page.waitForSelector('.qitem', { timeout: 15000 })
})
await page.screenshot({ path: `${SHOTS}/console-1-queue.png` })

const queueCount = await page.locator('.qitem').count()
console.log(`  queue items: ${queueCount}`)

await step('open-handoff-with-history', async () => {
  // Some handoffs legitimately carry no threads — a customer who opens with
  // "get me a human" escalates before any issue exists. Pick one that has them.
  const items = await page.locator('.qitem').count()
  let opened = false
  for (let i = 0; i < items; i++) {
    const label = await page.locator('.qitem').nth(i).innerText()
    if (/^0 issue/m.test(label) || label.includes('0 issue(s)')) continue
    await page.locator('.qitem').nth(i).click()
    await page.waitForSelector('.packet .pkthead h1', { timeout: 20000 })
    try {
      await page.waitForSelector('.thread', { timeout: 8000 })
    } catch { continue }
    if (await page.locator('.steps li').count() > 0) { opened = true; break }
  }
  if (!opened) throw new Error('no handoff in the queue carries diagnostic steps')
})
await page.screenshot({ path: `${SHOTS}/console-2-packet.png` })

const packet = await page.evaluate(() => ({
  customer: document.querySelector('.pkthead h1')?.innerText,
  sections: [...document.querySelectorAll('section h3')].map((h) => h.innerText.trim()),
  threads: [...document.querySelectorAll('.thread')].map((t) =>
    t.innerText.split('\n').slice(0, 3).join(' | ')),
  steps: [...document.querySelectorAll('.steps li')].length,
}))
console.log(`  customer: ${packet.customer}`)
console.log(`  sections: ${JSON.stringify(packet.sections)}`)
console.log(`  threads:  ${JSON.stringify(packet.threads)}`)
console.log(`  diagnostic steps rendered: ${packet.steps}`)

// ---- Manuals tab ----------------------------------------------------------
console.log('\n  --- manuals tab ---')

await step('switch-to-manuals', async () => {
  await page.click('.vtab:has-text("Manuals")')
  await page.waitForSelector('.dropzone', { timeout: 15000 })
})
await page.screenshot({ path: `${SHOTS}/console-3-manuals.png` })

const before = await page.evaluate(() => ({
  stats: [...document.querySelectorAll('.stat')].map((s) => s.innerText.replace('\n', '=')),
  backfill: document.querySelectorAll('.row').length,
  firstGap: document.querySelector('.row')?.innerText.split('\n')[0],
}))
console.log(`  coverage stats: ${JSON.stringify(before.stats)}`)
console.log(`  backfill rows: ${before.backfill}, first: ${before.firstGap}`)

if (PDF) {
  const jobsBefore = await page.locator('.jobrow').count()

  await step('upload-pdf', async () => {
    await page.setInputFiles('input[type=file]', PDF)
    // Wait for a NEW row to appear, then for THAT row to finish. Checking the
    // first row alone races against the list still showing the previous state.
    await page.waitForFunction(
      (n) => document.querySelectorAll('.jobrow').length > n,
      jobsBefore, { timeout: 30000 })
    await page.waitForFunction(() => {
      const row = document.querySelector('.jobrow')
      const badge = row?.querySelector('.badge')
      return badge && /^(done|failed)$/i.test(badge.innerText.trim())
    }, null, { timeout: 300000 })
    await page.waitForTimeout(2500)   // let the final refresh land
  })
  await page.screenshot({ path: `${SHOTS}/console-4-uploaded.png` })
  await page.locator('.jobrow').first().scrollIntoViewIfNeeded()
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${SHOTS}/console-5-jobrow.png` })

  const job = await page.evaluate(() => {
    const row = document.querySelector('.jobrow')
    return {
      text: row?.innerText.replace(/\n/g, ' | '),
      mismatch: document.querySelector('.mismatch')?.innerText || null,
    }
  })
  console.log(`  job: ${job.text}`)
  if (job.mismatch) console.log(`  mismatch banner: ${job.mismatch}`)

  const after = await page.evaluate(() =>
    [...document.querySelectorAll('.stat')].map((s) => s.innerText.replace('\n', '=')))
  console.log(`  coverage after: ${JSON.stringify(after)}`)
}

console.log(`\n  console errors: ${errors.length}`)
errors.slice(0, 10).forEach((e) => console.log(`    ! ${e.slice(0, 200)}`))
console.log(`  failed requests: ${failures.length}`)
failures.slice(0, 10).forEach((f) => console.log(`    ! ${f.slice(0, 200)}`))

await browser.close()
if (errors.length || failures.length) process.exitCode = 2
