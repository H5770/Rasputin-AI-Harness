// ============================================================================
// SERAPH// PROTOCOL  ::  [V2.04_ABHORRENT_IMPERATIVE]
// WARMIND CORE RENDER SUBSYSTEM  ::  RASPUTIN_NODE_0xA7
// CLEARANCE: AURORA_REACH  ::  STATUS: SATELLITE_LINK_ESTABLISHED
// MORPHOLOGY: HOLOGRAPHIC_SHARD_SPHERE (translucent, no solid mass)
// ============================================================================
import * as THREE from "https://unpkg.com/three@0.160.0/build/three.module.js";
import { RoomEnvironment } from "https://unpkg.com/three@0.160.0/examples/jsm/environments/RoomEnvironment.js";

// --- WARMIND_PALETTE :: charcoal base / dark-red alert -----------------------
const WARMIND_PALETTE = {
  VOID_BLACK: 0x0c0d10,
  CHARCOAL: 0x121418,
  ALERT_RED: 0xb31212,
  COMBAT_RED: 0xff1a1a,
  ALERT_ORANGE: 0x7a0000, // dark red
  WARMIND_AMBER: 0x9b0d0d, // dark red
  CIRCUIT_GRAY: 0x3a3d45,
  METAL_COPPER: 0xb87333,
  DATA_GLOW: 0xc41e1e, // dark red glow
  EMBER_HOT: 0xff4a2a, // rare hot sparks
  EMBER_DEEP: 0x4a0000,
};

// --- HEX_LABEL / TERMINAL OUTPUT --------------------------------------------
const SERAPH_PREFIX = "SERAPH//";
const hexStamp = () =>
  "0x" + Math.floor(Math.random() * 0xffff).toString(16).toUpperCase().padStart(4, "0");

const TRANSMIT_SERAPH_LOG = (level, message) => {
  const line = `[${SERAPH_PREFIX}${level}] ${hexStamp()} :: ${message}`;
  const css =
    "color:#c41e1e;background:#0c0d10;font-family:'Share Tech Mono','Courier New',monospace;";
  if (level === "CRITICAL") console.error("%c" + line, css);
  else if (level === "WARN") console.warn("%c" + line, css);
  else console.log("%c" + line, css);
};

// ----------------------------------------------------------------------------
// INITIATE_AURORA_REACH  (formerly initJarvis3D)
// ----------------------------------------------------------------------------
export function INITIATE_AURORA_REACH({
  heroCanvas,
  corridorCanvas, // RESERVED_CHANNEL: unallocated
  vaultCanvas, // RESERVED_CHANNEL: unallocated
  armCanvas, // RESERVED_CHANNEL: unallocated
  galleryCanvases = [], // RESERVED_CHANNEL: unallocated
  onArmImpact, // RESERVED_CHANNEL: unallocated
  onArmStatus, // RESERVED_CHANNEL: unallocated
  onReactorPulse,
} = {}) {
  const SUBMIND_REGISTRY = new Map();

  if (heroCanvas) {
    SUBMIND_REGISTRY.set("hero", FORGE_RASPUTIN_WARMIND_CORE(heroCanvas, { onReactorPulse }));
    TRANSMIT_SERAPH_LOG("LINK", "SATELLITE_LINK_ESTABLISHED // WARMIND_CORE_ONLINE");
  } else {
    TRANSMIT_SERAPH_LOG("WARN", "NO_HERO_CANVAS_DETECTED // CORE_OFFLINE");
  }

  const CHRONO_CLOCK = new THREE.Clock();
  let tickHandle = 0;

  const SUBMIND_TICK = () => {
    const delta = CHRONO_CLOCK.getDelta();
    const elapsed = CHRONO_CLOCK.elapsedTime;
    SUBMIND_REGISTRY.forEach((submind) => {
      submind.update?.({ elapsed, delta });
    });
    tickHandle = window.requestAnimationFrame(SUBMIND_TICK);
  };

  SUBMIND_TICK();

  return {
    pulse() {
      SUBMIND_REGISTRY.get("hero")?.pulse?.();
    },
    setMode(mode) {
      SUBMIND_REGISTRY.forEach((submind) => submind.setMode?.(mode));
    },
    setSceneActive(key, active) {
      SUBMIND_REGISTRY.get(key)?.setActive?.(active);
    },
    // VOICE_LINK hooks: state is "idle" | "listening" | "thinking" | "speaking"
    setVoiceState(state) {
      SUBMIND_REGISTRY.get("hero")?.setVoiceState?.(state);
    },
    speechBeat(strength = 1) {
      SUBMIND_REGISTRY.get("hero")?.speechBeat?.(strength);
    },
    dispose() {
      window.cancelAnimationFrame(tickHandle);
      SUBMIND_REGISTRY.forEach((submind) => submind.dispose?.());
      TRANSMIT_SERAPH_LOG("LINK", "SUBMIND_REGISTRY_PURGED // LINK_TERMINATED");
    },
  };
}

