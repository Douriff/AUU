# Splash intro (every visit)

Fullscreen particle morph (MIT `3d-shape-particle-text` + `three`) into **AUUTRADE**, with a short Chinese/English intro and always-visible enter/skip.

## Behaviour

- Shows on **every full page load** (no `localStorage` / `sessionStorage` "seen" gate).
- **Verify:** reload https://auutrade.com — splash plays again.
- Skip / Enter / Esc / Enter key / auto after morph+hold (~6s) / 12s failsafe → unmount canvas (package cancels rAF and disposes Three.js resources). Dismiss lasts for this page load only.
- `prefers-reduced-motion: reduce` → static branded panel, no WebGL.
- Mobile: lower `particleCount`, dpr cap, no globe wireframe.
- Frontend-only; does not gate login or API.

## Chunk

`SplashCanvas` is `React.lazy` so Three.js loads with the splash overlay each visit, then is disposed when the overlay closes.
