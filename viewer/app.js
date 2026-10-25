import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { toCreasedNormals } from "three/addons/utils/BufferGeometryUtils.js";

const stage = document.getElementById("stage");
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.domElement.setAttribute("role", "img");
renderer.domElement.setAttribute("aria-label", "3D model viewport");
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
camera.up.set(0, 0, 1); // CadQuery workplanes and explode vectors use Z as the vertical axis.
const controls = new OrbitControls(camera, renderer.domElement);

const key = new THREE.DirectionalLight(0xffffff, 1.25);
key.position.set(60, -40, 90);
scene.add(key, new THREE.AmbientLight(0xffffff, 0.15));
scene.environmentIntensity = 0.85;

let reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
controls.enableDamping = !reduced;
const httpPort = Number(location.port || 3939);
async function api(path, options = {}, expectedHash = null) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
// Requests cannot follow a redirect outside the worker's loopback origin.
    const response = await fetch(path, { ...options, cache: "no-store", redirect: "error", signal: controller.signal });
    if (!response.ok) throw new Error(`worker returned ${response.status}`);
    const text = await response.text();
    if (expectedHash && await contentHash(text) !== expectedHash)
      throw new Error("payload changed while loading; waiting for the next build");
    return JSON.parse(text);
  } finally {
    clearTimeout(timeout);
  }
}
async function contentHash(text) {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(bytes), (b) => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
}

// ---------- state ----------
let projects = [];           // [{name, root, state, hashes, ...}]
let current = null;          // project name
let manifest = null;
let model = null;            // visible model; previews never carry certified interactions
let certifiedModel = null;   // retained until a replacement passes and loads completely
let certifiedView = null;
let currentStep = -1, displayedStep = -1;
let explodeAmt = 0, explodeTarget = 0;
let xrayOn = false;
let camAnim = null;
let wsDown = false, rebuildPending = false;
let workerEpoch = 0, cameraRequest = 0, explodeRequest = 0, xrayRequest = 0;
let projectRequest = 0, manifestRequest = 0, variantRequest = 0, projectsRequest = 0, selectionIntent = 0;
const payloadRequests = { preview: 0, certified: 0 };

// ---------- small helpers ----------
const vector = (v) => Array.isArray(v) && v.length === 3 && v.every(Number.isFinite);
const amount = (v) => Number.isFinite(v) && v >= 0 && v <= 1;
const interactive = () => !!(manifest && model && model.tier === "certified");
function validateManifest(value, name) {
  if (!value || value.name !== name || !Array.isArray(value.steps || []) || !Array.isArray(value.chips || []))
    throw new Error("invalid project manifest");
  const steps = value.steps || [], chips = value.chips || [];
  const ids = new Set();
  for (const s of steps) {
    if (!s || typeof s.id !== "string" || ids.has(s.id) || typeof s.label !== "string" ||
        (s.caption !== undefined && typeof s.caption !== "string") ||
        (s.variant !== undefined && typeof s.variant !== "string") ||
        (s.explode !== undefined && !amount(s.explode)) ||
        (s.xray !== undefined && typeof s.xray !== "boolean") ||
        (s.camera && (!vector(s.camera.position) || !vector(s.camera.target))))
      throw new Error("invalid manifest step");
    ids.add(s.id);
  }
  if (chips.some((c) => typeof c !== "string")) throw new Error("invalid manifest chips");
  return { ...value, steps, chips };
}
function notifyWorkbench() {
  if (typeof CustomEvent !== "undefined" && window.dispatchEvent)
    window.dispatchEvent(new CustomEvent("cassquery:viewchange"));
}
function updateTools() {
  const enabled = interactive();
  for (const id of ["explode", "xraybtn", "selshow"])
    document.getElementById(id).disabled = !enabled;
  for (const id of ["selisolate", "selhide"])
    document.getElementById(id).disabled = !enabled || !selected;
  document.querySelectorAll("#steps button").forEach((b) => { b.disabled = !enabled; });
  document.getElementById("prev").disabled = !enabled || currentStep <= 0;
  document.getElementById("next").disabled = !enabled || currentStep < 0 || currentStep >= manifest.steps.length - 1;
  document.getElementById("buildbtn").disabled = !manifest || rebuildPending;
  document.getElementById("resetview").disabled = !model;
  notifyWorkbench();
}
function clearSelection() {
  selected = null;
  downAt = null;
  const v = interactive() ? activeVariant()[1] : null;
  const hidden = v && Array.from(v.parts.values()).some((p) => p.meshes.some((m) => !m.visible));
  document.getElementById("selection").style.display = hidden ? "flex" : "none";
  document.getElementById("selname").textContent = "";
  document.getElementById("selisolate").disabled = true;
  document.getElementById("selhide").disabled = true;
}
function b64indices(s) {
  if (s.itype !== "u16" && s.itype !== "u32") throw new Error("invalid index encoding");
  const bin = atob(s.indices), u8 = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  const out = s.itype === "u16" ? new Uint16Array(u8.buffer) : new Uint32Array(u8.buffer);
  if (!out.length || out.length % 3) throw new Error("invalid triangle indices");
  return out;
}
function b64positions(s) {
  // positions arrive as uint16 quantized (qoffset + q * qscale)
  if (!vector(s.qoffset) || !Number.isFinite(s.qscale) || s.qscale <= 0)
    throw new Error("invalid position encoding");
  const bin = atob(s.positions), u8 = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  const q = new Uint16Array(u8.buffer);
  if (!q.length || q.length % 3) throw new Error("invalid vertex positions");
  const out = new Float32Array(q.length);
  for (let i = 0; i < q.length; i++) out[i] = s.qoffset[i % 3] + q[i] * s.qscale;
  if (!out.every(Number.isFinite)) throw new Error("non-finite vertex positions");
  return out;
}
function fitDist(radius) {
  // distance that fits the bounding sphere inside the smaller of the
  // vertical/horizontal fov (portrait stages are width-bound), +2% margin
  const v = THREE.MathUtils.degToRad(camera.fov / 2);
  const h = Math.atan(Math.tan(v) * camera.aspect);
  return radius / Math.sin(Math.min(v, h)) * 1.02;
}
function stopControlMotion() {
  const position = camera.position.clone(), target = controls.target.clone();
  const damping = controls.enableDamping;
  controls.enableDamping = false;
  controls.update(); // Consume OrbitControls' pending rotation/pan without retaining its motion.
  controls.enableDamping = damping;
  camera.position.copy(position);
  controls.target.copy(target);
  controls.update();
}
function cameraDepth(radius) {
  camera.near = radius / 50;
  camera.far = Math.max(radius * 40, fitDist(radius) + radius * 2);
  camera.updateProjectionMatrix();
}
function frameCamera(center, radius, dir = new THREE.Vector3(0.95, -1.35, 0.62)) {
  camAnim = null;
  stopControlMotion();
  const d = dir.clone().normalize().multiplyScalar(fitDist(radius));
  camera.position.copy(center).add(d);
  controls.target.copy(center);
  cameraDepth(radius);
  controls.update();
}
function tweenCamera(pos, target) {
  ++cameraRequest;
  camAnim = null;
  stopControlMotion();
  if (reduced) {
    if (pos) camera.position.copy(pos);
    if (target) controls.target.copy(target);
    controls.update();
    return;
  }
  camAnim = { p0: camera.position.clone(), t0: controls.target.clone(),
              p1: pos ? pos.clone() : null, t1: target ? target.clone() : null,
              start: performance.now(), dur: 550 };
}

