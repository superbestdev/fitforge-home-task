import { resolve } from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Both UIs are served by this one dev server as separate documents:
//   /          -> index.html          -> src/chat
//   /console/  -> console/index.html  -> src/console
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

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      input: {
        chat: resolve(__dirname, 'index.html'),
        console: resolve(__dirname, 'console/index.html'),
      },
    },
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
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
