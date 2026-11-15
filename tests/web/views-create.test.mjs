// create.js on the DOM fake: status metrics from the comfy/hardware/training endpoints, queueing a
// generation (the POST /comfyui/generate body), dry-run validation (POST /comfyui/validate-generation),
// the gallery, dataset creation and validation reports, and the training panel. Fixtures mirror the
// response shapes in app/api/create_api.py and app/api/training_api.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake; both install on globalThis at import time.
import createView from "../../app/web/views/create.js";
import { resetToasts } from "../../app/web/ui.js";
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom, Event } from "./dom.mjs";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// Fixtures mirror the shapes create_api.py and training_api.py build.
const comfyOnline = {
  connected: true,
  base_url: "http://127.0.0.1:8188",
  version: "0.3.45",
  checkpoints: ["sd_xl_base_1.0.safetensors"],
  loras: ["product_v1.safetensors"],
  devices: [{ name: "NVIDIA RTX 4090", vram_total: 25769803776 }], // 24 GiB
};
const hardwareBody = { gpus: [{ name: "NVIDIA GeForce RTX 4090" }], accelerator: "cuda", cpu_cores: 16, ram_total_mb: 65536 };
const trainingStatusBody = { configured: true, toolkit_path: "C:/ai-toolkit", python: "python", supported_archs: ["sd15", "sdxl"], error: null };
const workflowsBody = { workflows: [{ id: "wf-xl", name: "Product photo XL" }, { id: "wf-studio", name: "Studio shot" }] };
const generatedBody = { images: [
  { url: "/files/generated/2.png", filename: "2.png" },
  { url: "/files/generated/1.png", filename: "1.png" },
] };
const datasetsBody = { count: 1, datasets: [{ id: 7, name: "Product photography v1", trigger_word: "sks-product", image_count: 48, description: "Hero shots on marble" }] };
const presetsBody = { presets: [{ id: "conservative", name: "Conservative" }, { id: "balanced", name: "Balanced" }] };
const projectsBody = { count: 1, projects: [{ id: 3, name: "Product LoRA v2", preset: "balanced" }] };

// Render the create view; record every call as [method, href] and every POST as [href, body].
function renderCreate({
  comfy = comfyOnline, hardware = hardwareBody, trainingStatus = trainingStatusBody,
  workflows = workflowsBody, generated = generatedBody, datasets = datasetsBody,
  presets = presetsBody, projects = projectsBody,
  validateReport = { valid: true, errors: [] },
  datasetValidateReport = { valid: true, issues: [] },
  generateResponse = json({ job_id: 42, seed: 1234567890 }),
  failStatusEndpoints = false,
} = {}) {
  resetEnv();
  resetDom();
  resetToasts(); // ui.js caches its toasts host; a fresh document needs a fresh one.
  const host = document.createElement("main");
  document.body.appendChild(host);
  const calls = [];
  const posts = [];
  setFetch((href, init) => {
    const method = init?.method ?? "GET";
    calls.push([method, href]);
    if (failStatusEndpoints && ["/comfyui/status", "/training/hardware", "/training/status"].includes(href)) {
      throw new Error("endpoint down");
    }
    switch (`${method} ${href}`) {
      case "GET /comfyui/status": return json(comfy);
      case "GET /training/hardware": return json(hardware);
      case "GET /training/status": return json(trainingStatus);
      case "GET /comfyui/workflows": return json(workflows);
      case "POST /comfyui/validate-generation": { const body = JSON.parse(init.body); posts.push([href, body]); return json(validateReport); }
      case "POST /comfyui/generate": { const body = JSON.parse(init.body); posts.push([href, body]); return generateResponse; }
      case "GET /generated?limit=24": return json(generated);
      case "GET /datasets": return json(datasets);
      case "POST /datasets": { const body = JSON.parse(init.body); posts.push([href, body]); return json(body, 201); }
      case "GET /training/presets": return json(presets);
      case "GET /training/projects": return json(projects);
    }
    if (method === "GET" && /^\/datasets\/\d+\/validate$/.test(href)) return json(datasetValidateReport);
    throw new Error(`unexpected fetch ${method} ${href}`);
  });
  createView(host);
  return { calls, posts };
}

