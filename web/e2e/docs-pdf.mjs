/**
 * Print the /docs page to a PDF.
 *
 * The page is already the source of truth for the architecture overview, so
 * this renders that rather than maintaining a second document that would drift.
 * Chromium does the layout, which means the print stylesheet in
 * web/docs/index.html is what controls how it paginates.
 *
 *   node docs-pdf.mjs [out.pdf]
 */
import { chromium } from 'playwright'

const OUT = process.argv[2] || '../../docs/FitForge_Architecture_Overview.pdf'
const URL = process.env.DOCS_URL || 'http://localhost:5173/docs/'

const browser = await chromium.launch()
const page = await browser.newPage()

const problems = []
page.on('requestfailed', (r) => problems.push(`${r.url()} :: ${r.failure()?.errorText}`))

await page.goto(URL, { waitUntil: 'networkidle' })

// Webfonts are the usual cause of a PDF that looks nothing like the page —
// layout is measured before they land and the metrics shift underneath it.
await page.evaluate(() => document.fonts.ready)
await page.emulateMedia({ media: 'print', colorScheme: 'light' })
await page.waitForTimeout(400)

await page.pdf({
  path: OUT,
  format: 'A4',
  printBackground: true,
  preferCSSPageSize: true,
  displayHeaderFooter: true,
  headerTemplate: '<div></div>',
  footerTemplate: `
    <div style="width:100%;padding:0 14mm;font:8pt Archivo,Arial,sans-serif;color:#6B7784;
                display:flex;justify-content:space-between;">
      <span>FitForge · Agentic Customer Support · Doc 00</span>
      <span class="pageNumber"></span>
    </div>`,
})

console.log(`wrote ${OUT}`)
if (problems.length) {
  console.log(`  ${problems.length} asset(s) failed to load:`)
  problems.slice(0, 6).forEach((p) => console.log('   !', p.slice(0, 160)))
}
await browser.close()