// ----------------------------------------------------------------------------
// GEOMETRY FORGES :: holographic shard construction
// ----------------------------------------------------------------------------
const rand = (a, b) => a + Math.random() * (b - a);

const randOnSphere = () => {
  const u = Math.random() * 2 - 1;
  const t = Math.random() * Math.PI * 2;
  const s = Math.sqrt(1 - u * u);
  return new THREE.Vector3(s * Math.cos(t), u, s * Math.sin(t));
};

const HEAT = {
  deep: new THREE.Color(WARMIND_PALETTE.EMBER_DEEP),
  dark: new THREE.Color(WARMIND_PALETTE.WARMIND_AMBER),
  glow: new THREE.Color(WARMIND_PALETTE.DATA_GLOW),
  hot: new THREE.Color(WARMIND_PALETTE.EMBER_HOT),
};

// Mostly dark red, some brighter red, rare hot sparks
const PICK_HEAT = (out) => {
  const r = Math.random();
  if (r < 0.35) return out.copy(HEAT.deep).lerp(HEAT.dark, Math.random());
  if (r < 0.85) return out.copy(HEAT.dark).lerp(HEAT.glow, Math.random());
  return out.copy(HEAT.glow).lerp(HEAT.hot, Math.random());
};

const ADDITIVE = {
  transparent: true,
  blending: THREE.AdditiveBlending,
  depthWrite: false,
  fog: false,
};

