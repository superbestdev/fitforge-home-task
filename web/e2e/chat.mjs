import { chromium } from 'playwright'

const SHOTS = process.argv[2] || './shots'
const EMAIL = process.argv[3] || 'alex.smith@example.com'

const errors = []
const failures = []

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 } })
const page = await ctx.newPage()

page.on('console', (m) => {
  if (m.type() === 'error') errors.push(m.text())
})
page.on('pageerror', (e) => errors.push(`PAGEERROR: ${e.message}`))
page.on('requestfailed', (r) =>
  failures.push(`${r.method()} ${r.url()} :: ${r.failure()?.errorText}`))

const step = async (name, fn) => {
  process.stdout.write(`  ${name} ... `)
  try {
    await fn()
    console.log('ok')
  } catch (e) {
    console.log(`FAILED: ${e.message.split('\n')[0]}`)
    await page.screenshot({ path: `${SHOTS}/FAIL-${name}.png` })
    throw e
  }
}

console.log('\n=== CUSTOMER CHAT (localhost:5173) ===')

await step('load', async () => {
  await page.goto('http://localhost:5173', { waitUntil: 'networkidle' })
  await page.waitForSelector('.startcard h1', { timeout: 15000 })
})
await page.screenshot({ path: `${SHOTS}/chat-1-start.png` })

await step('start-session', async () => {
  await page.fill('#email', EMAIL)
  await page.click('button:has-text("Start chat")')
  await page.waitForSelector('.messages .msg.agent', { timeout: 20000 })
})
await page.screenshot({ path: `${SHOTS}/chat-2-greeting.png` })

await step('send-first-message', async () => {
  await page.fill('.composer textarea', 'my treadmill belt keeps slipping when I run on it')
  await page.click('.composer button')
  // Wait for a real reply, not the thinking indicator: an agent bubble that
  // is not .thinking and actually has text.
  await page.waitForFunction(() => {
    const bubbles = [...document.querySelectorAll('.messages .msg.agent .bubble')]
      .filter((b) => !b.classList.contains('thinking'))
    return bubbles.length >= 2 && bubbles[bubbles.length - 1].innerText.trim().length > 10
  }, null, { timeout: 300000 })
  await page.waitForTimeout(1000)
})
await page.screenshot({ path: `${SHOTS}/chat-3-reply.png`, fullPage: false })

const state = await page.evaluate(() => ({
  messages: [...document.querySelectorAll('.messages .msg')].map((m) => ({
    role: m.className.replace('msg ', '').trim(),
    text: m.innerText.slice(0, 220),
  })),
  citations: [...document.querySelectorAll('.cite')].map((c) => c.innerText),
  issues: [...document.querySelectorAll('.issue')].map((i) => i.innerText.replace(/\n/g, ' | ')),
}))

console.log('\n  --- rendered state ---')
for (const m of state.messages) console.log(`  [${m.role}] ${m.text.replace(/\n/g, ' ')}`)
console.log(`  citations: ${JSON.stringify(state.citations)}`)
console.log(`  issue panel: ${JSON.stringify(state.issues)}`)

console.log(`\n  console errors: ${errors.length}`)
errors.slice(0, 8).forEach((e) => console.log(`    ! ${e.slice(0, 180)}`))
console.log(`  failed requests: ${failures.length}`)
failures.slice(0, 8).forEach((f) => console.log(`    ! ${f.slice(0, 180)}`))

await browser.close()
if (errors.length || failures.length) process.exitCode = 2