// ---------- model ----------
function disposeGroup(group, materials = new Set()) {
  group.traverse((o) => {
    if (o.geometry) o.geometry.dispose();
    if (o.material) materials.add(o.material);
  });
  for (const material of materials) material.dispose();
}
function disposeModel(value = model) {
  if (!value || value.disposed) return;
  value.disposed = true;
  scene.remove(value.group);
  const materials = new Set();
  for (const [, variant] of value.variants)
    for (const [, part] of variant.parts) materials.add(part.mat);
  disposeGroup(value.group, materials);
  if (model === value) model = null;
  if (certifiedModel === value) certifiedModel = null;
}
function validateBounds(b) {
  if (!b || !vector(b.min) || !vector(b.max) || !vector(b.center) ||
      !Number.isFinite(b.radius) || b.radius <= 0 || b.min.some((x, i) => x > b.max[i]))
    throw new Error("invalid model bounds");
}
async function validateIndex(index, name, tier, hash) {
  if (!index || index.project !== name || index.tier !== tier || index.hash !== hash ||
      !/^[a-f0-9]{16}$/.test(hash) || !Number.isInteger(index.gen) || index.gen < 0 ||
      !index.variants || !Object.hasOwn(index.variants, "neutral"))
    throw new Error("invalid model index");
  if (tier === "certified") {
    const ids = ["units", "watertight", "volume", "thin-extent", "triangulation",
                 "payload-roundtrip", "budget-tris", "budget-bytes"];
    if (index.gates?.pass !== true || !Array.isArray(index.gates.checks) ||
        index.gates.checks.some((c) => !c || c.status !== "pass") ||
        ids.some((id) => !index.gates.checks.some((c) => c.id === id)))
      throw new Error("certified gates did not pass");
  }
  for (const [variant, meta] of Object.entries(index.variants)) {
    if (!meta || !/^[a-f0-9]{16}$/.test(meta.hash) || typeof meta.url !== "string")
      throw new Error("invalid variant metadata");
    const base = `/payload/${encodeURIComponent(name)}-${tier}-${encodeURIComponent(variant)}`;
    const allowed = [new URL(base + ".json", location.origin).href,
                     new URL(base + "-" + meta.hash + ".json", location.origin).href];
    if (!allowed.includes(new URL(meta.url, location.origin).href))
      throw new Error("invalid variant metadata");
  }
  const hashes = Object.keys(index.variants).sort().map((n) => index.variants[n].hash).join("");
  if (await contentHash(hashes) !== hash) throw new Error("invalid model content hash");
}
async function buildModel(index) {
  const value = { group: new THREE.Group(), variants: new Map(), pending: new Map(),
    index, hash: index.hash, tier: index.tier, gates: index.gates,
    bounds: null, activeName: "neutral", disposed: false, epoch: workerEpoch };
  try {
    const entry = await loadVariant("neutral", value);
    entry.group.visible = true;
    return value;
  } catch (error) {
    disposeModel(value);
    throw error;
  }
}

