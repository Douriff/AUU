/**
 * Cyber-brain holographic splash: neural mesh + data digits → mouse spill → AUUTRADE morph.
 * Caller must dispose() on unmount (cancels rAF, frees GPU).
 */
import * as THREE from "three";

export type CyberBrainOptions = {
  container: HTMLElement;
  particleCount?: number;
  mobile?: boolean;
  text?: string;
};

type Phase = "brain" | "spill" | "morph" | "hold";

const AMBER = new THREE.Color(0xf0a531);
const AMBER_HI = new THREE.Color(0xffd27a);
const CYAN = new THREE.Color(0x58a6ff);
const CYAN_LO = new THREE.Color(0x1a4a7a);

function hash01(n: number): number {
  const x = Math.sin(n * 127.1 + 311.7) * 43758.5453;
  return x - Math.floor(x);
}

/** Rough organic brain volume: dual lobes + stem + noise. */
function sampleBrainPoint(i: number, out: THREE.Vector3): void {
  const u = hash01(i * 1.7);
  const v = hash01(i * 3.1 + 0.4);
  const w = hash01(i * 5.9 + 1.2);
  const lobe = u < 0.48 ? -1 : u < 0.96 ? 1 : 0; // stem when 0
  if (lobe === 0) {
    // brainstem
    out.set((w - 0.5) * 0.55, -1.15 + v * 0.9, (u - 0.5) * 0.4);
  } else {
    const theta = v * Math.PI * 2;
    const phi = Math.acos(2 * w - 1);
    let r = 0.55 + 0.35 * hash01(i * 9.2);
    // flatten slightly, elongate front-back
    const x = Math.sin(phi) * Math.cos(theta) * r * 0.95 + lobe * 0.72;
    const y = Math.cos(phi) * r * 0.78 + 0.15;
    const z = Math.sin(phi) * Math.sin(theta) * r * 1.15;
    // sulcus-ish ridges
    const ridge = 0.08 * Math.sin(x * 7 + z * 5) * Math.cos(y * 6);
    out.set(x + ridge * lobe, y + ridge * 0.5, z);
  }
}

