// create.js on the DOM fake, second pass: the branch sides views-create.test.mjs leaves unexercised. A down
// ComfyUI endpoint reports its error reason while hardware and training stay up; a workflows answer without a
// list shows the import hint, and a workflow without a name is listed by its id; clearing steps or cfg drops
// them from the payload; dry run with an empty prompt posts nothing, and an issues list of plain strings is
// joined verbatim in the plural banner; generated and datasets answers missing their lists show the empty
// states; a dataset validation report without issues or errors renders no rows, and an issue object without
// message or code falls back to "Issue"; projects and presets answers missing their keys leave training idle.
// Fixtures mirror app/api/create.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake; both install on globalThis at import time.
import createView from "../../app/web/views/create.js";
import { resetToasts } from "../../app/web/ui.js";
import { resetEnv, setFetch } from "./env.mjs";
import { Event, resetDom } from "./dom.mjs";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// Fixtures mirror the shapes create.py builds; each test drops one key or value so the view's defensive
// fallback is what renders.
const comfyOnline = { connected: true, base_url: "http://127.0.0.1:8188", version: "0.3.45", checkpoints: ["sd_xl_base_1.0.safetensors"], loras: ["product_v1.safetensors"], devices: [{ name: "NVIDIA RTX 4090", vram_total: 25769803776 }] };
const hardwareBody = { gpus: [{ name: "NVIDIA GeForce RTX 4090" }], accelerator: "cuda", cpu_cores: 16, ram_total_mb: 65536 };
const trainingStatusBody = { configured: true, toolkit_path: "C:/ai-toolkit", python: "python", supported_archs: ["sd15", "sdxl"], error: null };

