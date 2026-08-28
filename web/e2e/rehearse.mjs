/**
 * Demo rehearsal. Drives the full video scenario and prints, for every beat,
 * the agent's actual reply and the wall-clock it took.
 *
 * Run it before recording. It mutates state (it uploads a manual and places an
 * order), so reset afterwards with:  ./run.ps1 demo-reset
 *
 *   node rehearse.mjs
 */
import { chromium } from 'playwright'

const EMAIL = 'james.maldonado@example.com'
const BIKE_PDF = process.argv[2]
  || 'd:/work/projects/FitForge/data/sample/FitForge_Sample_Bike_Manual.pdf'

const errs = []
const marks = []

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 } })
const page = await ctx.newPage()
page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()) })
page.on('pageerror', (e) => errs.push('PAGEERROR: ' + e.message))

const bubbles = () => page.evaluate(() =>
  [...document.querySelectorAll('.messages .msg.agent .bubble')]
    .filter((b) => !b.classList.contains('thinking'))
    .map((b) => b.innerText.trim()))

const say = async (label, text) => {
  const before = (await bubbles()).length
  const t0 = Date.now()
  await page.fill('.composer textarea', text)
  await page.click('.composer button')
  await page.waitForFunction((n) => {
    const b = [...document.querySelectorAll('.messages .msg.agent .bubble')]
      .filter((x) => !x.classList.contains('thinking'))
    return b.length > n && b[b.length - 1].innerText.trim().length > 3
  }, before, { timeout: 300000 })
  const secs = (Date.now() - t0) / 1000
  await page.waitForTimeout(500)
  const all = await bubbles()
  const reply = all[all.length - 1]
  marks.push({ label, secs })
  console.log(`\n[${secs.toFixed(1)}s] ${label}`)
  console.log(`  YOU:   ${text}`)
  console.log(`  AGENT: ${reply.replace(/\n+/g, ' | ').slice(0, 400)}`)
  return reply
}

const startSession = async () => {
  await page.goto('http://localhost:5173/', { waitUntil: 'networkidle' })
  await page.fill('#email', EMAIL)
  await page.click('button:has-text("Start chat")')
  await page.waitForSelector('.messages .msg.agent', { timeout: 20000 })
}

const issuePanel = async () => page.evaluate(() =>
  [...document.querySelectorAll('.issue')].map((i) => i.innerText.replace(/\n/g, ' | ')))

// ===========================================================================
console.log('\n============ ACT 1 — the gap (Velodrome has no manual) ============')
await startSession()
await say('opens with the bike fault',
  'my Velodrome bike is making a grinding noise from the flywheel')
let last = (await bubbles()).slice(-1)[0]
if (/which|two|both|300 S|Circuit/i.test(last) && !/grinding|check|confirm/i.test(last)) {
  await say('picks the machine', 'the Velodrome 300 S')
}
console.log('  issues:', JSON.stringify(await issuePanel()))

// ===========================================================================
console.log('\n============ ACT 2 — upload the manual ============')
{
  const t0 = Date.now()
  await page.goto('http://localhost:5173/console/', { waitUntil: 'networkidle' })
  await page.click('.vtab:has-text("Manuals")')
  await page.waitForSelector('.dropzone')
  const before = await page.evaluate(() =>
    [...document.querySelectorAll('.stat')].map((s) => s.innerText.replace(/\n/g, '=')))
  console.log('  coverage before:', JSON.stringify(before.slice(-3)))
  await page.setInputFiles('input[type=file]', BIKE_PDF)
  // Poll the job API rather than the DOM. The panel only re-polls while a job
  // is active, so a fast ingest can finish between renders and the row never
  // passes through a state the DOM watcher can catch.
  await page.waitForFunction(async () => {
    const r = await fetch('/api/manuals/jobs').then((x) => x.json())
    return (r.jobs || []).some((j) => ['done', 'failed', 'awaiting_model'].includes(j.status))
  }, null, { timeout: 300000, polling: 1500 })
  await page.reload({ waitUntil: 'networkidle' })
  await page.click('.vtab:has-text("Manuals")')
  await page.waitForSelector('.jobrow', { timeout: 20000 })
  const secs = (Date.now() - t0) / 1000
  marks.push({ label: 'upload + ingest', secs })
  const row = await page.evaluate(() =>
    document.querySelector('.jobrow')?.innerText.replace(/\n/g, ' | '))
  console.log(`\n[${secs.toFixed(1)}s] upload + ingest`)
  console.log('  job:', row)
  await page.waitForTimeout(2500)
  const after = await page.evaluate(() =>
    [...document.querySelectorAll('.stat')].map((s) => s.innerText.replace(/\n/g, '=')))
  console.log('  coverage after: ', JSON.stringify(after.slice(-3)))
}

