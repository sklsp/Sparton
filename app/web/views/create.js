// Create — ComfyUI generation, dataset curation, LoRA training.

import { api, settleAll, settledValue } from "../api.js";
import {
  h, fill, icon, card, metric, button, badge, skeleton, skeletonMetrics, empty,
  errorState, banner, toast, dialog, num, bytes, title, asyncPanel,
} from "../ui.js";

export default function create(view) {
  view.append(
    h("div.page-head",
      h("div",
        h("h1", "Create"),
        h("p", "Image generation, dataset curation and LoRA training on local hardware."))));

  const statusHost = h("div");
  view.append(statusHost);
  const reloadStatus = asyncPanel(statusHost, loadStatus, renderStatus, { loading: () => skeletonMetrics(3) });

  // --- generate
  const genHost = h("section.card");
  const genBody = h("div.card-body");
  genHost.append(
    h("header.card-head", h("div", h("h2", "Generate"), h("div.sub", "Queue a workflow on the local ComfyUI server"))),
    genBody);
  view.append(genHost);
  asyncPanel(genBody, () => api.workflows(), (data) => renderGenerateForm(data, () => reloadGallery()));

  // --- gallery
  const galleryHost = h("section.card");
  const galleryBody = h("div.card-body");
  const refresh = h("button.icon-btn", { type: "button", "aria-label": "Refresh gallery" }, icon("refresh", 15));
  galleryHost.append(
    h("header.card-head",
      h("div", h("h2", "Generated images"), h("div.sub", "Most recent output first")),
      h("div.card-head-actions", refresh)),
    galleryBody);
  view.append(galleryHost);
  const reloadGallery = asyncPanel(galleryBody, () => api.generated(24), renderGallery);
  refresh.onclick = () => {
    refresh.dataset.busy = "true";
    setTimeout(() => delete refresh.dataset.busy, 600);
    reloadGallery();
  };

  // --- datasets + training
  const grid = h("div.grid.grid-2");
  view.append(grid);

  const datasetsHost = h("section.card");
  const datasetsBody = h("div.card-body.flush");
  const newDataset = button("New", { size: "sm", iconName: "plus", onClick: () => openDatasetDialog(() => reloadDatasets()) });
  datasetsHost.append(
    h("header.card-head",
      h("div", h("h2", "Datasets"), h("div.sub", "Curated image sets for training")),
      h("div.card-head-actions", newDataset)),
    datasetsBody);
  grid.append(datasetsHost);
  const reloadDatasets = asyncPanel(datasetsBody, () => api.datasets(),
    (data, reload) => renderDatasets(data, reload, () => openDatasetDialog(() => reload())));

  const trainingHost = h("section.card");
  const trainingBody = h("div.card-body.flush");
  trainingHost.append(
    h("header.card-head", h("div", h("h2", "Training"), h("div.sub", "LoRA projects and hardware presets"))),
    trainingBody);
  grid.append(trainingHost);
  asyncPanel(trainingBody, loadTraining, renderTraining);

  return () => { /* no timers */ };
}

/* ---------------------------------------------------------------- status */

async function loadStatus() {
  const [comfy, hardware, training] = await settleAll([
    api.comfyStatus(), api.hardware(), api.trainingStatus(),
  ]);
  return {
    comfy: settledValue(comfy, { connected: false, error: comfy.reason?.message }),
    hardware: settledValue(hardware, null),
    training: settledValue(training, null),
  };
}

