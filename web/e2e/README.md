# Browser end-to-end tests

Playwright scripts that drive the two UIs against a running stack. They exist
because the unit tests and golden sessions both pass on code paths that are
still broken in a browser — and these caught two real bugs on their first run:

1. **The card form vanished after a decline.** The agent said "would you like to
   try another card?" while the widget removed the form. `requires_payment` was
   not carried on the websocket frame for a failed payment.

2. **A declined card poisoned the quote permanently.** The idempotency key
   covered the whole quote, so the payment provider replayed the stored decline
   for every later attempt, with any card, forever. The customer could never
   pay. The key now covers a single attempt.

Neither was visible from the API alone; both needed a real browser clicking a
real button.

## Running

The stack must be up, seeded and ingested first.

```bash
npm install
npx playwright install chromium

npm run chat       # identification, diagnosis, citations, issue panel
npm run console    # handoff packet + manual upload, incl. coverage change
npm run payment    # quote -> decline -> retry -> order placed
```

Screenshots land in `./shots`, including a `FAIL-<step>.png` for any failure.
Every script also asserts **zero console errors and zero failed requests**, and
exits non-zero otherwise, so they can gate CI.

## Notes

- Turns take 15-30 s on CPU inference, so the waits are deliberately long.
- The scripts wait for a *non-thinking* agent bubble with real text. Waiting for
  "an agent bubble" matches the typing indicator and passes far too early.
- `console.mjs` skips handoffs with zero issue threads: a customer who opens
  with "get me a human" escalates before any issue exists, which is correct
  behaviour but a poor test subject.
