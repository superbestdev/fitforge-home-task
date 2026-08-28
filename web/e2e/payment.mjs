import { chromium } from 'playwright'

const SHOTS = process.argv[2] || './shots'
const EMAIL = process.argv[3] || 'dawn.clark@example.com'

const errors = []
const failures = []

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 } })
const page = await ctx.newPage()

page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', (e) => errors.push(`PAGEERROR: ${e.message}`))
page.on('requestfailed', (r) =>
  failures.push(`${r.method()} ${r.url()} :: ${r.failure()?.errorText}`))

const agentBubbles = () =>
  page.evaluate(() =>
    [...document.querySelectorAll('.messages .msg.agent .bubble')]
      .filter((b) => !b.classList.contains('thinking'))
      .map((b) => b.innerText.trim()))

const say = async (text, label) => {
  const before = (await agentBubbles()).length
  process.stdout.write(`  ${label} ... `)
  await page.fill('.composer textarea', text)
  await page.click('.composer button')
  await page.waitForFunction((n) => {
    const b = [...document.querySelectorAll('.messages .msg.agent .bubble')]
      .filter((x) => !x.classList.contains('thinking'))
    return b.length > n && b[b.length - 1].innerText.trim().length > 5
  }, before, { timeout: 300000 })
  await page.waitForTimeout(700)
  const all = await agentBubbles()
  console.log('ok')
  console.log(`    > ${all[all.length - 1].replace(/\n/g, ' | ').slice(0, 260)}`)
  return all[all.length - 1]
}

const step = async (name, fn) => {
  process.stdout.write(`  ${name} ... `)
  try { await fn(); console.log('ok') }
  catch (e) {
    console.log(`FAILED: ${e.message.split('\n')[0]}`)
    await page.screenshot({ path: `${SHOTS}/FAIL-${name}.png` })
    throw e
  }
}

console.log('\n=== COMMERCE / PAYMENT FLOW ===')

await step('start', async () => {
  await page.goto('http://localhost:5173', { waitUntil: 'networkidle' })
  await page.fill('#email', EMAIL)
  await page.click('button:has-text("Start chat")')
  await page.waitForSelector('.messages .msg.agent', { timeout: 20000 })
})

await say('my treadmill belt keeps slipping badly when I run', 'describe-fault')
await say('can I just order a replacement running belt', 'ask-to-order')

await step('quote-shown', async () => {
  const last = (await agentBubbles()).slice(-1)[0]
  if (!/total/i.test(last)) throw new Error(`no quote in reply: ${last.slice(0, 160)}`)
})
await page.screenshot({ path: `${SHOTS}/pay-1-quote.png` })

await say('yes please go ahead', 'confirm-order')

await step('payment-form-appears', async () => {
  await page.waitForSelector('.paybox', { timeout: 20000 })
})
await page.screenshot({ path: `${SHOTS}/pay-2-cardform.png` })

const cards = await page.locator('.paybox .cards button').allInnerTexts()
console.log(`  card options: ${JSON.stringify(cards)}`)

// First try a card that must be declined — the failure path matters more.
await step('declined-card-is-handled', async () => {
  const before = (await agentBubbles()).length
  await page.click('.paybox button:has-text("insufficient funds")')
  await page.waitForFunction((n) =>
    [...document.querySelectorAll('.messages .msg.agent .bubble')]
      .filter((x) => !x.classList.contains('thinking')).length > n,
    before, { timeout: 120000 })
  await page.waitForTimeout(600)
  const last = (await agentBubbles()).slice(-1)[0]
  console.log(`    > ${last.replace(/\n/g, ' | ').slice(0, 200)}`)
  if (!/declin/i.test(last)) throw new Error('decline was not reported to the customer')
})
await page.screenshot({ path: `${SHOTS}/pay-3-declined.png` })

// Then the approving card.
await step('approved-card-places-order', async () => {
  const before = (await agentBubbles()).length
  const box = await page.locator('.paybox').count()
  if (box === 0) {
    // The decline path may have cleared the form; re-trigger it.
    await page.fill('.composer textarea', 'yes please go ahead')
    await page.click('.composer button')
    await page.waitForSelector('.paybox', { timeout: 120000 })
  }
  await page.click('.paybox button:has-text("Approves")')
  await page.waitForFunction((n) =>
    [...document.querySelectorAll('.messages .msg.agent .bubble')]
      .filter((x) => !x.classList.contains('thinking')).length > n,
    before, { timeout: 180000 })
  await page.waitForTimeout(900)
  const last = (await agentBubbles()).slice(-1)[0]
  console.log(`    > ${last.replace(/\n/g, ' | ').slice(0, 280)}`)
  if (!/PO-/.test(last)) throw new Error('no order number in the confirmation')
})
await page.screenshot({ path: `${SHOTS}/pay-4-ordered.png` })

const issues = await page.evaluate(() =>
  [...document.querySelectorAll('.issue')].map((i) => i.innerText.replace(/\n/g, ' | ')))
console.log(`  issue panel: ${JSON.stringify(issues)}`)

console.log(`\n  console errors: ${errors.length}`)
errors.slice(0, 8).forEach((e) => console.log(`    ! ${e.slice(0, 200)}`))
console.log(`  failed requests: ${failures.length}`)
failures.slice(0, 8).forEach((f) => console.log(`    ! ${f.slice(0, 200)}`))

await browser.close()
if (errors.length || failures.length) process.exitCode = 2
