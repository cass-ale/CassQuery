import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";

const stage = document.getElementById("stage");
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.0;
stage.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(getComputedStyle(document.documentElement)
  .getPropertyValue("--panel").trim() || "#1c1c1f");
const pmrem = new THREE.PMREMGenerator(renderer);
scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;

const camera = new THREE.PerspectiveCamera(40, 1, 0.5, 4000);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

const key = new THREE.DirectionalLight(0xffffff, 1.25);
key.position.set(60, -40, 90);
scene.add(key, new THREE.AmbientLight(0xffffff, 0.15));
scene.environmentIntensity = 0.85;

const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const httpPort = Number(location.port || 3939);
const api = (p) => fetch(p).then((r) => r.json());

// ---------- state ----------
let projects = [];           // [{name, root, state, hashes, ...}]
let current = null;          // project name
let manifest = null;
let model = null;            // {group, parts: Map name->[{mesh, base}], bounds, hash, tier}
let currentStep = -1;
let explodeAmt = 0, explodeTarget = 0;
let xrayOn = false;
let camAnim = null;
const shownHashes = {};      // project -> {preview, certified}

// ---------- small helpers ----------
function b64f32(s) {
  const bin = atob(s), u8 = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  return new Float32Array(u8.buffer);
}
function b64indices(s) {
  const bin = atob(s.indices), u8 = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  return s.itype === "u16" ? new Uint16Array(u8.buffer) : new Uint32Array(u8.buffer);
}
function b64positions(s) {
  // positions arrive as int16 quantized (qoffset + q * qscale)
  const bin = atob(s.positions), u8 = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  const q = new Int16Array(u8.buffer);
  const out = new Float32Array(q.length);
  for (let i = 0; i < q.length; i++) out[i] = s.qoffset[i % 3] + q[i] * s.qscale;
  return out;
}
function frameCamera(center, radius, dir = new THREE.Vector3(0.95, -1.35, 0.62)) {
  // distance that fits the bounding sphere inside the vertical fov, +2% margin
  const fit = radius / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * 1.02;
  const d = dir.clone().normalize().multiplyScalar(fit);
  camera.position.copy(center).add(d);
  controls.target.copy(center);
  camera.near = Math.max(0.1, radius / 50);
  camera.far = radius * 40;
  camera.updateProjectionMatrix();
  controls.update();
}
function tweenCamera(pos, target, dist) {
  if (reduced) {
    if (pos) camera.position.copy(pos);
    if (target) controls.target.copy(target);
    if (dist !== undefined) {
      const dir = camera.position.clone().sub(controls.target).normalize();
      camera.position.copy(controls.target).add(dir.multiplyScalar(dist));
    }
    controls.update();
    return;
  }
  camAnim = { p0: camera.position.clone(), t0: controls.target.clone(),
              p1: pos ? pos.clone() : null, t1: target ? target.clone() : null,
              dist, start: performance.now(), dur: 550 };
}

// ---------- model ----------
function disposeModel() {
  if (!model) return;
  scene.remove(model.group);
  model.group.traverse((o) => {
    if (o.geometry) o.geometry.dispose();
    if (o.material) o.material.dispose();
  });
  model = null;
}

async function buildModel(index) {
  disposeModel();
  const group = new THREE.Group();
  scene.add(group);
  model = { group, variants: new Map(), index, hash: index.hash, tier: index.tier,
            gates: index.gates, bounds: null, activeName: null };
  await loadVariant("neutral");
  applyExplode(0, true);
  applyXray(false);
}

// Variants load lazily: the index names each variant file + hash; a variant
// is fetched the first time a step (or command) asks for it.
async function loadVariant(name) {
  if (!model || model.variants.has(name)) return model.variants.get(name);
  const meta = model.index.variants[name];
  if (!meta) return undefined;
  const m0 = model;
  const v = await api(meta.url);
  if (model !== m0) return undefined; // model replaced while fetching
  const vgroup = new THREE.Group();
  vgroup.visible = false;
  const vparts = new Map();
  for (const part of v.parts) {
    const mat = new THREE.MeshStandardMaterial({
      color: new THREE.Color(part.finish.color),
      metalness: part.finish.metalness, roughness: part.finish.roughness });
    const meshes = [];
    for (const s of part.solids) {
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.BufferAttribute(b64positions(s), 3));
      g.setIndex(new THREE.BufferAttribute(b64indices(s), 1));
      g.computeVertexNormals();
      const mesh = new THREE.Mesh(g, mat);
      mesh.userData.part = part.name;
      vgroup.add(mesh);
      meshes.push(mesh);
    }
    vparts.set(part.name, { meshes, mat, xray: part.xray,
                            explode: new THREE.Vector3(...part.explode) });
  }
  model.group.add(vgroup);
  const entry = { group: vgroup, parts: vparts, bounds: v.bounds };
  model.variants.set(name, entry);
  if (name === "neutral") model.bounds = v.bounds;
  return entry;
}