function renderCreate({ comfy = comfyOnline, hardware = hardwareBody, trainingStatus = trainingStatusBody, workflows = { count: 0, workflows: [] }, generated = { count: 0, images: [] }, datasets = { count: 0, datasets: [] }, presets = { count: 0, presets: [] }, projects = { count: 0, projects: [] }, validateReport = { valid: true, errors: [] }, datasetValidateReport = { valid: true, issues: [] }, failComfyStatusOnly = false } = {}) {
  resetEnv(); resetDom(); resetToasts();
  const host = document.createElement("main");
  document.body.appendChild(host);
  const calls = [];
  const posts = [];
  setFetch((href, init) => {
    const method = init?.method ?? "GET";
    calls.push([method, href]);
    if (failComfyStatusOnly && href === "/comfyui/status") throw new Error("endpoint down");
    switch (`${method} ${href}`) {
      case "GET /comfyui/status": return json(comfy);
      case "GET /training/hardware": return json(hardware);
      case "GET /training/status": return json(trainingStatus);
      case "GET /comfyui/workflows": return json(workflows);
      case "POST /comfyui/validate-generation": { const body = JSON.parse(init.body); posts.push([href, body]); return json(validateReport); }
      case "POST /comfyui/generate": { const body = JSON.parse(init.body); posts.push([href, body]); return json({ job_id: 42, seed: 1234567890 }); }
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
  const deadline = Date.now() + 2000;
  for (;;) {
    if (condition()) return condition();
    if (Date.now() > deadline) throw new Error(`Timed out waiting for ${label}`);
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

const buttonByText = (text) => [...document.querySelectorAll("button")].find((el) => el.textContent.trim() === text);
const sectionByHeading = (heading) => [...document.querySelectorAll("section.card")].find((s) => s.querySelector("h2")?.textContent === heading);

const workflowBody = { count: 1, workflows: [{ id: "wf-xl", name: "Product photo XL" }] };

test("a down ComfyUI endpoint reports its error while hardware and training stay up", async () => {
  renderCreate({ failComfyStatusOnly: true }); // only /comfyui/status rejects; settleAll keeps the other two
  const banner = await waitFor(() => document.querySelector(".banner"), "offline banner");
  assert.equal(banner.dataset.tone, "warning");
  // api.js wraps the thrown fetch error in an ApiError; loadStatus reads its message as the reason headline.
  assert.match(banner.textContent, /ComfyUI is not reachable at the configured address \(Could not reach the SPARTON API/);
});

test("a workflows answer without a list shows the import hint", async () => {
  renderCreate({ workflows: {} }); // no "workflows" key at all
  const empty = await waitFor(() => sectionByHeading("Generate")?.querySelector(".empty"), "workflows empty state");
  assert.match(empty.textContent, /No workflows imported/);
});

test("a workflow without a name is listed by its id", async () => {
  renderCreate({ workflows: { count: 1, workflows: [{ id: "wf-raw" }] } }); // no name key
  const options = await waitFor(() => {
    const list = document.querySelectorAll("option");
    return list.length === 1 ? list : null; // waitFor returns the condition's value, so hand back the array
  }, "workflow option");
  assert.equal(options[0].textContent, "wf-raw");
  assert.equal(options[0].value, "wf-raw");
});

test("clearing steps and cfg drops them from the payload", async () => {
  const { posts } = renderCreate({ workflows: workflowBody });
  const form = await waitFor(() => document.querySelector("form"), "generate form");
  form.querySelector('select[aria-label="Workflow"]').value = "wf-xl";
  form.querySelector('textarea[aria-label="Prompt"]').value = "A product photo on marble";
  form.querySelector('input[aria-label="Steps"]').value = ""; // Number("") is 0, so the key is omitted
  form.querySelector('input[aria-label="CFG"]').value = "";
  form.dispatchEvent(new Event("submit"));
  await waitFor(() => posts.length === 1, "generate POST");
  assert.deepEqual(posts[0][1], { workflow_id: "wf-xl", prompt: "A product photo on marble" }); // no steps or cfg keys
});

test("dry run with an empty prompt focuses it and posts nothing", async () => {
  const { calls } = renderCreate({ workflows: workflowBody });
  await waitFor(() => document.querySelector("form"), "generate form");
  buttonByText("Dry run").click(); // prompt is still empty
  await new Promise((resolve) => setTimeout(resolve, 25));
  assert.ok(!calls.some(([method, href]) => method === "POST" && href === "/comfyui/validate-generation"));
  assert.equal(document.querySelector(".banner"), null); // no validation banner rendered
});

test("dry run reads an issues list of plain strings", async () => {
  renderCreate({ workflows: workflowBody, validateReport: { valid: false, issues: ["Prompt too long", "Unknown node"] } });
  const form = await waitFor(() => document.querySelector("form"), "generate form");
  form.querySelector('select[aria-label="Workflow"]').value = "wf-xl";
  form.querySelector('textarea[aria-label="Prompt"]').value = "A product photo on marble";
  buttonByText("Dry run").click();
  const banner = await waitFor(() => document.querySelector(".banner"), "problems banner");
  assert.equal(banner.dataset.tone, "danger"); // two problems: plural wording, strings joined verbatim
  assert.match(banner.textContent, /2 problems: Prompt too long; Unknown node/);
});

test("a generated answer without an image list shows the empty gallery", async () => {
  renderCreate({ generated: {} }); // no "images" key at all
  const empty = await waitFor(() => sectionByHeading("Generated images")?.querySelector(".empty"), "gallery empty state");
  assert.match(empty.textContent, /Nothing generated yet/);
});

test("a datasets answer without a list shows the create hint", async () => {
  renderCreate({ datasets: {} }); // no "datasets" key at all
  const empty = await waitFor(() => sectionByHeading("Datasets")?.querySelector(".empty"), "datasets empty state");
  assert.match(empty.textContent, /No datasets/);
});

test("a dataset validation report without issues or errors renders no rows", async () => {
  renderCreate({ datasets: { count: 1, datasets: [{ id: 7, name: "Product shots" }] }, datasetValidateReport: {} }); // neither key
  await waitFor(() => buttonByText("Validate"), "dataset row");
  buttonByText("Validate").click();
  const dialogEl = document.querySelector("dialog");
  const banner = await waitFor(() => dialogEl.querySelector(".banner"), "ready banner");
  assert.equal(banner.dataset.tone, "info"); // no issues and no errors: ready to train
  assert.match(banner.textContent, /This dataset is ready to train/);
  assert.equal(dialogEl.querySelectorAll(".row-title").length, 0); // nothing to list
});

test("an issue object without message or code falls back to Issue", async () => {
  renderCreate({ datasets: { count: 1, datasets: [{ id: 7, name: "Product shots" }] }, datasetValidateReport: { valid: false, issues: [{ detail: "blurry" }] } }); // no message, no code
  await waitFor(() => buttonByText("Validate"), "dataset row");
  buttonByText("Validate").click();
  const dialogEl = document.querySelector("dialog");
  const banner = await waitFor(() => dialogEl.querySelector(".banner"), "issues banner");
  assert.equal(banner.dataset.tone, "warning"); // one issue: singular wording
  assert.match(banner.textContent, /1 issue would block training/);
  const row = await waitFor(() => dialogEl.querySelector(".row-title"), "issue row");
  assert.equal(row.textContent, "Issue"); // title falls back to the generic word
  assert.equal(dialogEl.querySelector(".row-sub").textContent, "blurry"); // detail still shown
});

test("projects and presets answers missing their keys leave training idle", async () => {
  renderCreate({ projects: {}, presets: {} }); // no "projects" or "presets" key at all
  const card = await waitFor(() => sectionByHeading("Training")?.querySelector(".empty"), "training empty state");
  assert.match(card.textContent, /No training projects/);
  assert.equal(sectionByHeading("Training").querySelector(".card-foot"), null); // no preset footer (status arch chips are separate)
});



