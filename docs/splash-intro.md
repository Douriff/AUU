# First-visit splash intro

Fullscreen particle morph (MIT `3d-shape-particle-text` + `three`) into **AUUTRADE**, with a short Chinese/English intro and always-visible enter/skip.

## Behaviour

- Shows once per browser profile when `localStorage["auu.splash.seen"]` is unset.
- **Re-see:** DevTools → Application → Local Storage → delete `auu.splash.seen`, reload.
- Skip / Enter / Esc / Enter key / auto after morph+hold (~5s) / 12s failsafe → unmount canvas (package cancels rAF and disposes Three.js resources).
- `prefers-reduced-motion: reduce` → static branded panel, no WebGL.
- Mobile: lower `particleCount`, no globe wireframe.
- Frontend-only; does not gate login or API.

## Chunk

`SplashCanvas` is `React.lazy` so Three.js loads only on first visit (when splash will show).