// Variants load lazily, deduplicated per model. Fixed filenames are mutable:
// verify the bytes against the index hash before using them, even for neutral.
async function loadVariant(name, value = model) {
  if (!value || value.disposed) return undefined;
  if (value.variants.has(name)) return value.variants.get(name);
  if (value.pending.has(name)) return value.pending.get(name);
  if (!Object.hasOwn(value.index.variants, name)) throw new Error(`unknown variant ${name}`);
  const meta = value.index.variants[name];
  const pending = (async () => {
    const v = await api(meta.url, {}, meta.hash);
    if (value.disposed) return undefined;
    validateBounds(v.bounds);
    if (!Array.isArray(v.parts)) throw new Error("invalid model parts");
    const vgroup = new THREE.Group(), vparts = new Map();
    vgroup.visible = false;
    try {
      for (const part of v.parts) {
        if (!part || typeof part.name !== "string" || vparts.has(part.name) ||
            !vector(part.explode) || !Array.isArray(part.solids) || !part.finish ||
            typeof part.finish.color !== "string" ||
            !amount(part.finish.metalness) || !amount(part.finish.roughness) ||
            !["ghost", "opaque"].includes(part.xray))
          throw new Error("invalid semantic part");
        const meshes = [];
        const mat = new THREE.MeshStandardMaterial({
          color: new THREE.Color(part.finish.color),
          metalness: part.finish.metalness, roughness: part.finish.roughness });
        vparts.set(part.name, { meshes, mat, xray: part.xray,
          explode: new THREE.Vector3(...part.explode) });
        for (const s of part.solids) {
          const positions = b64positions(s), indices = b64indices(s);
          if (indices.some((i) => i >= positions.length / 3)) throw new Error("triangle index outside vertices");
          const g = new THREE.BufferGeometry();
          const mesh = new THREE.Mesh(g, mat);
          vgroup.add(mesh);
          meshes.push(mesh);
          g.setAttribute("position", new THREE.BufferAttribute(positions, 3));
          g.setIndex(new THREE.BufferAttribute(indices, 1));
          // Welded CAD vertices share sharp boundaries; split normals at creases
          // while retaining smooth normals across curved tessellation facets.
          const creased = toCreasedNormals(g, Math.PI / 3);
          mesh.geometry = creased;
          if (creased !== g) g.dispose();
          mesh.userData.part = part.name;
        }
      }
    } catch (error) {
      // Parts without meshes still own a material.
      for (const [, part] of vparts) part.mat.dispose();
      vgroup.traverse((o) => { if (o.geometry) o.geometry.dispose(); });
      throw error;
    }
    value.group.add(vgroup);
    const entry = { group: vgroup, parts: vparts, bounds: v.bounds };
    for (const [part, p] of vparts)
      for (const mesh of p.meshes) mesh.visible = !value.hiddenParts?.get(name)?.includes(part);
    value.variants.set(name, entry);
    if (name === "neutral") value.bounds = v.bounds;
    return entry;
  })();
  value.pending.set(name, pending);
  try { return await pending; }
  finally { value.pending.delete(name); }
}

function activeVariant() {
  if (model && model.activeName && model.variants.has(model.activeName))
    return [model.activeName, model.variants.get(model.activeName)];
  return ["neutral", model && model.variants.get("neutral")];
}
async function setVariant(name) {
  if (!interactive()) return false;
  const value = model, request = ++variantRequest;
  const loading = !value.variants.has(name);
  const loadingText = `loading ${name} view`;
  if (loading) setStale(loadingText);
  try {
    const entry = await loadVariant(name, value);
    if (!entry || model !== value || request !== variantRequest) return false;
    model.activeName = name;
    for (const [n, v] of model.variants) v.group.visible = n === name;
    clearSelection();
    applyExplode(explodeAmt, true);
    applyXray(xrayOn);
    return true;
  } catch (e) {
    if (model === value && request === variantRequest)
      document.getElementById("diag").textContent = `Could not load the ${name} view: ${e.message || e}`;
    return false;
  } finally {
    const line = document.getElementById("buildline");
    if (model === value && request === variantRequest && line.textContent.trim().startsWith("loading "))
      line.textContent = "";
  }
}
function applyExplode(amt, hard = false) {
  explodeAmt = amt;
  document.getElementById("explode").value = Math.round(amt * 100);
  document.getElementById("explodeval").textContent = Math.round(amt * 100) + "%";
  if (!model) return;
  const [, v] = activeVariant();
  if (!v) return;
  for (const [, p] of v.parts)
    for (const m of p.meshes) m.position.copy(p.explode).multiplyScalar(amt);
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
  const chips = document.getElementById("chips");
  chips.innerHTML = "";
  for (const c of manifest.chips) {
    const li = document.createElement("li");
    li.textContent = c;
    chips.append(li);
  }
}
function updateStepText() {
  const s = manifest?.steps[currentStep];
  document.getElementById("caption").textContent = s ? (s.caption || "") : "";
  document.querySelectorAll("#steps button").forEach((b, j) =>
    b.setAttribute("aria-pressed", String(j === currentStep)));
  updateTools();
}
async function goStep(i, animate) {
  if (!interactive() || !Number.isInteger(i) || i < 0 || i >= manifest.steps.length) return false;
  const value = model, project = projectRequest, oldStep = displayedStep;
  const s = manifest.steps[i], before = activeVariant()[0];
  const cameraAtCall = cameraRequest, xrayAtCall = xrayRequest, explodeAtCall = explodeRequest;
  currentStep = i;
  if (animate) camAnim = null;
  updateStepText();
  const loading = setVariant(s.variant || "neutral"), request = variantRequest;
  const success = await loading;
  if (model !== value || project !== projectRequest || currentStep !== i || request !== variantRequest) return false;
  if (!success) {
    currentStep = oldStep;
    updateStepText();
    return false;
  }
  displayedStep = i;
  if (xrayAtCall === xrayRequest) applyXray(!!s.xray);
  const oldExplode = explodeAmt;
  if (explodeAtCall === explodeRequest) explodeTarget = s.explode || 0;
  if (reduced || !animate) applyExplode(explodeTarget, true);
  if (cameraAtCall !== cameraRequest) return true;
  if (animate && s.camera) {
    tweenCamera(new THREE.Vector3(...s.camera.position), new THREE.Vector3(...s.camera.target));
  } else if (animate && (before !== (s.variant || "neutral") || i === 0 ||
                        Math.abs(explodeTarget - oldExplode) > 0.5)) {
    const b = viewBounds(explodeTarget);
    frameCamera(b.center, b.radius);
  }
  return true;
}