function renderStatus({ comfy, hardware, training }) {
  const wrap = h("div");
  if (!comfy.connected) {
    // Transport errors arrive as multi-line stack text — keep the headline.
    const reason = String(comfy.error || "").split(/[(\n]/)[0].trim().slice(0, 120);
    wrap.append(banner(
      `ComfyUI is not reachable at ${comfy.base_url || "the configured address"}${reason ? ` — ${reason}` : ""}. Start it locally to generate images; datasets and training remain available.`,
      { tone: "warning" }));
  }

  const device = hardware?.devices?.[0] || comfy.devices?.[0];
  wrap.append(h("div.grid.grid-metrics", { style: { marginTop: comfy.connected ? "0" : "16px" } },
    metric({
      label: "ComfyUI", iconName: "image",
      value: comfy.connected ? "Online" : "Offline",
      tone: comfy.connected ? "success" : "danger",
      sub: comfy.version ? `v${comfy.version}` : comfy.base_url || "not configured",
    }),
    metric({
      label: "Models", iconName: "db",
      value: num((comfy.checkpoints || []).length),
      sub: h("span", h("b", num((comfy.loras || []).length)), " LoRAs available"),
    }),
    metric({
      label: "Hardware", iconName: "cpu",
      value: hardware?.tier ? title(hardware.tier) : device?.name ? "Detected" : "Unknown",
      sub: device?.vram_total
        ? `${bytes(device.vram_total)} VRAM`
        : hardware?.gpu_name || training?.status || "no accelerator reported",
    })));
  return wrap;
}

/* -------------------------------------------------------------- generate */

function renderGenerateForm(data, onQueued) {
  const workflows = data.workflows || [];
  if (!workflows.length) {
    return empty({
      iconName: "image",
      title: "No workflows imported",
      message: "Import a ComfyUI workflow graph via POST /comfyui/workflows and it becomes selectable here.",
    });
  }

  const workflow = h("select.select", { "aria-label": "Workflow" },
    workflows.map((w) => h("option", { value: w.id }, w.name || w.id)));
  const prompt = h("textarea.textarea", { placeholder: "A product photo on a marble surface, soft studio light…", required: true, maxlength: 4000, "aria-label": "Prompt" });
  const negative = h("input.input", { placeholder: "Optional negative prompt", "aria-label": "Negative prompt", maxlength: 4000 });
  const steps = h("input.input", { type: "number", min: 1, max: 150, value: 25, "aria-label": "Steps" });
  const cfg = h("input.input", { type: "number", min: 0.5, max: 30, step: 0.5, value: 7, "aria-label": "CFG" });
  const seed = h("input.input", { type: "number", min: 0, placeholder: "random", "aria-label": "Seed" });
  const submit = button("Queue generation", { variant: "primary", iconName: "bolt", type: "submit" });
  const validationHost = h("div");

  const payloadOf = () => {
    const body = {
      workflow_id: workflow.value,
      prompt: prompt.value.trim(),
      steps: Number(steps.value) || undefined,
      cfg: Number(cfg.value) || undefined,
    };
    if (negative.value.trim()) body.negative_prompt = negative.value.trim();
    if (seed.value !== "") body.seed = Number(seed.value);
    return body;
  };

  const form = h("form", {
    onsubmit: async (event) => {
      event.preventDefault();
      if (!prompt.value.trim()) return;
      submit.dataset.loading = "true";
      try {
        const result = await api.generate(payloadOf());
        toast(`Queued job #${result.job_id} (seed ${result.seed})`, "success");
        onQueued();
      } catch (err) {
        toast(err.message, "danger");
      } finally {
        delete submit.dataset.loading;
      }
    },
  },
    h("div", { style: { display: "grid", gap: "16px" } },
      h("div", { style: { display: "grid", gap: "16px", gridTemplateColumns: "repeat(auto-fit,minmax(180px,1fr))" } },
        h("label.field", h("span", "Workflow"), workflow),
        h("label.field", h("span", "Negative prompt"), negative)),
      h("label.field", h("span", "Prompt"), prompt),
      h("div", { style: { display: "grid", gap: "16px", gridTemplateColumns: "repeat(auto-fit,minmax(120px,1fr))" } },
        h("label.field", h("span", "Steps"), steps),
        h("label.field", h("span", "CFG"), cfg),
        h("label.field", h("span", "Seed"), seed)),
      validationHost,
      h("div", { style: { display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" } },
        h("span.metric-sub", { style: { marginRight: "auto" } }, "Runs on the shared job queue — the gallery updates when it lands."),
        button("Dry run", { iconName: "check", onClick: async (event) => {
          const btn = event.currentTarget;
          if (!prompt.value.trim()) { prompt.focus(); return; }
          btn.dataset.loading = "true";
          try {
            const report = await api.validateGeneration(payloadOf());
            const problems = report.errors || report.issues || [];
            fill(validationHost, problems.length
              ? banner(`${problems.length} problem${problems.length === 1 ? "" : "s"}: ${problems.map((p) => p.message || p).join("; ")}`, { tone: "danger" })
              : banner(report.warnings?.length
                  ? `Valid, with warnings: ${report.warnings.join("; ")}`
                  : "Valid — this request will run against the selected workflow.",
                { tone: report.warnings?.length ? "warning" : "info" }));
          } catch (err) {
            fill(validationHost, banner(err.message, { tone: "danger" }));
          } finally { delete btn.dataset.loading; }
        } }),
        submit)));

  return form;
}

/* --------------------------------------------------------------- gallery */

function renderGallery(data) {
  const images = data.images || [];
  if (!images.length) {
    return empty({
      iconName: "image",
      title: "Nothing generated yet",
      message: "Queue a workflow above and finished images land here.",
    });
  }
  return h("div.gallery",
    images.map((image) =>
      h("figure",
        h("img", {
          src: image.url, alt: image.filename, loading: "lazy", decoding: "async",
          onerror: (event) => { event.currentTarget.replaceWith(h("div", { style: { aspectRatio: "1", display: "grid", placeItems: "center", color: "var(--text-3)" } }, icon("image", 22))); },
        }),
        h("figcaption", { title: image.filename }, image.filename))));
}

/* -------------------------------------------------------------- datasets */

function renderDatasets(data, reload, onCreate) {
  const datasets = data.datasets || [];
  if (!datasets.length) {
    return empty({
      iconName: "db",
      title: "No datasets",
      message: "A dataset groups captioned images into a training set.",
      action: button("Create dataset", { size: "sm", variant: "primary", iconName: "plus", onClick: onCreate }),
    });
  }
  return h("div.rows", datasets.map((dataset) =>
    h("div.row",
      h("div.row-main",
        h("div.row-title", dataset.name),
        h("div.row-sub", [
          dataset.trigger_word && `trigger “${dataset.trigger_word}”`,
          dataset.image_count != null && `${num(dataset.image_count)} images`,
          dataset.description,
        ].filter(Boolean).join(" · ") || "no images yet")),
      h("div.row-side",
        button("Validate", { size: "sm", variant: "ghost", onClick: () => showValidation(dataset) })))));
}

function openDatasetDialog(onCreated) {
  const name = h("input.input", { required: true, maxlength: 160, placeholder: "Product photography v1", "aria-label": "Dataset name" });
  const trigger = h("input.input", { maxlength: 64, placeholder: "sks-product", "aria-label": "Trigger word" });
  const description = h("textarea.textarea", { placeholder: "What is this dataset for?", "aria-label": "Description" });

  const el = dialog({
    title: "New dataset",
    body: h("div", { style: { display: "grid", gap: "16px" } },
      h("label.field", h("span", "Name"), name),
      h("label.field", h("span", "Trigger word"), trigger),
      h("label.field", h("span", "Description"), description)),
    actions: [
      button("Cancel", { variant: "ghost", onClick: () => el.close() }),
      button("Create", { variant: "primary", onClick: async (event) => {
        if (!name.value.trim()) { name.focus(); return; }
        const btn = event.currentTarget;
        btn.dataset.loading = "true";
        try {
          await api.createDataset({
            name: name.value.trim(),
            trigger_word: trigger.value.trim(),
            description: description.value.trim(),
          });
          toast("Dataset created", "success");
          el.close();
          onCreated();
        } catch (err) {
          toast(err.message, "danger");
        } finally {
          delete btn.dataset.loading;
        }
      } }),
    ],
  });
  name.focus();
}

async function showValidation(dataset) {
  const el = dialog({ title: `Validate “${dataset.name}”`, body: skeleton(4), actions: [] });
  const body = el.querySelector(".dialog-body");
  try {
    const report = await api.validateDataset(dataset.id);
    const issues = report.issues || report.errors || [];
    fill(body,
      report.valid === false || issues.length
        ? h("div.banner", { "data-tone": "warning" }, icon("alert", 16),
            h("div", `${issues.length} issue${issues.length === 1 ? "" : "s"} would block training.`))
        : h("div.banner", { "data-tone": "info" }, icon("check", 16), h("div", "This dataset is ready to train.")),
      issues.length
        ? h("div.rows", { style: { border: "1px solid var(--border)", borderRadius: "var(--r-md)" } },
            issues.map((issue) => h("div.row", h("div.row-main",
              h("div.row-title", typeof issue === "string" ? issue : issue.message || issue.code || "Issue"),
              typeof issue === "object" && issue.detail ? h("div.row-sub", issue.detail) : null))))
        : null,
      h("pre.pre", JSON.stringify(report, null, 2)));
  } catch (err) {
    fill(body, errorState({ message: err.message }));
  }
  el.querySelector(".dialog-foot")?.remove();
  el.append(h("footer.dialog-foot", button("Close", { variant: "ghost", onClick: () => el.close() })));
}

/* -------------------------------------------------------------- training */

async function loadTraining() {
  const [projects, presets, status] = await settleAll([
    api.trainingProjects(), api.trainingPresets(), api.trainingStatus(),
  ]);
  return {
    projects: settledValue(projects, {}).projects || [],
    presets: settledValue(presets, {}).presets || [],
    status: settledValue(status, null),
  };
}

function renderTraining({ projects, presets, status }) {
  const wrap = h("div");

  // Only report configuration the backend actually filled in — a wall of
  // `null` reads as broken rather than "not set up yet".
  const known = Object.entries(status || {}).filter(([, value]) =>
    value !== null && value !== undefined && value !== "");
  if (known.length) {
    wrap.append(h("div", { style: { padding: "16px 20px 0" } },
      h("dl.kv", known.slice(0, 5).flatMap(([key, value]) => [
        h("dt", title(key)),
        h("dd", Array.isArray(value)
          ? h("div.chips", value.map((entry) => h("span.chip", String(entry))))
          : typeof value === "boolean"
            ? badge(value ? "yes" : "no", value ? "success" : "neutral")
            : typeof value === "object" ? JSON.stringify(value) : String(value)),
      ]))));
  }

  if (projects.length) {
    wrap.append(h("div.rows", projects.map((project) =>
      h("div.row",
        h("div.row-main",
          h("div.row-title", project.name),
          h("div.row-sub", `preset: ${project.preset || "default"}`)),
        h("div.row-side", badge(project.preset || "default", "primary"))))));
  } else {
    wrap.append(empty({
      iconName: "cpu",
      title: "No training projects",
      message: presets.length
        ? `${presets.length} presets available — create a project against a dataset to start.`
        : "Create a dataset first, then attach a training project to it.",
    }));
  }

  if (presets.length) {
    wrap.append(h("footer.card-foot",
      h("span", "Presets:"),
      h("div.chips", presets.map((preset) => h("span.chip", preset.id || preset.name)))));
  }
  return wrap;
}
