# Splash intro — cyber-brain (every visit)

Custom Three.js holographic **cyber-brain** intro: neural mesh + floating hex/digit data streams, mouse-reactive spill, then particles coalesce into **AUUTRADE**.

## Behaviour

- Plays on **every full page load** (no storage gate).
- Timeline: brain (~2.2s) → data spill (~1.4s) → morph to text (~2.2s) → hold, then auto-enter.
- Mouse move scatters / overflows data while the brain is live.
- Skip / Enter / Esc / failsafe → unmount canvas and dispose WebGL (rAF cancel + geometry/material/renderer dispose).
- `prefers-reduced-motion: reduce` → static branded panel, no WebGL.
- Mobile: lower particle/digit counts, dpr ≤ 1.5.
- Frontend-only; does not gate login or API.

## Verify

Reload https://auutrade.com — splash plays again with brain → spill → AUUTRADE.

## Chunk

`SplashCanvas` is `React.lazy`; engine lives in `cyberBrainEngine.ts` (three only).