// ---------- project / payload loading ----------
function captureView() {
  return { step: manifest?.steps[displayedStep]?.id, variant: model?.activeName || "neutral",
    explode: explodeAmt, target: explodeTarget, xray: xrayOn,
    hiddenParts: new Map([...(model.hiddenParts || []), ...[...model.variants].map(([name, v]) =>
      [name, [...v.parts].filter(([, p]) => p.meshes.some((m) => !m.visible)).map(([part]) => part)])]) };
}
function restoreView(view) {
  currentStep = view ? manifest.steps.findIndex((s) => s.id === view.step) : -1;
  displayedStep = currentStep;
  model.hiddenParts = view?.hiddenParts || new Map();
  for (const [name, v] of model.variants)
    for (const [part, p] of v.parts)
      for (const m of p.meshes) m.visible = !model.hiddenParts.get(name)?.includes(part);
  clearSelection();
  explodeTarget = view?.target || 0;
  applyExplode(view?.explode || 0, true);
  applyXray(view?.xray || false);
  updateStepText();
}
function viewBounds(amt = explodeAmt) {
  const v = activeVariant()[1], b = v.bounds, lo = [0, 0, 0], hi = [0, 0, 0];
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
  return { center, radius: Math.max(b.radius, size.length() / 2) };
}
function fitModel() {
  const b = viewBounds();
  const dir = camera.position.clone().sub(controls.target);
  frameCamera(b.center, b.radius,
    dir.lengthSq() ? dir : new THREE.Vector3(0.95, -1.35, 0.62));
}
function setCameraView(name) {
  if (!model) return false;
  const directions = { front: [0, -1, 0], side: [1, 0, 0], top: [0.000001, -0.000001, 1], iso: [0.95, -1.35, 0.62] };
  if (!Object.hasOwn(directions, name)) return false;
  ++cameraRequest;
  const b = viewBounds(); frameCamera(b.center, b.radius, new THREE.Vector3(...directions[name]));
  return true;
}
function selectPart(name) {
  if (!interactive() || !activeVariant()[1].parts.has(name)) return false;
  selected = name; document.getElementById("selname").textContent = name;
  document.getElementById("selection").style.display = "flex";
  updateTools(); return true;
}
async function refreshProjects(name = current) {
  const intent = ++selectionIntent, request = ++projectsRequest;
  const value = validateProjects(await api("/api/projects"));
  if (request === projectsRequest) { projects = value; renderProjects(); }
  if (intent !== selectionIntent || !name || !projects.some((p) => p.name === name)) return false;
  const loaded = await loadProject(name, intent);
  notifyWorkbench();
  return loaded !== false && intent === selectionIntent && current === name && !!manifest;
}
async function reloadManifest(name = current) {
  const project = projectRequest, request = ++manifestRequest;
  const value = validateManifest(await api(`/api/manifest?project=${encodeURIComponent(name)}`), name);
  if (project !== projectRequest || request !== manifestRequest || name !== current) return false;
  const step = manifest?.steps[currentStep]?.id, displayed = manifest?.steps[displayedStep]?.id;
  ++variantRequest;
  manifest = value;
  currentStep = manifest.steps.findIndex((s) => s.id === step);
  displayedStep = manifest.steps.findIndex((s) => s.id === displayed);
  renderSteps();
  updateStepText();
  return true;
}
async function loadProject(name, intent = ++selectionIntent) {
  if (intent !== selectionIntent) return false;
  if (name === current && manifest) return true;
  if (window.__cq?.beforeProjectChange && !await window.__cq.beforeProjectChange(name)) return false;
  if (intent !== selectionIntent) return false;
  const request = ++projectRequest;
  ++manifestRequest; ++variantRequest;
  ++payloadRequests.preview; ++payloadRequests.certified;
  current = name;
  rebuildPending = false;
  ++commandRequest; commandWork = Promise.resolve();
  manifest = null;
  currentStep = -1; displayedStep = -1;
  camAnim = null;
  disposeModel(model);
  disposeModel(certifiedModel);
  certifiedView = null;
  clearSelection();
  applyExplode(0, true); explodeTarget = 0; applyXray(false);
  document.getElementById("steps").innerHTML = "";
  document.getElementById("chips").innerHTML = "";
  document.getElementById("caption").textContent = "";
  document.getElementById("diag").textContent = "";
  document.querySelectorAll("#projects button").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.name === name)));
  setBadge(null, "loading…");
  hideStageMsg();
  updateTools();
  try {
    if (!await reloadManifest(name) || request !== projectRequest) return;
    const st = projects.find((p) => p.name === name) || {};
    const tier = st.hashes?.certified ? "certified" : "preview";
    if (st.hashes?.[tier]) await fetchPayload(name, tier, st.hashes[tier]);
    if (request !== projectRequest) return;
    if (tier === "certified" && st.hashes?.preview && !["gate-failed", "error"].includes(st.state))
      await fetchPayload(name, "preview", st.hashes.preview);
    if (request !== projectRequest) return;
    if (!model) {
      setBadge(null, "no model yet");
      document.getElementById("caption").textContent =
        "No build yet. Use Rebuild certified to build this project.";
    }
    reflectStatus(st);
  } catch (e) {
    if (request === projectRequest)
      document.getElementById("diag").textContent = `Could not load ${name}: ${e.message || e}`;
  }
}
function canDisplay(index) {
  return !model || model.epoch !== workerEpoch || index.gen > model.index.gen ||
    (index.gen === model.index.gen && !(index.tier === "preview" && model.tier === "certified"));
}
async function fetchPayload(name, tier, hash) {
  if (name !== current || !["preview", "certified"].includes(tier)) return false;
  const project = projectRequest, request = ++payloadRequests[tier];
  const fresh = () => project === projectRequest && name === current && request === payloadRequests[tier];
  let candidate = null;
  try {
    const payload = await api(`/payload/${encodeURIComponent(name)}-${tier}.json`);
    if (!fresh()) return false;
    if (payload.hash !== hash) throw new Error("payload changed while loading; waiting for the next build");
    await validateIndex(payload, name, tier, hash);
    if (!fresh()) return false;
    if (model?.index.project === name && model.hash === hash && model.tier === tier &&
        (model.epoch !== workerEpoch || payload.gen >= model.index.gen)) {
      model.index = payload;
      model.epoch = workerEpoch;
      document.getElementById("diag").textContent = "";
      setBadge(tier, tier === "certified" ? `certified · gates passed · ${hash.slice(0, 8)}`
        : `preview · not gate-checked · ${hash.slice(0, 8)}`);
      return true;
    }
    if (!canDisplay(payload) && tier !== "certified") return false;
    candidate = await buildModel(payload);
    if (!fresh()) { disposeModel(candidate); return false; }
    if (tier === "certified" && certifiedModel?.epoch === workerEpoch && payload.gen < certifiedModel.index.gen) {
      disposeModel(candidate); return false;
    }
    let view = interactive() ? captureView() : certifiedView;
    if (tier === "certified" && view) {
      // A step/command may change while its replacement variant is loading.
      // Carry the latest visible variant, not the one captured before the fetch.
      while (Object.hasOwn(payload.variants, view.variant)) {
        await loadVariant(view.variant, candidate);
        if (!fresh()) { disposeModel(candidate); return false; }
        const latest = interactive() ? captureView() : certifiedView;
        if (!latest || latest.variant === view.variant) { view = latest; break; }
        view = latest;
      }
      if (view && candidate.variants.has(view.variant)) {
        candidate.activeName = view.variant;
        for (const [n, v] of candidate.variants) v.group.visible = n === view.variant;
      }
    }
    if (tier === "certified" && view && !candidate.variants.has(view.variant)) view = null;
    if (!canDisplay(payload)) {
      if (tier !== "certified") { disposeModel(candidate); return false; }
      const old = certifiedModel;
      certifiedModel = candidate;
      certifiedView = view;
      if (old && old !== model) disposeModel(old);
      return false;
    }
    const old = model, oldCertified = certifiedModel;
    if (old?.tier === "certified") certifiedView = captureView();
    ++variantRequest;
    camAnim = null;
    model = candidate;
    scene.add(model.group);
    if (old) scene.remove(old.group);
    if (tier === "certified") certifiedModel = model;
    if (old && old !== certifiedModel) disposeModel(old);
    if (oldCertified && oldCertified !== certifiedModel && oldCertified !== old) disposeModel(oldCertified);
    clearSelection();
    restoreView(tier === "certified" ? view : null);
    fitModel();
    hideStageMsg();
    document.getElementById("diag").textContent = "";
    setBadge(tier, tier === "certified"
      ? `certified · gates passed · ${hash.slice(0, 8)}`
      : `preview · not gate-checked · ${hash.slice(0, 8)}`);
    if (tier === "certified" && !view && manifest.steps.length) await goStep(0, !oldCertified);
    return true;
  } catch (e) {
    if (candidate && candidate !== model && candidate !== certifiedModel) disposeModel(candidate);
    if (fresh()) document.getElementById("diag").textContent =
      `Could not load the ${tier} model for ${name}: ${e.message || e}`;
    return false;
  }
}
function restoreCertified() {
  ++payloadRequests.preview; ++payloadRequests.certified; ++variantRequest;
  camAnim = null;
  if (certifiedModel && model !== certifiedModel) {
    disposeModel(model);
    model = certifiedModel;
    scene.add(model.group);
    clearSelection();
    restoreView(certifiedView);
    fitModel();
  }
}
function gateFailed(gates) {
  restoreCertified();
  setBadge(null, certifiedModel ? "gate FAILED — showing last certified" : "gate FAILED — no certified model");
  document.getElementById("tierbadge").className = "badge bad";
  document.getElementById("diag").textContent = (gates?.diagnostics || []).slice(0, 6).join("\n");
}
function reflectStatus(st) {
  if (st.state === "stale" || st.state?.startsWith("building")) setStale(st.state.replace("-", " "));
  else if (st.state === "gate-failed") gateFailed(st.gates || { diagnostics: [st.detail || "Certified gates failed"] });
  else if (st.state === "error") {
    restoreCertified();
    setBadge(null, certifiedModel ? "build failed — showing last certified" : "build failed — no certified model");
    document.getElementById("tierbadge").className = "badge bad";
    document.getElementById("buildline").textContent = "build failed";
    const err = (st.history || []).filter((h) => h.event === "error").pop();
    document.getElementById("diag").textContent = "Build error: " + (st.detail || err?.detail || "unknown error");
  } else document.getElementById("buildline").textContent = wsDown ? "disconnected — reconnecting" : "";
  notifyWorkbench();
}