function sampleTextTargets(
  text: string,
  count: number,
  width: number,
  height: number,
): Float32Array {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d")!;
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = "#fff";
  ctx.font = `bold ${Math.floor(height * 0.55)}px Inter, system-ui, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, width / 2, height / 2 + height * 0.04);
  const { data } = ctx.getImageData(0, 0, width, height);
  const pts: number[] = [];
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      if (data[(y * width + x) * 4] > 140) pts.push(x, y);
    }
  }
  const out = new Float32Array(count * 3);
  const aspect = width / height;
  const scaleX = 5.8;
  const scaleY = scaleX / aspect;
  for (let i = 0; i < count; i++) {
    const pick = pts.length ? ((i * 7919) % (pts.length / 2)) | 0 : 0;
    const px = pts.length ? pts[pick * 2] : width / 2;
    const py = pts.length ? pts[pick * 2 + 1] : height / 2;
    const jx = (hash01(i + 3) - 0.5) * 0.03;
    const jy = (hash01(i + 7) - 0.5) * 0.03;
    out[i * 3] = ((px / width) - 0.5) * scaleX + jx;
    out[i * 3 + 1] = (0.5 - py / height) * scaleY + jy;
    out[i * 3 + 2] = (hash01(i + 11) - 0.5) * 0.12;
  }
  return out;
}

const DIGITS = "0123456789ABCDEF#$%*+<>/";

function makeDigitTexture(): THREE.CanvasTexture {
  const size = 64;
  const canvas = document.createElement("canvas");
  canvas.width = size * 8;
  canvas.height = size * 4;
  const ctx = canvas.getContext("2d")!;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.font = `bold ${size * 0.72}px "JetBrains Mono", ui-monospace, monospace`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (let i = 0; i < 32; i++) {
    const ch = DIGITS[i % DIGITS.length];
    const col = i % 8;
    const row = (i / 8) | 0;
    ctx.fillStyle = i % 3 === 0 ? "#f0a531" : i % 3 === 1 ? "#58a6ff" : "#e8f0ff";
    ctx.globalAlpha = 0.85;
    ctx.fillText(ch, col * size + size / 2, row * size + size / 2);
  }
  const tex = new THREE.CanvasTexture(canvas);
  tex.needsUpdate = true;
  tex.minFilter = THREE.LinearFilter;
  tex.magFilter = THREE.LinearFilter;
  return tex;
}

export type CyberBrainHandle = {
  dispose: () => void;
};

export function createCyberBrainSplash(opts: CyberBrainOptions): CyberBrainHandle {
  const mobile = !!opts.mobile;
  const count = opts.particleCount ?? (mobile ? 4200 : 11000);
  const text = opts.text ?? "AUUTRADE";
  const container = opts.container;

  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x05070a, 0.045);

  const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 80);
  camera.position.set(0, 0.15, mobile ? 7.2 : 6.4);

  const renderer = new THREE.WebGLRenderer({
    antialias: !mobile,
    alpha: false,
    powerPreference: "high-performance",
  });
  renderer.setClearColor(0x05070a, 1);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, mobile ? 1.5 : 1.75));
  container.appendChild(renderer.domElement);
  const canvas = renderer.domElement;
  canvas.className = "auu-splash-canvas";
  canvas.style.width = "100%";
  canvas.style.height = "100%";
  canvas.style.display = "block";

  // --- particle buffers ---
  const brainPos = new Float32Array(count * 3);
  const textPos = sampleTextTargets(text, count, 512, 128);
  const curPos = new Float32Array(count * 3);
  const vel = new Float32Array(count * 3);
  const colors = new Float32Array(count * 3);
  const sizes = new Float32Array(count);
  const seeds = new Float32Array(count);
  const tmp = new THREE.Vector3();

  for (let i = 0; i < count; i++) {
    sampleBrainPoint(i + 1, tmp);
    brainPos[i * 3] = tmp.x;
    brainPos[i * 3 + 1] = tmp.y;
    brainPos[i * 3 + 2] = tmp.z;
    curPos[i * 3] = tmp.x;
    curPos[i * 3 + 1] = tmp.y;
    curPos[i * 3 + 2] = tmp.z;
    seeds[i] = hash01(i * 17.3);
    sizes[i] = mobile ? 0.09 + seeds[i] * 0.08 : 0.07 + seeds[i] * 0.1;
    const c = seeds[i] < 0.55 ? AMBER.clone().lerp(AMBER_HI, seeds[i]) : CYAN.clone().lerp(CYAN_LO, seeds[i]);
    colors[i * 3] = c.r;
    colors[i * 3 + 1] = c.g;
    colors[i * 3 + 2] = c.b;
  }

  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(curPos, 3));
  geo.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  geo.setAttribute("aSize", new THREE.BufferAttribute(sizes, 1));

  const mat = new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    uniforms: {
      uTime: { value: 0 },
      uPixelRatio: { value: renderer.getPixelRatio() },
    },
    vertexShader: /* glsl */ `
      attribute float aSize;
      attribute vec3 color;
      varying vec3 vColor;
      uniform float uTime;
      uniform float uPixelRatio;
      void main() {
        vColor = color;
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        float pulse = 1.0 + 0.15 * sin(uTime * 3.0 + position.x * 4.0 + position.y * 3.0);
        gl_PointSize = max(2.5, aSize * uPixelRatio * (520.0 / max(0.15, -mv.z)) * pulse);
        gl_Position = projectionMatrix * mv;
      }
    `,
    fragmentShader: /* glsl */ `
      varying vec3 vColor;
      void main() {
        vec2 uv = gl_PointCoord - 0.5;
        float d = length(uv);
        float alpha = smoothstep(0.5, 0.05, d);
        gl_FragColor = vec4(vColor, alpha * 0.92);
      }
    `,
  });
  // enable vertex colors

  const points = new THREE.Points(geo, mat);
  scene.add(points);

  // neural mesh lines (subset)
  const linkCount = mobile ? 360 : 1100;
  const linkPos = new Float32Array(linkCount * 2 * 3);
  for (let i = 0; i < linkCount; i++) {
    const a = (i * 13) % count;
    let best = (a + 1) % count;
    let bestD = 1e9;
    for (let k = 1; k < 12; k++) {
      const b = (a + k * 97) % count;
      const dx = brainPos[a * 3] - brainPos[b * 3];
      const dy = brainPos[a * 3 + 1] - brainPos[b * 3 + 1];
      const dz = brainPos[a * 3 + 2] - brainPos[b * 3 + 2];
      const d = dx * dx + dy * dy + dz * dz;
      if (d < bestD && d > 0.02) {
        bestD = d;
        best = b;
      }
    }
    for (let c = 0; c < 3; c++) {
      linkPos[i * 6 + c] = brainPos[a * 3 + c];
      linkPos[i * 6 + 3 + c] = brainPos[best * 3 + c];
    }
  }
  const linkGeo = new THREE.BufferGeometry();
  linkGeo.setAttribute("position", new THREE.BufferAttribute(linkPos, 3));
  const linkMat = new THREE.LineBasicMaterial({
    color: 0x58a6ff,
    transparent: true,
    opacity: 0.35,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  });
  const links = new THREE.LineSegments(linkGeo, linkMat);
  scene.add(links);

  // holographic brain hull (dual-lobe wireframe)
  const hullGeo = new THREE.IcosahedronGeometry(1.35, mobile ? 2 : 3);
  const hullPos = hullGeo.attributes.position as THREE.BufferAttribute;
  for (let i = 0; i < hullPos.count; i++) {
    const x = hullPos.getX(i);
    const y = hullPos.getY(i);
    const z = hullPos.getZ(i);
    const lobe = x >= 0 ? 1 : -1;
    const nx = x * 0.85 + lobe * 0.55;
    const ny = y * 0.72 + 0.12;
    const nz = z * 1.05;
    const ridge = 0.06 * Math.sin(nx * 6 + nz * 5);
    hullPos.setXYZ(i, nx + ridge * lobe, ny + ridge * 0.4, nz);
  }
  hullPos.needsUpdate = true;
  hullGeo.computeVertexNormals();
  const hullMat = new THREE.MeshBasicMaterial({
    color: 0x58a6ff,
    wireframe: true,
    transparent: true,
    opacity: 0.22,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
  const brainHull = new THREE.Mesh(hullGeo, hullMat);
  scene.add(brainHull);
  const hullMat2 = hullMat.clone();
  hullMat2.color = new THREE.Color(0xf0a531);
  hullMat2.opacity = 0.1;
  const hullGeo2 = hullGeo.clone();
  const brainHull2 = new THREE.Mesh(hullGeo2, hullMat2);
  brainHull2.scale.setScalar(1.04);
  scene.add(brainHull2);

  // floating digit sprites (data streams)
  const digitCount = mobile ? 260 : 560;
  const digitTex = makeDigitTexture();
  const digitGeo = new THREE.BufferGeometry();
  const dPos = new Float32Array(digitCount * 3);
  const dVel = new Float32Array(digitCount * 3);
  const dCol = new Float32Array(digitCount * 3);
  const dOff = new Float32Array(digitCount);
  for (let i = 0; i < digitCount; i++) {
    sampleBrainPoint(i * 3 + 99, tmp);
    // start near surface, drift outward slightly
    tmp.multiplyScalar(1.05 + hash01(i) * 0.35);
    dPos[i * 3] = tmp.x;
    dPos[i * 3 + 1] = tmp.y;
    dPos[i * 3 + 2] = tmp.z;
    dVel[i * 3] = (hash01(i + 1) - 0.5) * 0.35;
    dVel[i * 3 + 1] = 0.25 + hash01(i + 2) * 0.55;
    dVel[i * 3 + 2] = (hash01(i + 3) - 0.5) * 0.35;
    const c = hash01(i + 4) < 0.5 ? AMBER : CYAN;
    dCol[i * 3] = c.r;
    dCol[i * 3 + 1] = c.g;
    dCol[i * 3 + 2] = c.b;
    dOff[i] = hash01(i + 5) * 32;
  }
  digitGeo.setAttribute("position", new THREE.BufferAttribute(dPos, 3));
  digitGeo.setAttribute("color", new THREE.BufferAttribute(dCol, 3));
  digitGeo.setAttribute("aOff", new THREE.BufferAttribute(dOff, 1));
  const digitPointSize = mobile ? "9.0 + fract(aOff) * 6.0" : "12.0 + fract(aOff) * 8.0";
  const digitMat = new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    uniforms: {
      uMap: { value: digitTex },
      uTime: { value: 0 },
      uPixelRatio: { value: renderer.getPixelRatio() },
      uSpill: { value: 0 },
    },
    vertexShader: `
      attribute float aOff;
      attribute vec3 color;
      varying vec3 vColor;
      varying float vOff;
      uniform float uTime;
      uniform float uPixelRatio;
      uniform float uSpill;
      void main() {
        vColor = color;
        vOff = aOff;
        vec3 p = position;
        p.y += sin(uTime * 2.0 + aOff) * 0.08;
        float len = length(p);
        vec3 dir = len > 0.001 ? p / len : vec3(0.0, 1.0, 0.0);
        p += dir * uSpill * (0.8 + fract(aOff) * 1.8);
        vec4 mv = modelViewMatrix * vec4(p, 1.0);
        float psz = __DIGIT_PSZ__;
        gl_PointSize = psz * uPixelRatio;
        gl_Position = projectionMatrix * mv;
      }
    `.replace("__DIGIT_PSZ__", digitPointSize),
    fragmentShader: `
      uniform sampler2D uMap;
      varying vec3 vColor;
      varying float vOff;
      void main() {
        float idx = floor(mod(vOff, 32.0));
        float col = mod(idx, 8.0);
        float row = floor(idx / 8.0);
        vec2 uv = vec2((col + gl_PointCoord.x) / 8.0, (row + gl_PointCoord.y) / 4.0);
        vec4 tex = texture2D(uMap, uv);
        if (tex.a < 0.08) discard;
        gl_FragColor = vec4(vColor * 1.15, tex.a * 0.85);
      }
    `,
  });

  const digits = new THREE.Points(digitGeo, digitMat);
  scene.add(digits);

  // soft glow orb behind brain
  const glowGeo = new THREE.SphereGeometry(2.4, 24, 24);
  const glowMat = new THREE.MeshBasicMaterial({
    color: 0xf0a531,
    transparent: true,
    opacity: 0.028,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
  const glow = new THREE.Mesh(glowGeo, glowMat);
  scene.add(glow);

  // mouse
  const mouse = new THREE.Vector2(0, 0);
  const mouseSmooth = new THREE.Vector2(0, 0);
  const onPointer = (e: PointerEvent) => {
    const rect = canvas.getBoundingClientRect();
    mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    mouse.y = -(((e.clientY - rect.top) / rect.height) * 2 - 1);
  };
  window.addEventListener("pointermove", onPointer, { passive: true });

  let phase: Phase = "brain";
  const t0 = performance.now();
  const T_BRAIN = 2200;
  const T_SPILL = 1400;
  const T_MORPH = 2200;
  // hold after morph handled by React

  let raf = 0;
  let alive = true;
  const clock = new THREE.Clock();

  const resize = () => {
    const w = container.clientWidth || window.innerWidth;
    const h = container.clientHeight || Math.max(320, window.innerHeight * 0.62);
    camera.aspect = w / Math.max(1, h);
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, mobile ? 1.5 : 1.75));
    mat.uniforms.uPixelRatio.value = renderer.getPixelRatio();
    digitMat.uniforms.uPixelRatio.value = renderer.getPixelRatio();
  };
  resize();
  const ro = new ResizeObserver(resize);
  ro.observe(container);

  const posAttr = geo.getAttribute("position") as THREE.BufferAttribute;
  const linkAttr = linkGeo.getAttribute("position") as THREE.BufferAttribute;

  function easeInOut(t: number): number {
    return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
  }

  function tick() {
    if (!alive) return;
    raf = requestAnimationFrame(tick);
    const elapsed = performance.now() - t0;
    const dt = Math.min(0.033, clock.getDelta());
    const time = clock.elapsedTime;
    mat.uniforms.uTime.value = time;
    digitMat.uniforms.uTime.value = time;

    mouseSmooth.lerp(mouse, 0.08);
    const mx = mouseSmooth.x * 3.2;
    const my = mouseSmooth.y * 2.2;

    if (elapsed < T_BRAIN) phase = "brain";
    else if (elapsed < T_BRAIN + T_SPILL) phase = "spill";
    else if (elapsed < T_BRAIN + T_SPILL + T_MORPH) phase = "morph";
    else phase = "hold";

    const spillAmt =
      phase === "spill"
        ? easeInOut((elapsed - T_BRAIN) / T_SPILL)
        : phase === "brain"
          ? 0
          : 1;
    digitMat.uniforms.uSpill.value = phase === "morph" || phase === "hold" ? Math.max(0, 1 - (elapsed - T_BRAIN - T_SPILL) / T_MORPH) * 0.3 : spillAmt * 2.2;

    const morphT =
      phase === "morph"
        ? easeInOut((elapsed - T_BRAIN - T_SPILL) / T_MORPH)
        : phase === "hold"
          ? 1
          : 0;

    // mouse scatter strength (cinematic overflow)
    const scatterBase = phase === "brain" ? 0.55 : phase === "spill" ? 1.4 : 0.15;

    for (let i = 0; i < count; i++) {
      const i3 = i * 3;
      let tx: number;
      let ty: number;
      let tz: number;
      if (morphT > 0) {
        const bx = brainPos[i3];
        const by = brainPos[i3 + 1];
        const bz = brainPos[i3 + 2];
        // after spill, expand then pull to text
        const expand = 1 + spillAmt * (1.2 + seeds[i] * 1.8);
        const sx = bx * expand + vel[i3];
        const sy = by * expand + vel[i3 + 1];
        const sz = bz * expand + vel[i3 + 2];
        tx = sx + (textPos[i3] - sx) * morphT;
        ty = sy + (textPos[i3 + 1] - sy) * morphT;
        tz = sz + (textPos[i3 + 2] - sz) * morphT;
      } else {
        // brain + spill
        const bx = brainPos[i3];
        const by = brainPos[i3 + 1];
        const bz = brainPos[i3 + 2];
        // organic idle motion
        const wobble = 0.04 * Math.sin(time * 2.2 + seeds[i] * 12);
        let px = bx * (1 + spillAmt * (0.9 + seeds[i] * 2.2));
        let py = by * (1 + spillAmt * (0.9 + seeds[i] * 2.0)) + wobble;
        let pz = bz * (1 + spillAmt * (0.9 + seeds[i] * 2.2));
        // radial burst during spill
        if (spillAmt > 0) {
          const len = Math.hypot(bx, by, bz) || 1;
          px += (bx / len) * spillAmt * (1.5 + seeds[i] * 3.5);
          py += (by / len) * spillAmt * (1.2 + hash01(i + 2) * 2.5) + spillAmt * (hash01(i) - 0.3);
          pz += (bz / len) * spillAmt * (1.5 + hash01(i + 3) * 3.0);
          vel[i3] = px - bx;
          vel[i3 + 1] = py - by;
          vel[i3 + 2] = pz - bz;
        }
        // mouse repulsion / overflow
        const dx = px - mx;
        const dy = py - my;
        const dist = Math.hypot(dx, dy) + 0.15;
        const force = scatterBase * (0.85 / (dist * dist)) * (0.5 + seeds[i]);
        px += (dx / dist) * force * (phase === "spill" ? 1.8 : 1);
        py += (dy / dist) * force * (phase === "spill" ? 1.8 : 1);
        // slight z kick from mouse
        pz += force * 0.35 * Math.sin(seeds[i] * 6 + time);
        tx = px;
        ty = py;
        tz = pz;
      }
      // smooth toward target
      const k = phase === "morph" ? 0.18 : 0.22;
      curPos[i3] += (tx - curPos[i3]) * k;
      curPos[i3 + 1] += (ty - curPos[i3 + 1]) * k;
      curPos[i3 + 2] += (tz - curPos[i3 + 2]) * k;
    }
    posAttr.needsUpdate = true;

    // update links to follow first endpoint particles approximate (brain phase only)
    const linkFade =
      phase === "brain" ? 0.4 : phase === "spill" ? 0.18 * (1 - spillAmt) : 0;
    linkMat.opacity = linkFade;
    links.visible = linkFade > 0.02;
    if (links.visible) {
      // lightly pull link endpoints toward current particle positions for a few samples
      for (let i = 0; i < Math.min(linkCount, 200); i++) {
        const a = (i * 13) % count;
        const b = (a + 97) % count;
        linkPos[i * 6] = curPos[a * 3];
        linkPos[i * 6 + 1] = curPos[a * 3 + 1];
        linkPos[i * 6 + 2] = curPos[a * 3 + 2];
        linkPos[i * 6 + 3] = curPos[b * 3];
        linkPos[i * 6 + 4] = curPos[b * 3 + 1];
        linkPos[i * 6 + 5] = curPos[b * 3 + 2];
      }
      linkAttr.needsUpdate = true;
    }

    // digit stream drift
    for (let i = 0; i < digitCount; i++) {
      const i3 = i * 3;
      if (phase === "morph" || phase === "hold") {
        // pull digits toward text area then fade via spill uniform
        dPos[i3] += (textPos[(i % count) * 3] - dPos[i3]) * 0.06;
        dPos[i3 + 1] += (textPos[(i % count) * 3 + 1] - dPos[i3 + 1]) * 0.06;
        dPos[i3 + 2] += (0 - dPos[i3 + 2]) * 0.06;
      } else {
        dPos[i3] += dVel[i3] * dt;
        dPos[i3 + 1] += dVel[i3 + 1] * dt;
        dPos[i3 + 2] += dVel[i3 + 2] * dt;
        // mouse scatter digits harder
        const dx = dPos[i3] - mx;
        const dy = dPos[i3 + 1] - my;
        const dist = Math.hypot(dx, dy) + 0.2;
        const f = scatterBase * 1.4 / (dist * dist);
        dPos[i3] += (dx / dist) * f * dt * 8;
        dPos[i3 + 1] += (dy / dist) * f * dt * 8;
        // recycle if too far
        if (Math.hypot(dPos[i3], dPos[i3 + 1], dPos[i3 + 2]) > 8) {
          sampleBrainPoint(i * 5 + ((time * 10) | 0), tmp);
          tmp.multiplyScalar(1.1);
          dPos[i3] = tmp.x;
          dPos[i3 + 1] = tmp.y;
          dPos[i3 + 2] = tmp.z;
        }
      }
    }
    (digitGeo.getAttribute("position") as THREE.BufferAttribute).needsUpdate = true;

    // camera / glow
    camera.position.x = mouseSmooth.x * 0.35;
    camera.position.y = 0.15 + mouseSmooth.y * 0.2;
    camera.lookAt(0, 0, 0);
    glow.scale.setScalar(1.15 + Math.sin(time * 1.5) * 0.06 + spillAmt * 0.5);
    glowMat.opacity = 0.025 + spillAmt * 0.03;
    glow.visible = phase === "brain" || phase === "spill";
    const hullOpacity =
      phase === "brain" ? 0.22 + 0.06 * Math.sin(time * 2.5) : phase === "spill" ? 0.12 * (1 - spillAmt) : 0;
    hullMat.opacity = hullOpacity;
    hullMat2.opacity = hullOpacity * 0.45;
    brainHull.visible = hullOpacity > 0.02;
    brainHull2.visible = hullOpacity > 0.02;
    brainHull.rotation.y = time * 0.25;
    brainHull2.rotation.y = time * 0.25;
    // mouse tilt
    brainHull.rotation.x = mouseSmooth.y * 0.25;
    brainHull2.rotation.x = mouseSmooth.y * 0.25;

    digits.visible = phase !== "hold";

    renderer.render(scene, camera);
  }

  raf = requestAnimationFrame(tick);

  return {
    dispose() {
      alive = false;
      cancelAnimationFrame(raf);
      window.removeEventListener("pointermove", onPointer);
      ro.disconnect();
      geo.dispose();
      mat.dispose();
      linkGeo.dispose();
      linkMat.dispose();
      hullGeo.dispose();
      hullGeo2.dispose();
      hullMat.dispose();
      hullMat2.dispose();
      digitGeo.dispose();
      digitMat.dispose();
      digitTex.dispose();
      glowGeo.dispose();
      glowMat.dispose();
      renderer.dispose();
      if (canvas.parentNode) canvas.parentNode.removeChild(canvas);
    },
  };
}
