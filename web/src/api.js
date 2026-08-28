/**
 * Every backend call is same-origin: the dev server (and any reverse proxy in
 * front of a production build) forwards /api and /ws to the API. Set
 * VITE_API_BASE only to point a build at a backend on a different host.
 */
export const API = import.meta.env.VITE_API_BASE || ''

export const WS = API
  ? API.replace(/^http/, 'ws')
  : `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}`