function setBadge(tier, text) {
  const el = document.getElementById("tierbadge");
  el.textContent = text;
  el.className = "badge" + (tier === "preview" ? " preview" : tier === "certified" ? " ok" : "");
  document.getElementById("buildline").textContent = wsDown ? "disconnected — reconnecting" : "";
  notifyWorkbench();
}
function setStale(text) {
  const line = document.getElementById("buildline"), spin = document.createElement("span");
  if (wsDown) { line.textContent = "disconnected — reconnecting"; return; }
  spin.className = "spin";
  line.replaceChildren(spin, document.createTextNode(" " + text));
}
function showStageMsg(title, detail) {
  const el = document.getElementById("stage-msg");
  el.innerHTML = "";
  const s = document.createElement("strong");
  s.textContent = title;
  el.append(s, document.createTextNode(detail || ""));
  el.hidden = false;
}
function hideStageMsg() {
  document.getElementById("stage-msg").hidden = true;
}

// ---------- picking ----------
const ray = new THREE.Raycaster();
let selected = null;
let downAt = null;
// Pick on release, and only when the pointer barely moved: a press that
// turns into an orbit drag must not also select (or deselect) a part.
renderer.domElement.addEventListener("pointerdown", (e) => {
  downAt = interactive() && e.button === 0 ? [e.clientX, e.clientY, e.pointerId, false] : null;
});
renderer.domElement.addEventListener("pointermove", (e) => {
  if (downAt && e.pointerId === downAt[2] && Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 6)
    downAt[3] = true;
});
renderer.domElement.addEventListener("pointercancel", () => { downAt = null; });
renderer.domElement.addEventListener("pointerup", (e) => {
  if (!downAt || e.pointerId !== downAt[2]) return;
  const moved = downAt[3] || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 6;
  downAt = null;
  if (moved || !interactive()) return;
  const r = renderer.domElement.getBoundingClientRect();
  if (!r.width || !r.height) return;
  const nd = new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1,
                               -((e.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(nd, camera);
  const [, v] = activeVariant();
  if (!v) return;
  const meshes = [];
  for (const [, p] of v.parts) meshes.push(...p.meshes.filter((m) => m.visible));
  const hit = ray.intersectObjects(meshes, false)[0];
  const box = document.getElementById("selection");
  if (hit) {
    selected = hit.object.userData.part;
    document.getElementById("selname").textContent = selected;
    box.style.display = "flex";
  } else {
    selected = null;
    document.getElementById("selname").textContent = "";
    const hidden = Array.from(v.parts.values()).some((p) => p.meshes.some((m) => !m.visible));
    box.style.display = hidden ? "flex" : "none"; // keep Show all reachable after hiding the last part
  }
  updateTools();
});
function showOnly(name) {
  if (!interactive()) return;
  const [, v] = activeVariant();
  if (!v) return;
  for (const [n, p] of v.parts)
    for (const m of p.meshes) m.visible = !name || n === name;
  updateTools();
}
document.getElementById("selisolate").addEventListener("click", () => selected && showOnly(selected));
document.getElementById("selhide").addEventListener("click", () => {
  if (!interactive() || !selected) return;
  const [, v] = activeVariant();
  if (!v || !v.parts.get(selected)) return;
  for (const m of v.parts.get(selected).meshes) m.visible = false;
  updateTools();
});
document.getElementById("selshow").addEventListener("click", () => showOnly(null));

// ---------- tools ----------
document.getElementById("explode").addEventListener("input", (e) => {
  if (!interactive()) return;
  const value = Number(e.target.value) / 100;
  if (!amount(value)) return;
  ++explodeRequest;
  explodeTarget = value;
  if (reduced) applyExplode(explodeTarget, true);
});
document.getElementById("xraybtn").addEventListener("click", () => {
  if (interactive()) { ++xrayRequest; applyXray(!xrayOn); }
});
document.getElementById("prev").addEventListener("click", () => goStep(currentStep - 1, true));
document.getElementById("next").addEventListener("click", () => goStep(currentStep + 1, true));
document.getElementById("buildbtn").addEventListener("click", async (e) => {
  if (!manifest || e.currentTarget.disabled) return;
  if (window.__cq?.beforeBuild && !await window.__cq.beforeBuild()) return;
  await rebuildProject();
});
async function rebuildProject() {
  if (!manifest || rebuildPending) return;
  const btn = document.getElementById("buildbtn"), project = projectRequest;
  rebuildPending = true;
  btn.disabled = true;
  setStale("rebuild queued");
  try {
    await api("/api/rebuild", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: current, tier: manifest.on_demand ? "certified" : "both" }) });
  } catch (err) {
    if (project === projectRequest) {
      document.getElementById("diag").textContent = `Could not queue a rebuild: ${err.message || err}`;
      document.getElementById("buildline").textContent = "";
    }
  } finally {
    if (project === projectRequest) { rebuildPending = false; updateTools(); }
  }
}
document.getElementById("resetview").addEventListener("click", () => {
  if (!model || !model.bounds) return;
  const b = viewBounds(explodeTarget);
  const center = b.center;
  const pos = center.clone().add(new THREE.Vector3(0.95, -1.35, 0.62)
    .normalize().multiplyScalar(fitDist(b.radius)));
  cameraDepth(b.radius);
  tweenCamera(pos, center);
});
window.addEventListener("keydown", (e) => {
  if (e.target.closest("input, textarea, select, [contenteditable], #editor-pane")) return;
  if (e.key === "ArrowRight") goStep(currentStep + 1, true);
  else if (e.key === "ArrowLeft") goStep(currentStep - 1, true);
});