function activeVariant() {
  if (model && model.activeName && model.variants.has(model.activeName))
    return [model.activeName, model.variants.get(model.activeName)];
  return ["neutral", model && model.variants.get("neutral")];
}
async function setVariant(name) {
  if (!model) return;
  const entry = await loadVariant(name);
  if (!entry || !model) return;
  model.activeName = name;
  for (const [n, v] of model.variants) v.group.visible = n === name;
  applyExplode(explodeAmt, true);
  applyXray(xrayOn);
}
function applyExplode(amt, hard = false) {
  explodeAmt = amt;
  if (!model) return;
  const [, v] = activeVariant();
  if (!v) return;
  for (const [, p] of v.parts)
    for (const m of p.meshes) m.position.copy(p.explode).multiplyScalar(amt);
  document.getElementById("explode").value = Math.round(amt * 100);
  document.getElementById("explodeval").textContent = Math.round(amt * 100) + "%";
}
function applyXray(on) {
  xrayOn = on;
  document.getElementById("xraybtn").setAttribute("aria-pressed", String(on));
  if (!model) return;
  const [, v] = activeVariant();
  if (!v) return;
  for (const [, p] of v.parts) {
    const ghost = on && p.xray === "ghost";
    p.mat.transparent = ghost;
    p.mat.opacity = ghost ? 0.2 : 1.0;
    p.mat.depthWrite = !ghost;
    p.mat.needsUpdate = true;
  }
}

// ---------- steps ----------
function renderSteps() {
  const nav = document.getElementById("steps");
  nav.innerHTML = "";
  manifest.steps.forEach((s, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = s.label;
    b.setAttribute("aria-pressed", String(i === currentStep));
    b.addEventListener("click", () => goStep(i, true));
    nav.appendChild(b);
  });
  document.getElementById("chips").innerHTML =
    manifest.chips.map((c) => `<li>${c}</li>`).join("");
}
function goStep(i, animate) {
  if (!manifest || i < 0 || i >= manifest.steps.length) return;
  currentStep = i;
  const s = manifest.steps[i];
  document.getElementById("caption").textContent = s.caption;
  document.querySelectorAll("#steps button").forEach((b, j) =>
    b.setAttribute("aria-pressed", String(j === i)));
  document.getElementById("prev").disabled = i === 0;
  document.getElementById("next").disabled = i === manifest.steps.length - 1;
  if (!model) return;
  const before = activeVariant()[0];
  const stepAtCall = i;
  setVariant(s.variant || "neutral").then(() => {
    if (currentStep !== stepAtCall || !model) return;
    applyXray(!!s.xray);
    if (s.camera) {
      tweenCamera(new THREE.Vector3(...s.camera.position),
                  new THREE.Vector3(...s.camera.target));
      return;
    }
    if (!(before !== (s.variant || "neutral") || stepAtCall === 0 ||
          Math.abs((s.explode || 0) - explodeAmt) > 0.5)) return;
    const v = model.variants.get(s.variant || "neutral");
    if (!v) return;
    const b = v.bounds, amt = s.explode || 0;
    const lo = [0, 0, 0], hi = [0, 0, 0];
    for (const [, p] of v.parts) {
      for (let k = 0; k < 3; k++) {
        lo[k] = Math.min(lo[k], p.explode.getComponent(k) * amt);
        hi[k] = Math.max(hi[k], p.explode.getComponent(k) * amt);
      }
    }
    const center = new THREE.Vector3(...b.center).add(
      new THREE.Vector3((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2));
    const size = new THREE.Vector3(b.max[0] - b.min[0] + hi[0] - lo[0],
                                   b.max[1] - b.min[1] + hi[1] - lo[1],
                                   b.max[2] - b.min[2] + hi[2] - lo[2]);
    frameCamera(center, size.length() / 2);
  });
  explodeTarget = s.explode || 0;
  if (reduced) applyExplode(explodeTarget, true);
}

// ---------- project / payload loading ----------
async function loadProject(name) {
  current = name;
  currentStep = -1;
  manifest = await api(`/api/manifest?project=${name}`);
  document.querySelectorAll("#projects button").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.name === name)));
  renderSteps();
  const st = projects.find((p) => p.name === name) || {};
  const tier = st.hashes && st.hashes.certified ? "certified" : "preview";
  if (st.hashes && st.hashes[tier]) {
    await fetchPayload(name, tier, st.hashes[tier]);
  } else {
    disposeModel();
    setBadge(null, "no model yet");
    document.getElementById("caption").textContent =
      "No build yet. Use Rebuild certified to build this project.";
  }
  goStep(0, false);
}