// ===========================================================================
console.log('\n============ ACT 3 — same question, real answer ============')
await startSession()
await say('same fault, now backed',
  'my Velodrome 300 S bike is making a grinding noise from the flywheel')
await say('answers the check', 'yes I did that and it still grinds')

console.log('\n--- second machine, mid-diagnosis ---')
await say('raises a second, unrelated issue',
  'also my Circuit 100 Pro bike screen keeps going blank')
console.log('  issues:', JSON.stringify(await issuePanel()))

await say('switches back', 'can we go back to the Velodrome')

// ===========================================================================
console.log('\n============ ACT 4 — warranty, decline, order ============')
await say('asks to buy a part', 'can I just order a new pedal set for the Velodrome')
const quote = (await bubbles()).slice(-1)[0]
console.log('  quote has a total:', /total/i.test(quote))
await say('confirms', 'yes please go ahead')

if (await page.locator('.paybox').count()) {
  let t0 = Date.now()
  let before = (await bubbles()).length
  await page.click('.paybox button:has-text("insufficient funds")')
  await page.waitForFunction((n) => [...document.querySelectorAll('.messages .msg.agent .bubble')]
    .filter((x) => !x.classList.contains('thinking')).length > n, before, { timeout: 180000 })
  marks.push({ label: 'declined card', secs: (Date.now() - t0) / 1000 })
  console.log(`\n[${((Date.now() - t0) / 1000).toFixed(1)}s] declined card`)
  console.log('  AGENT:', (await bubbles()).slice(-1)[0].replace(/\n+/g, ' | ').slice(0, 260))

  await page.waitForTimeout(800)
  if (!(await page.locator('.paybox').count())) await say('retries', 'yes please go ahead')
  t0 = Date.now(); before = (await bubbles()).length
  await page.click('.paybox button:has-text("Approves")')
  await page.waitForFunction((n) => [...document.querySelectorAll('.messages .msg.agent .bubble')]
    .filter((x) => !x.classList.contains('thinking')).length > n, before, { timeout: 240000 })
  marks.push({ label: 'approved card', secs: (Date.now() - t0) / 1000 })
  console.log(`\n[${((Date.now() - t0) / 1000).toFixed(1)}s] approved card`)
  console.log('  AGENT:', (await bubbles()).slice(-1)[0].replace(/\n+/g, ' | ').slice(0, 320))
} else {
  console.log('  !! no payment form appeared — check the warranty verdict above')
}

// ===========================================================================
console.log('\n============ ACT 5 — safety stop (no model call) ============')
await say('safety phrase', 'wait, I can smell burning coming from the motor housing')

console.log('\n============ TIMINGS ============')
let total = 0
for (const m of marks) { total += m.secs; console.log(`  ${m.secs.toFixed(1).padStart(6)}s  ${m.label}`) }
console.log(`  ${total.toFixed(1).padStart(6)}s  TOTAL agent time (excludes your narration)`)
console.log(`\n  console errors: ${errs.length}`)
errs.slice(0, 5).forEach((e) => console.log('   !', e.slice(0, 160)))

await browser.close()