// ---------- websocket: announces + agent commands ----------
function validateProjects(value) {
  if (!Array.isArray(value) || value.some((p) => !p || typeof p.name !== "string" ||
      (p.state !== undefined && typeof p.state !== "string")) ||
      new Set(value.map((p) => p.name)).size !== value.length)
    throw new Error("invalid projects response");
  return value;
}
function renderProjects() {
  const nav = document.getElementById("projects");
  nav.innerHTML = "";
  for (const p of projects) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = p.name;
    b.dataset.name = p.name;
    b.setAttribute("aria-pressed", String(p.name === current));
    b.addEventListener("click", () => loadProject(p.name));
    nav.appendChild(b);
  }
}
let commandRequest = 0, commandWork = Promise.resolve();
async function runCommand(m) {
  const project = projectRequest, request = ++commandRequest;
  if ((m.step !== undefined || m.variant !== undefined || m.explode !== undefined || m.xray !== undefined) && !interactive())
    throw new Error("view controls require a certified model");
  if ((m.camera && ((m.camera.position !== undefined && !vector(m.camera.position)) ||
                    (m.camera.target !== undefined && !vector(m.camera.target)))) ||
      (m.explode !== undefined && !amount(m.explode)) ||
      (m.xray !== undefined && typeof m.xray !== "boolean") ||
      (m.variant !== undefined && typeof m.variant !== "string"))
    throw new Error("invalid view command");
  if (m.step !== undefined) {
    const i = manifest.steps.findIndex((s) => s.id === m.step);
    if (i < 0) throw new Error("unknown step");
    if (!await goStep(i, true)) return;
  }
  if (project !== projectRequest || request !== commandRequest) return;
  if (m.variant !== undefined && !await setVariant(m.variant)) return;
  if (project !== projectRequest || request !== commandRequest) return;
  if (m.explode !== undefined) { ++explodeRequest; explodeTarget = m.explode; applyExplode(m.explode, true); }
  if (m.xray !== undefined) { ++xrayRequest; applyXray(m.xray); }
  if (m.camera) tweenCamera(m.camera.position && new THREE.Vector3(...m.camera.position),
                            m.camera.target && new THREE.Vector3(...m.camera.target));
}
async function handleMessage(m) {
  if (!m || typeof m !== "object" || typeof m.type !== "string") throw new Error("invalid worker message");
  if (m.type === "projects") {
    const snapshot = validateProjects(m.projects);
    ++projectsRequest;
    projects = snapshot;
    renderProjects();
    notifyWorkbench();
    return;
  }
  if (m.type === "hello") {
    ++projectsRequest; ++workerEpoch;
    ++payloadRequests.preview; ++payloadRequests.certified;
    projects = validateProjects(m.projects);
    renderProjects();
    if (!current || !projects.some((p) => p.name === current)) {
      if (projects[0]) await loadProject(projects[0].name);
      else showStageMsg("No projects configured", "");
      return;
    }
    // Hello is the reconnect snapshot: missed announcements must not leave an
    // old hash or stale manifest on screen forever.
    if (manifest) {
      const name = current, project = projectRequest;
      if (!await reloadManifest(name) || project !== projectRequest) return;
      if (currentStep !== displayedStep) { currentStep = displayedStep; updateStepText(); }
      const st = projects.find((p) => p.name === name);
      if (st.hashes?.certified) await fetchPayload(name, "certified", st.hashes.certified);
      if (project !== projectRequest) return;
      if (st.hashes?.preview && st.state !== "gate-failed") await fetchPayload(name, "preview", st.hashes.preview);
      if (project === projectRequest) reflectStatus(st);
    }
    return;
  }
  const st = projects.find((p) => p.name === m.project);
  if (m.type === "announce") {
    if (!["preview", "certified"].includes(m.tier) || typeof m.hash !== "string") throw new Error("invalid announcement");
    if (st) { st.hashes = { ...(st.hashes || {}), [m.tier]: m.hash }; st.state = m.tier; }
  } else if (m.type === "status") {
    if (typeof m.state !== "string") throw new Error("invalid worker status");
    if (st) { st.state = m.state; st.detail = m.detail; if (m.hashes) st.hashes = m.hashes; }
  } else if (m.type === "gate_failed" && st) {
    st.state = "gate-failed"; st.gates = m.gates;
  } else if (m.type === "build_failed" && st) {
    st.state = "error"; st.detail = m.detail || m.error;
  }
  if (m.project && m.project !== current) return;
  const project = projectRequest;
  if (m.type === "announce" && manifest) {
    await fetchPayload(current, m.tier, m.hash);
    if (project === projectRequest && st) reflectStatus(st);
  } else if (m.type === "status") {
    reflectStatus(st || m);
  } else if (m.type === "gate_failed") {
    gateFailed(m.gates);
  } else if (m.type === "build_failed") {
    reflectStatus(st || { state: "error", detail: m.detail || m.error });
  } else if (m.type === "manifest") {
    if (await reloadManifest() && project === projectRequest && interactive())
      await goStep(currentStep < 0 ? 0 : currentStep, false);
  } else if (m.type === "command") {
    const work = runCommand(m);
    commandWork = work;
    try { await work; }
    finally { if (commandWork === work) commandWork = Promise.resolve(); }
  } else if (m.type === "screenshot" && m.project === current) {
    await commandWork;
    if (project !== projectRequest) return;
    // Capture the settled command, not an intermediate camera/explode tween.
    if (camAnim) {
      if (camAnim.p1) camera.position.copy(camAnim.p1);
      if (camAnim.t1) controls.target.copy(camAnim.t1);
      camAnim = null;
    }
    applyExplode(explodeTarget, true);
    controls.update();
    renderer.render(scene, camera);
    await api("/api/shot", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: m.id, project: current, data: renderer.domElement.toDataURL("image/png") }) });
  }
}
let socketRequest = 0;
function connectWS() {
  const request = ++socketRequest;
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.hostname}:${httpPort + 1000}`);
  ws.onopen = () => {
    if (request !== socketRequest) return;
    wsDown = false;
    const line = document.getElementById("buildline");
    if (line.textContent === "disconnected — reconnecting") line.textContent = "";
  };
  ws.onmessage = async (ev) => {
    if (request !== socketRequest) return;
    const project = projectRequest;
    try { await handleMessage(JSON.parse(ev.data)); }
    catch (e) {
      if (project === projectRequest)
        document.getElementById("diag").textContent = `Worker message failed: ${e.message || e}`;
    }
  };
  ws.onclose = () => {
    if (request !== socketRequest) return;
    wsDown = true;
    document.getElementById("buildline").textContent = "disconnected — reconnecting";
    setTimeout(connectWS, 2000);
  };
}

// ---------- main loop ----------
let lastW = 0, lastH = 0;
function resize() {
  const w = stage.clientWidth, h = stage.clientHeight;
  if (!w || !h) return;
  const pw = Math.floor(w * renderer.getPixelRatio());
  const ph = Math.floor(h * renderer.getPixelRatio());
  if (pw === lastW && ph === lastH) return; // no layout work when nothing changed
  lastW = pw; lastH = ph;
  renderer.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
function tick(now) {
  requestAnimationFrame(tick);
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

window.__cq = {
  camera, controls, scene, get model() { return model; }, goStep,
  get project() { return current; }, get manifest() { return manifest; },
  get projects() { return projects; }, get selected() { return selected; },
  get status() { return projects.find((p) => p.name === current); },
  get rebuilding() { return rebuildPending; },
  selectPart, refreshProjects, selectProject: loadProject, rebuild: rebuildProject,
  fit: () => model && fitModel(), setCameraView,
  configureView({ reducedMotion } = {}) {
    const next = reducedMotion === "on" || matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (next !== reduced) { stopControlMotion(); reduced = next; controls.enableDamping = !next; }
    scene.background = new THREE.Color(getComputedStyle(document.documentElement).getPropertyValue("--panel").trim());
  },
};
if (typeof CustomEvent !== "undefined" && window.dispatchEvent) window.dispatchEvent(new CustomEvent("cassquery:ready"));

// ---------- boot ----------
resize();
new ResizeObserver(resize).observe(stage);
controls.addEventListener("start", () => { ++cameraRequest; camAnim = null; });
updateTools();
requestAnimationFrame(tick);
connectWS();
(async () => {
  const request = ++projectsRequest;
  try {
    const value = validateProjects(await api("/api/projects"));
    if (request !== projectsRequest) return;
    projects = value;
  } catch (e) {
    if (!current) showStageMsg("Can't reach the CassQuery worker",
      `Tried ${location.origin}/api/projects — is the worker running?`);
    return;
  }
  renderProjects();
  if (!projects.length) {
    showStageMsg("No projects configured",
      "This worker was started without projects. Restart it with project " +
      "directories as arguments, or with CASSQUERY_PROJECTS set.");
  }
  if (!current && projects[0]) await loadProject(projects[0].name);
})();