async function fetchPayload(name, tier, hash) {
  if (shownHashes[name] && shownHashes[name][tier] === hash && model && model.tier === tier) return;
  const payload = await api(`/payload/${name}-${tier}.json`);
  if (payload.hash !== hash) return; // stale fetch
  shownHashes[name] = { ...(shownHashes[name] || {}), [tier]: hash };
  const keepDir = camera.position.clone().sub(controls.target);
  await buildModel(payload);
  if (keepDir.lengthSq() > 0) {
    const b = model.bounds;
    const dist = 2.5 * b.radius;
    const dir = keepDir.normalize();
    controls.target.set(...b.center);
    camera.position.copy(controls.target).add(dir.multiplyScalar(dist));
    camera.near = Math.max(0.1, b.radius / 50);
    camera.far = b.radius * 40;
    camera.updateProjectionMatrix();
    controls.update();
  }
  setBadge(tier, tier === "certified"
    ? `certified · gates ${payload.gates && payload.gates.pass ? "passed" : "FAILED"} · ${hash.slice(0, 8)}`
    : `preview · not gate-checked · ${hash.slice(0, 8)}`);
  if (currentStep >= 0) {
    const keep = currentStep;
    currentStep = -1;
    goStep(keep, false);
  }
}

function setBadge(tier, text) {
  const el = document.getElementById("tierbadge");
  el.textContent = text;
  el.className = "badge" + (tier === "preview" ? " preview" : tier === "certified" ? " ok" : "");
  document.getElementById("buildline").textContent = "";
}
function setStale(text) {
  document.getElementById("buildline").innerHTML = `<span class="spin"></span> ${text}`;
}

// ---------- picking ----------
const ray = new THREE.Raycaster();
let selected = null;
renderer.domElement.addEventListener("pointerdown", (e) => {
  if (!model) return;
  const r = renderer.domElement.getBoundingClientRect();
  const nd = new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1,
                               -((e.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(nd, camera);
  const [, v] = activeVariant();
  if (!v) return;
  const meshes = [];
  for (const [, p] of v.parts) meshes.push(...p.meshes);
  const hit = ray.intersectObjects(meshes, false)[0];
  const box = document.getElementById("selection");
  if (hit) {
    selected = hit.object.userData.part;
    document.getElementById("selname").textContent = selected;
    box.style.display = "flex";
  } else {
    selected = null;
    box.style.display = "none";
  }
});
function showOnly(name) {
  if (!model) return;
  const [, v] = activeVariant();
  if (!v) return;
  for (const [n, p] of v.parts)
    for (const m of p.meshes) m.visible = !name || n === name;
}
document.getElementById("selisolate").addEventListener("click", () => selected && showOnly(selected));
document.getElementById("selhide").addEventListener("click", () => {
  if (!model || !selected) return;
  const [, v] = activeVariant();
  if (!v || !v.parts.get(selected)) return;
  for (const m of v.parts.get(selected).meshes) m.visible = false;
});
document.getElementById("selshow").addEventListener("click", () => showOnly(null));

// ---------- tools ----------
document.getElementById("explode").addEventListener("input", (e) => {
  explodeTarget = e.target.value / 100;
  if (reduced) applyExplode(explodeTarget, true);
});
document.getElementById("xraybtn").addEventListener("click", () => applyXray(!xrayOn));
document.getElementById("prev").addEventListener("click", () => goStep(currentStep - 1, true));
document.getElementById("next").addEventListener("click", () => goStep(currentStep + 1, true));
document.getElementById("buildbtn").addEventListener("click", async () => {
  setStale("rebuild queued");
  await fetch("/api/rebuild", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project: current, tier: manifest.on_demand ? "certified" : "both" }) });
});