// Fragmented streaks scattered through a spherical shell (the "data shards")
function BUILD_SHARD_SHELL(count, { rMin, rMax, bias, lenMin, lenMax }) {
  const pos = new Float32Array(count * 6);
  const col = new Float32Array(count * 6);
  const c = new THREE.Color();
  for (let i = 0; i < count; i++) {
    const dir = randOnSphere();
    const r = rMin + (rMax - rMin) * Math.pow(Math.random(), bias);
    const a = dir.clone().multiplyScalar(r);
    const tangent = randOnSphere().cross(a).normalize();
    const b = a.clone().addScaledVector(tangent, rand(lenMin, lenMax));
    pos.set([a.x, a.y, a.z, b.x, b.y, b.z], i * 6);
    PICK_HEAT(c);
    col.set([c.r * 0.3, c.g * 0.3, c.b * 0.3, c.r, c.g, c.b], i * 6);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
  const mat = new THREE.LineBasicMaterial({ vertexColors: true, opacity: 0.85, ...ADDITIVE });
  return new THREE.LineSegments(geo, mat);
}

// Ring of circuit ticks and dashes (the data bands wrapping the sphere)
function BUILD_DATA_BAND(radius, { ticks = 240, tickMin = 0.04, tickMax = 0.22 } = {}) {
  const verts = [];
  const cols = [];
  const c = new THREE.Color();
  for (let i = 0; i < ticks; i++) {
    if (Math.random() < 0.22) continue;
    const ang = (i / ticks) * Math.PI * 2 + rand(-0.01, 0.01);
    const z = rand(-0.05, 0.05);
    let a;
    let b;
    if (Math.random() < 0.7) {
      const r1 = radius + rand(tickMin, tickMax) * (Math.random() < 0.5 ? 1 : -1);
      a = [Math.cos(ang) * radius, Math.sin(ang) * radius, z];
      b = [Math.cos(ang) * r1, Math.sin(ang) * r1, z];
    } else {
      const ang1 = ang + rand(0.02, 0.12);
      a = [Math.cos(ang) * radius, Math.sin(ang) * radius, z];
      b = [Math.cos(ang1) * radius, Math.sin(ang1) * radius, z];
    }
    verts.push(...a, ...b);
    PICK_HEAT(c);
    cols.push(c.r, c.g, c.b, c.r * 0.4, c.g * 0.4, c.b * 0.4);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(verts, 3));
  geo.setAttribute("color", new THREE.Float32BufferAttribute(cols, 3));
  const mat = new THREE.LineBasicMaterial({ vertexColors: true, opacity: 0.9, ...ADDITIVE });
  return new THREE.LineSegments(geo, mat);
}

// Sweeping spiral arc (the swirling hook inside the sphere)
function BUILD_SWIRL_ARC(radius, sweep) {
  const N = 160;
  const pos = new Float32Array(N * 3);
  const col = new Float32Array(N * 3);
  const c = new THREE.Color();
  for (let i = 0; i < N; i++) {
    const s = i / (N - 1);
    const th = s * sweep;
    const r = radius * (1 - 0.3 * s);
    pos.set([Math.cos(th) * r, Math.sin(th) * r, Math.sin(s * Math.PI) * radius * 0.35], i * 3);
    c.copy(HEAT.dark).lerp(HEAT.hot, Math.pow(s, 2));
    col.set([c.r * (0.4 + 0.6 * s), c.g * (0.4 + 0.6 * s), c.b * (0.4 + 0.6 * s)], i * 3);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
  const line = new THREE.Line(geo, new THREE.LineBasicMaterial({ vertexColors: true, opacity: 0.9, ...ADDITIVE }));
  const dots = new THREE.Points(
    geo,
    new THREE.PointsMaterial({ vertexColors: true, size: 0.05, opacity: 0.9, ...ADDITIVE })
  );
  const group = new THREE.Group();
  group.add(line, dots);
  return group;
}

// Cloud of glowing motes
function BUILD_EMBER_CLOUD(count, rMin, rMax, size) {
  const pos = new Float32Array(count * 3);
  const col = new Float32Array(count * 3);
  const c = new THREE.Color();
  for (let i = 0; i < count; i++) {
    const p = randOnSphere().multiplyScalar(rand(rMin, rMax));
    pos.set([p.x, p.y, p.z], i * 3);
    PICK_HEAT(c);
    col.set([c.r, c.g, c.b], i * 3);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
  return new THREE.Points(
    geo,
    new THREE.PointsMaterial({ vertexColors: true, size, opacity: 0.8, sizeAttenuation: true, ...ADDITIVE })
  );
}

const FORGE_GLOW_TEXTURE = () => {
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const g = c.getContext("2d");
  const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grad.addColorStop(0, "rgba(255,110,80,1)");
  grad.addColorStop(0.25, "rgba(196,30,30,0.55)");
  grad.addColorStop(1, "rgba(0,0,0,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
};

// ----------------------------------------------------------------------------
// FORGE_RASPUTIN_WARMIND_CORE  (formerly createRasputinCoreScene)
// ----------------------------------------------------------------------------
export function FORGE_RASPUTIN_WARMIND_CORE(canvas, { onReactorPulse } = {}) {
  const runtime = FORGE_SCENE_RUNTIME(canvas, {
    cameraPosition: [0, 0, 9],
    target: [0, 0, 0],
    fogDensity: 0.02,
  });
  const { scene, camera, renderer, resize } = runtime;
  let linkActive = true;
  let pulseAmount = 0;
  let glitchTimer = 0;
  let overrideEngaged = false;

  // VOICE_LINK state (smoothed blends so the core eases in and out)
  let voiceState = "idle";
  let speechKick = 0;
  let speakingBlend = 0;
  let listeningBlend = 0;
  let thinkingBlend = 0;

  const hologram = new THREE.Group();
  scene.add(hologram);

  const spinners = []; // { obj, rate:[x,y,z] }
  const spin = (obj, x, y, z) => {
    spinners.push({ obj, rate: [x, y, z] });
    return obj;
  };

  // 1. GLASS_ENVELOPE :: barely-there translucent shell
  const envelopeMat = new THREE.MeshBasicMaterial({
    color: WARMIND_PALETTE.ALERT_ORANGE,
    opacity: 0.05,
    side: THREE.DoubleSide,
    ...ADDITIVE,
  });
  hologram.add(new THREE.Mesh(new THREE.SphereGeometry(2.45, 48, 32), envelopeMat));

  const latticeMat = new THREE.MeshBasicMaterial({
    color: WARMIND_PALETTE.DATA_GLOW,
    wireframe: true,
    opacity: 0.07,
    ...ADDITIVE,
  });
  hologram.add(spin(new THREE.Mesh(new THREE.IcosahedronGeometry(2.45, 3), latticeMat), 0.01, 0.03, 0));

  // 2. SHARD_SHELLS :: dense fragmented streaks, heaviest at the outer rim
  const shardOuter = BUILD_SHARD_SHELL(2600, { rMin: 1.2, rMax: 2.45, bias: 0.35, lenMin: 0.05, lenMax: 0.32 });
  const shardMid = BUILD_SHARD_SHELL(1400, { rMin: 0.7, rMax: 1.8, bias: 0.7, lenMin: 0.04, lenMax: 0.22 });
  const shardInner = BUILD_SHARD_SHELL(700, { rMin: 0.3, rMax: 1.1, bias: 1, lenMin: 0.03, lenMax: 0.15 });
  hologram.add(spin(shardOuter, 0.02, 0.07, 0.01));
  hologram.add(spin(shardMid, -0.04, -0.1, 0.02));
  hologram.add(spin(shardInner, 0.08, 0.14, -0.05));

  // 3. DATA_BANDS :: tilted circuit rings
  const bandSpecs = [
    { r: 2.35, tilt: [0.2, 0.0, 0.0], rate: [0, 0, 0.12] },
    { r: 2.05, tilt: [1.2, 0.3, 0.0], rate: [0, 0, -0.18] },
    { r: 1.7, tilt: [0.5, 1.1, 0.4], rate: [0, 0, 0.25] },
    { r: 1.35, tilt: [1.5, -0.6, 0.2], rate: [0, 0, -0.3] },
  ];
  bandSpecs.forEach(({ r, tilt, rate }) => {
    const holder = new THREE.Group();
    holder.rotation.set(...tilt);
    const band = BUILD_DATA_BAND(r);
    holder.add(band);
    hologram.add(holder);
    spin(band, ...rate);
  });

  // 4. SWIRL_ARCS :: the curling filaments inside the sphere
  [
    [2.0, Math.PI * 1.6],
    [1.5, Math.PI * 1.9],
    [1.1, Math.PI * 1.4],
    [1.8, Math.PI * 1.2],
    [0.8, Math.PI * 2.0],
  ].forEach(([r, sweep], i) => {
    const arc = BUILD_SWIRL_ARC(r, sweep);
    arc.rotation.set(rand(0, Math.PI), rand(0, Math.PI), rand(0, Math.PI));
    hologram.add(spin(arc, rand(-0.2, 0.2), rand(-0.3, 0.3), 0.15 * (i % 2 ? 1 : -1)));
  });

  // 5. EMBER_LAYERS :: twinkling motes (three layers desync for shimmer)
  const emberLayers = [
    BUILD_EMBER_CLOUD(1100, 0.4, 2.5, 0.035),
    BUILD_EMBER_CLOUD(900, 0.6, 2.4, 0.028),
    BUILD_EMBER_CLOUD(700, 1.2, 2.6, 0.05),
  ];
  emberLayers.forEach((l, i) => hologram.add(spin(l, 0.01 * i, 0.04 + 0.02 * i, 0)));

  // 6. SINGULARITY_GLOW :: soft light core, no solid mass
  const glowMat = new THREE.SpriteMaterial({
    map: FORGE_GLOW_TEXTURE(),
    color: 0xffffff,
    opacity: 0.9,
    ...ADDITIVE,
  });
  const glow = new THREE.Sprite(glowMat);
  glow.scale.setScalar(2.0);
  hologram.add(glow);
  hologram.add(spin(BUILD_EMBER_CLOUD(500, 0.02, 0.55, 0.04), 0.1, 0.2, 0.05));

  // --- BLOOM_UPLINK :: loaded on demand; falls back to plain render ---------
  let composer = null;
  let bloom = null;
  let lastW = 0;
  let lastH = 0;
  const BASE_BLOOM = 1.1;

  Promise.all([
    import("https://unpkg.com/three@0.160.0/examples/jsm/postprocessing/EffectComposer.js"),
    import("https://unpkg.com/three@0.160.0/examples/jsm/postprocessing/RenderPass.js"),
    import("https://unpkg.com/three@0.160.0/examples/jsm/postprocessing/UnrealBloomPass.js"),
    import("https://unpkg.com/three@0.160.0/examples/jsm/postprocessing/OutputPass.js"),
  ])
    .then(([{ EffectComposer }, { RenderPass }, { UnrealBloomPass }, { OutputPass }]) => {
      composer = new EffectComposer(renderer);
      composer.addPass(new RenderPass(scene, camera));
      bloom = new UnrealBloomPass(new THREE.Vector2(256, 256), BASE_BLOOM, 0.6, 0.05);
      composer.addPass(bloom);
      composer.addPass(new OutputPass());
      lastW = 0; // force size sync
      TRANSMIT_SERAPH_LOG("INFO", "BLOOM_UPLINK_ESTABLISHED // HOLOGRAPHIC_GLOW_ENGAGED");
    })
    .catch(() => {
      TRANSMIT_SERAPH_LOG("WARN", "BLOOM_UPLINK_FAILED // FALLBACK_RENDER_ACTIVE");
    });

  const SYNC_COMPOSER = () => {
    if (!composer) return;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (!w || !h || (w === lastW && h === lastH)) return;
    composer.setPixelRatio(renderer.getPixelRatio());
    composer.setSize(w, h);
    lastW = w;
    lastH = h;
  };

  const RENDER_FRAME = () => {
    if (composer) composer.render();
    else renderer.render(scene, camera);
  };

  // --- INPUT HANDLER :: manual reactor surge --------------------------------
  const ON_REACTOR_SURGE = () => {
    pulseAmount = 2.0;
    glitchTimer = 0.25;
    TRANSMIT_SERAPH_LOG("EXEC", "SUBMIND_EXECUTE // REACTOR_SURGE_INITIATED");
    onReactorPulse?.();
  };
  canvas.addEventListener("pointerdown", ON_REACTOR_SURGE);

  return {
    setActive(next) {
      linkActive = next;
      if (linkActive) {
        resize();
        SYNC_COMPOSER();
        RENDER_FRAME();
        TRANSMIT_SERAPH_LOG("LINK", "SATELLITE_LINK_ESTABLISHED // CORE_RESUMED");
      } else {
        TRANSMIT_SERAPH_LOG("WARN", "CORE_STANDBY // RENDER_SUSPENDED");
      }
    },
    pulse() {
      pulseAmount = 2.0;
      glitchTimer = 0.25;
    },
    setVoiceState(next) {
      voiceState = next;
      TRANSMIT_SERAPH_LOG("INFO", `VOICE_LINK_STATE // ${String(next).toUpperCase()}`);
    },
    speechBeat(strength = 1) {
      speechKick = Math.min(1, speechKick + 0.55 * strength);
    },
    setMode(mode) {
      overrideEngaged = mode === "combat";
      if (overrideEngaged) {
        glowMat.color.setHex(0xff6a6a);
        TRANSMIT_SERAPH_LOG("CRITICAL", "CRITICAL_OVERRIDE_ACTIVE // PROTOCOL_YUGA_SUNDOWN");
      } else {
        glowMat.color.setHex(0xffffff);
        TRANSMIT_SERAPH_LOG("INFO", "OVERRIDE_STANDDOWN // BASELINE_RESTORED");
      }
    },
    update({ elapsed, delta }) {
      resize();
      if (!linkActive) return;
      SYNC_COMPOSER();

      pulseAmount = Math.max(0, pulseAmount - delta * 1.5);
      glitchTimer = Math.max(0, glitchTimer - delta);

      // VOICE_LINK blends
      const ease = Math.min(1, delta * 6);
      speakingBlend += ((voiceState === "speaking" ? 1 : 0) - speakingBlend) * ease;
      listeningBlend += ((voiceState === "listening" ? 1 : 0) - listeningBlend) * ease;
      thinkingBlend += ((voiceState === "thinking" ? 1 : 0) - thinkingBlend) * ease;
      speechKick = Math.max(0, speechKick - delta * 3.5);

      // Layered spin (spins faster while "thinking")
      const speed = (overrideEngaged ? 2.2 : 1) * (1 + thinkingBlend * 0.9);
      spinners.forEach(({ obj, rate }) => {
        obj.rotation.x += rate[0] * delta * speed;
        obj.rotation.y += rate[1] * delta * speed;
        obj.rotation.z += rate[2] * delta * speed;
      });

      // Breathing + surge expansion + VOICE swell (swells as it speaks, shrinks back after)
      const speechSwell =
        speakingBlend * (0.05 + 0.04 * (0.5 + 0.5 * Math.sin(elapsed * 8))) + speechKick * 0.07;
      const listenBreath = listeningBlend * (0.012 + 0.012 * Math.sin(elapsed * 2.2));
      hologram.scale.setScalar(
        1 + Math.sin(elapsed * 0.8) * 0.012 + pulseAmount * 0.06 + speechSwell + listenBreath
      );

      // Shimmer
      emberLayers.forEach((layer, i) => {
        layer.material.opacity = 0.55 + 0.35 * Math.sin(elapsed * (2 + i * 1.3) + i * 2.1);
      });
      shardOuter.material.opacity = 0.75 + 0.15 * Math.sin(elapsed * 1.7);
      shardMid.material.opacity = 0.7 + 0.2 * Math.sin(elapsed * 2.3 + 1);
      shardInner.material.opacity = 0.8 + 0.2 * Math.sin(elapsed * 3.1 + 2);

      // Core glow throb
      glow.scale.setScalar(
        1.9 + Math.sin(elapsed * 3) * 0.12 + pulseAmount * 0.7 + speakingBlend * 0.5 + speechKick * 0.5
      );
      glowMat.opacity = 0.8 + Math.sin(elapsed * 4) * 0.1;

      // GLITCH_PROTOCOL :: signal tear (more frequent under override)
      const glitchChance = overrideEngaged ? 0.04 : 0.008;
      if (glitchTimer <= 0 && Math.random() < glitchChance) glitchTimer = 0.08;
      if (glitchTimer > 0) {
        hologram.position.x = (Math.random() - 0.5) * 0.14;
        shardOuter.material.opacity = Math.random();
      } else {
        hologram.position.x = 0;
      }

      if (bloom) {
        bloom.strength =
          (overrideEngaged ? 1.6 : BASE_BLOOM) + pulseAmount * 0.5 + speakingBlend * 0.3 + speechKick * 0.4;
      }

      RENDER_FRAME();
    },
    dispose() {
      canvas.removeEventListener("pointerdown", ON_REACTOR_SURGE);
      hologram.traverse((o) => {
        o.geometry?.dispose?.();
        if (o.material) {
          o.material.map?.dispose?.();
          o.material.dispose();
        }
      });
      composer?.dispose?.();
      runtime.dispose();
    },
  };
}

// ----------------------------------------------------------------------------
// FORGE_SCENE_RUNTIME  (formerly createSceneRuntime)
// ----------------------------------------------------------------------------
function FORGE_SCENE_RUNTIME(
  canvas,
  { cameraPosition = [0, 0, 6], target = [0, 0, 0], fogDensity = 0.05 } = {}
) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(WARMIND_PALETTE.VOID_BLACK);
  scene.fog = new THREE.FogExp2(WARMIND_PALETTE.VOID_BLACK, fogDensity);

  const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 100);
  camera.position.set(...cameraPosition);
  camera.lookAt(...target);

  const renderer = new THREE.WebGLRenderer({
    antialias: true,
    alpha: false,
    canvas,
    powerPreference: "high-performance",
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.08;

  const pmremGenerator = new THREE.PMREMGenerator(renderer);
  scene.environment = pmremGenerator.fromScene(new RoomEnvironment(), 0.05).texture;
  pmremGenerator.dispose();

  const resize = () => {
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!width || !height) return;
    const dpr = renderer.getPixelRatio();
    if (canvas.width !== width * dpr || canvas.height !== height * dpr) {
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    }
  };

  resize();
  return {
    scene,
    camera,
    renderer,
    resize,
    dispose() {
      renderer.dispose();
    },
  };
}

// ----------------------------------------------------------------------------
// LEGACY_UPLINK_ALIASES :: keeps existing callers functional
// ----------------------------------------------------------------------------
export const initJarvis3D = INITIATE_AURORA_REACH;
export const createRasputinCoreScene = FORGE_RASPUTIN_WARMIND_CORE;