async function waitFor(condition, label = "condition") {
  for (let i = 0; i < 400; i++) {
    if (condition()) return condition();
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error(`timed out waiting for ${label}`);
}

const called = (calls, method, href) => calls.some(([m, h]) => m === method && h === href);
const buttonByText = (text) => [...document.querySelectorAll("button")].find((el) => el.textContent.trim() === text);
const sectionByHeading = (heading) => [...document.querySelectorAll("section.card")].find((s) => s.querySelector("h2")?.textContent === heading);
const metricByLabel = (label) => [...document.querySelectorAll(".metric")].find((el) => el.querySelector(".metric-label")?.textContent.trim() === label);

// Fill the generate form and return it. The fake DOM does not auto-select a <select> option, so set it explicitly.
async function filledForm({ promptText = "A product photo on marble, soft studio light", negative = "", seed = "" } = {}) {
  const form = await waitFor(() => document.querySelector("form"), "generate form");
  form.querySelector('select[aria-label="Workflow"]').value = "wf-xl";
  form.querySelector('textarea[aria-label="Prompt"]').value = promptText;
  if (negative) form.querySelector('input[aria-label="Negative prompt"]').value = negative;
  if (seed !== "") form.querySelector('input[aria-label="Seed"]').value = seed;
  return form;
}

test("renders status metrics from the comfy, hardware and training endpoints", async () => {
  const { calls } = renderCreate();
  await waitFor(() => document.querySelectorAll(".metric-label").length === 3, "status metrics");
  assert.ok(called(calls, "GET", "/comfyui/status"));
  assert.ok(called(calls, "GET", "/training/hardware"));
  assert.ok(called(calls, "GET", "/training/status"));

  const comfy = metricByLabel("ComfyUI");
  assert.equal(comfy.querySelector(".metric-value").textContent, "Online");
  assert.equal(comfy.querySelector(".metric-sub").textContent, "v0.3.45");

  const models = metricByLabel("Models");
  assert.equal(models.querySelector(".metric-value").textContent, "1");
  assert.equal(models.querySelector(".metric-sub").textContent, "1 LoRAs available");

  const hardwareMetric = metricByLabel("Hardware");
  assert.equal(hardwareMetric.querySelector(".metric-value").textContent, "Detected");
  assert.equal(hardwareMetric.querySelector(".metric-sub").textContent, "24.0 GB VRAM");
});

test("shows a warning banner and an offline metric when ComfyUI is down", async () => {
  renderCreate({ comfy: { connected: false, base_url: "http://127.0.0.1:8188", error: "fetch failed (ECONNREFUSED)" } });
  const banner = await waitFor(() => document.querySelector(".banner"), "offline banner");
  assert.equal(banner.dataset.tone, "warning");
  assert.match(banner.textContent, /ComfyUI is not reachable at http:\/\/127\.0\.0\.1:8188 \(fetch failed\)/);
  assert.equal(metricByLabel("ComfyUI").querySelector(".metric-value").textContent, "Offline");
});

test("queues a generation with the form payload and refreshes the gallery", async () => {
  const { calls, posts } = renderCreate();
  const form = await filledForm({ negative: "blurry" });
  form.dispatchEvent(new Event("submit"));
  await waitFor(() => posts.length === 1, "generate POST");
  assert.equal(posts[0][0], "/comfyui/generate");
  assert.deepEqual(posts[0][1], { workflow_id: "wf-xl", prompt: "A product photo on marble, soft studio light", negative_prompt: "blurry", steps: 25, cfg: 7 });

  const toastEl = await waitFor(() => document.querySelector(".toast"), "queued toast");
  assert.equal(toastEl.dataset.tone, "success");
  assert.match(toastEl.textContent, /Queued job #42 \(seed 1234567890\)/);

  const galleryLoads = calls.filter(([m, h]) => m === "GET" && h === "/generated?limit=24").length;
  assert.equal(galleryLoads, 2); // initial load + reload after queueing
});

test("includes an explicit seed in the payload when provided", async () => {
  const { posts } = renderCreate();
  const form = await filledForm({ seed: "12345" });
  form.dispatchEvent(new Event("submit"));
  await waitFor(() => posts.length === 1, "generate POST");
  assert.equal(posts[0][1].seed, 12345); // the view sends Number(seed.value)
});

test("dry run posts to /comfyui/validate-generation without queueing a job", async () => {
  const { calls, posts } = renderCreate();
  await filledForm();
  buttonByText("Dry run").click();
  const banner = await waitFor(() => document.querySelector(".banner"), "validation banner");
  assert.equal(banner.dataset.tone, "info");
  assert.match(banner.textContent, /Valid: this request will run against the selected workflow/);
  assert.ok(called(calls, "POST", "/comfyui/validate-generation"));
  assert.deepEqual(posts[0][1], { workflow_id: "wf-xl", prompt: "A product photo on marble, soft studio light", steps: 25, cfg: 7 });
  assert.ok(!called(calls, "POST", "/comfyui/generate"), "dry run must not queue a job");
});

test("dry run surfaces validation problems", async () => {
  renderCreate({ validateReport: { valid: false, errors: [{ message: "Workflow wf-xl not found" }] } });
  await filledForm();
  buttonByText("Dry run").click();
  const banner = await waitFor(() => document.querySelector(".banner"), "problems banner");
  assert.equal(banner.dataset.tone, "danger");
  assert.match(banner.textContent, /1 problem: Workflow wf-xl not found/);
});

test("dry run shows a warning tone when the report has warnings", async () => {
  renderCreate({ validateReport: { valid: true, warnings: ["CFG 7 is high for SDXL"] } });
  await filledForm();
  buttonByText("Dry run").click();
  const banner = await waitFor(() => document.querySelector(".banner"), "warnings banner");
  assert.equal(banner.dataset.tone, "warning");
  assert.match(banner.textContent, /Valid, with warnings: CFG 7 is high for SDXL/);
});

test("shows a danger toast when queueing fails", async () => {
  renderCreate({ generateResponse: json({ detail: "ComfyUI is not reachable" }, 502) });
  const form = await filledForm();
  form.dispatchEvent(new Event("submit"));
  const toastEl = await waitFor(() => document.querySelector(".toast"), "failure toast");
  assert.equal(toastEl.dataset.tone, "danger");
  assert.match(toastEl.textContent, /ComfyUI is not reachable/);
});


test("renders the gallery and its empty state", async () => {
  renderCreate();
  await waitFor(() => document.querySelectorAll(".gallery figure").length === 2, "gallery figures");
  assert.equal(document.querySelector(".gallery img").getAttribute("src"), "/files/generated/2.png");
  assert.equal(document.querySelector(".gallery figcaption").textContent, "2.png");

  renderCreate({ generated: { images: [] } });
  const empty = await waitFor(() => document.querySelector(".empty"), "gallery empty state");
  assert.match(empty.textContent, /Nothing generated yet/);
});

test("generate form shows an empty state when no workflows are imported", async () => {
  renderCreate({ workflows: { workflows: [] } });
  const empty = await waitFor(() => document.querySelector(".empty"), "workflows empty state");
  assert.match(empty.textContent, /No workflows imported/);
});

test("creates a dataset from the dialog", async () => {
  const { calls, posts } = renderCreate();
  buttonByText("New").click();
  const dialogEl = document.querySelector("dialog");
  assert.ok(dialogEl, "dataset dialog opens");
  dialogEl.querySelector('input[aria-label="Dataset name"]').value = "Hero shots v2";
  dialogEl.querySelector('input[aria-label="Trigger word"]').value = "sks-hero";
  dialogEl.querySelector('textarea[aria-label="Description"]').value = "Marble hero shots";
  [...dialogEl.querySelectorAll("button")].find((b) => b.textContent.trim() === "Create").click();

  await waitFor(() => posts.some(([href]) => href === "/datasets"), "dataset POST");
  assert.deepEqual(posts.find(([href]) => href === "/datasets")[1], { name: "Hero shots v2", trigger_word: "sks-hero", description: "Marble hero shots" });

  const toastEl = await waitFor(() => document.querySelector(".toast"), "created toast");
  assert.match(toastEl.textContent, /Dataset created/);
  assert.equal(document.querySelector("dialog"), null, "dialog closes after create");
  assert.equal(calls.filter(([m, h]) => m === "GET" && h === "/datasets").length, 2); // initial + reload
});

test("validates a dataset and reports readiness", async () => {
  const { calls } = renderCreate();
  await waitFor(() => buttonByText("Validate"), "dataset row");
  buttonByText("Validate").click();
  const dialogEl = document.querySelector("dialog");
  assert.ok(dialogEl, "validation dialog opens");
  const banner = await waitFor(() => dialogEl.querySelector(".banner"), "readiness report");
  assert.equal(banner.dataset.tone, "info");
  assert.match(banner.textContent, /This dataset is ready to train/);
  assert.ok(called(calls, "GET", "/datasets/7/validate"));
  const pre = await waitFor(() => dialogEl.querySelector("pre"), "report JSON");
  assert.match(pre.textContent, /"valid": true/);
});

test("dataset validation lists blocking issues with details", async () => {
  renderCreate({ datasetValidateReport: { valid: false, issues: [{ message: "Only 3 images", detail: "Need at least 10 captioned images" }] } });
  await waitFor(() => buttonByText("Validate"), "dataset row");
  buttonByText("Validate").click();
  const dialogEl = document.querySelector("dialog");
  const banner = await waitFor(() => dialogEl.querySelector(".banner"), "issues banner");
  assert.equal(banner.dataset.tone, "warning");
  assert.match(banner.textContent, /1 issue would block training/);
  const rowTitle = await waitFor(() => dialogEl.querySelector(".row-title"), "issue row");
  assert.equal(rowTitle.textContent, "Only 3 images");
  assert.match(dialogEl.querySelector(".row-sub").textContent, /Need at least 10 captioned images/);
});

test("renders the training panel with configuration, projects and presets", async () => {
  const { calls } = renderCreate();
  await waitFor(() => sectionByHeading("Training")?.querySelector(".row-title"), "training project row");
  const training = sectionByHeading("Training");
  assert.ok(called(calls, "GET", "/training/projects"));
  assert.ok(called(calls, "GET", "/training/presets"));

  assert.equal(training.querySelector(".row-title").textContent, "Product LoRA v2");
  assert.match(training.querySelector(".row-sub").textContent, /preset: balanced/);
  assert.ok([...training.querySelectorAll(".badge")].some((b) => b.textContent === "Balanced")); // badge() title-cases

  // error is null, so only the four filled-in keys show, title-cased by title().
  const dts = [...training.querySelectorAll("dt")].map((el) => el.textContent);
  assert.deepEqual(dts, ["Configured", "Toolkit Path", "Python", "Supported Archs"]);
  assert.ok([...training.querySelectorAll("dd")].some((dd) => dd.textContent.trim() === "C:/ai-toolkit"));

  const chips = [...training.querySelectorAll(".chip")].map((el) => el.textContent);
  assert.deepEqual(chips, ["sd15", "sdxl", "conservative", "balanced"]);
});

test("shows an error state when every status endpoint fails", async () => {
  renderCreate({ failStatusEndpoints: true });
  const error = await waitFor(() => document.querySelector(".error-state"), "status error state");
  assert.match(error.textContent, /Could not reach the SPARTON API/);
});