// ---------- websocket: announces + agent commands ----------
function connectWS() {
  const ws = new WebSocket(`ws://${location.hostname}:${httpPort + 1000}`);
  ws.onmessage = async (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "announce" && m.project === current) {
      const st = projects.find((p) => p.name === current);
      if (st) { st.hashes = { ...(st.hashes || {}), [m.tier]: m.hash }; st.state = m.tier; }
      // certified wins over preview when both are fresh
      if (m.tier === "certified" || !shownHashes[current] || !shownHashes[current].certified) {
        await fetchPayload(current, m.tier, m.hash);
      }
      document.getElementById("buildline").textContent = "";
    } else if (m.type === "status" && m.project === current) {
      if (m.state === "stale" || m.state.startsWith("building")) setStale(m.state.replace("-", " "));
      if (m.state === "preview" || m.state === "certified") document.getElementById("buildline").textContent = "";
    } else if (m.type === "gate_failed" && m.project === current) {
      const el = document.getElementById("tierbadge");
      el.textContent = "gate FAILED — showing last certified";
      el.className = "badge bad";
      document.getElementById("diag").textContent =
        (m.gates.diagnostics || []).slice(0, 6).join("\n");
    } else if (m.type === "build_failed" && m.project === current) {
      document.getElementById("diag").textContent = "Build error: " + m.error;
      setStale("build failed");
    } else if (m.type === "manifest" && m.project === current) {
      manifest = await api(`/api/manifest?project=${current}`);
      renderSteps();
      const keep = currentStep;
      currentStep = -1;
      goStep(keep < 0 ? 0 : keep, false);
    } else if (m.type === "command" && (!m.project || m.project === current)) {
      if (m.step !== undefined && manifest) {
        const i = manifest.steps.findIndex((s) => s.id === m.step);
        if (i >= 0) goStep(i, true);
      }
      if (m.camera) tweenCamera(m.camera.position && new THREE.Vector3(...m.camera.position),
                                m.camera.target && new THREE.Vector3(...m.camera.target));
      if (m.variant) setVariant(m.variant);
      if (m.explode !== undefined) { explodeTarget = m.explode; if (reduced) applyExplode(m.explode, true); }
      if (m.xray !== undefined) applyXray(!!m.xray);
    } else if (m.type === "screenshot" && m.project === current) {
      renderer.render(scene, camera);
      const data = renderer.domElement.toDataURL("image/png");
      await fetch("/api/shot", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: m.id, project: current, data }) });
    }
  };
  ws.onclose = () => setTimeout(connectWS, 2000);
}

// ---------- main loop ----------
function resize() {
  const w = stage.clientWidth, h = stage.clientHeight;
  if (w && h && (renderer.domElement.width !== Math.floor(w * renderer.getPixelRatio()) || true)) {
    renderer.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
}
function tick(now) {
  requestAnimationFrame(tick);
  resize();
  if (camAnim) {
    const k = Math.min(1, (now - camAnim.start) / camAnim.dur);
    const e = 1 - Math.pow(1 - k, 3);
    if (camAnim.p1) camera.position.lerpVectors(camAnim.p0, camAnim.p1, e);
    if (camAnim.t1) controls.target.lerpVectors(camAnim.t0, camAnim.t1, e);
    if (k >= 1) camAnim = null;
  }
  if (Math.abs(explodeTarget - explodeAmt) > 0.001) {
    applyExplode(explodeAmt + (explodeTarget - explodeAmt) * 0.18);
  } else if (explodeAmt !== explodeTarget) {
    applyExplode(explodeTarget, true);
  }
  controls.update();
  renderer.render(scene, camera);
}

window.__cq = { camera, controls, scene, get model() { return model; }, goStep };

// ---------- boot ----------
(async () => {
  projects = await api("/api/projects");
  const nav = document.getElementById("projects");
  for (const p of projects) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = p.name;
    b.dataset.name = p.name;
    b.setAttribute("aria-pressed", "false");
    b.addEventListener("click", () => loadProject(p.name));
    nav.appendChild(b);
  }
  connectWS();
  const first = projects[0];
  if (first) await loadProject(first.name);
  requestAnimationFrame(tick);
})();
