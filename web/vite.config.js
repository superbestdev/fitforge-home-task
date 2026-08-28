import { resolve } from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Everything is served by this one dev server as separate documents:
//   /          -> index.html          -> src/chat
//   /console/  -> console/index.html  -> src/console
//   /docs/     -> docs/index.html     -> a static page, no bundle
//
// They stay separate documents rather than becoming one SPA with a router
// because their stylesheets are independent and share plenty of generic class
// names (.row, .badge, .muted). Two documents means neither can ever restyle
// the other; one document would have needed both sheets namespaced first.
//
// /api and /ws are proxied to the API so the browser only ever talks to this
// origin. That removes CORS from the picture entirely and means the frontend
// carries no absolute backend URL to get wrong.
const API_ORIGIN = process.env.API_ORIGIN || 'http://localhost:8000'

// Vite serves a request only when its Host header is a loopback name or is
// listed here. That is DNS-rebinding protection, not bureaucracy: without it a
// page anywhere on the internet could point its own domain at 127.0.0.1 and
// read this dev server's responses — your source, your .env-derived config —
// out of a visitor's browser.
//
// A tunnel (ngrok, Cloudflare, Tailscale Funnel) arrives carrying its own
// hostname, so it has to be named. Set WEB_ALLOWED_HOSTS to a comma-separated
// list in .env; a leading dot covers a domain and all of its subdomains, which
// is what you want for ngrok since the subdomain changes:
//
//   WEB_ALLOWED_HOSTS=.ngrok-free.dev,.trycloudflare.com
//
// Do not reach for `true` here. It disables the check for every host, which is
// the whole vulnerability.
const ALLOWED_HOSTS = (process.env.WEB_ALLOWED_HOSTS || '')
  .split(',')
  .map((h) => h.trim())
  .filter(Boolean)

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      input: {
        chat: resolve(__dirname, 'index.html'),
        console: resolve(__dirname, 'console/index.html'),
        // Plain HTML with an inline stylesheet — no module graph of its own,
        // but it still needs to be an entry or `vite build` drops it.
        docs: resolve(__dirname, 'docs/index.html'),
      },
    },
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    allowedHosts: ALLOWED_HOSTS,
    // Behind a TLS tunnel the page is served over https, so the HMR socket has
    // to be wss on 443 rather than ws on 5173 — the port the dev server would
    // otherwise advertise is not the port the browser can reach.
    hmr: process.env.WEB_PUBLIC_HOST
      ? { protocol: 'wss', host: process.env.WEB_PUBLIC_HOST, clientPort: 443 }
      : undefined,
    // The source tree is bind-mounted from the host, so inotify events do not
    // cross into the container and HMR needs polling to see edits at all.
    watch: { usePolling: true },
    proxy: {
      '/api': { target: API_ORIGIN, changeOrigin: true },
      '/health': { target: API_ORIGIN, changeOrigin: true },
      // ws:true upgrades the socket instead of proxying the handshake as a
      // plain GET. Vite's own HMR socket is unaffected — it does not live
      // under /ws, so these prefixes never collide.
      '/ws': { target: API_ORIGIN, ws: true, changeOrigin: true },
    },
  },
})
