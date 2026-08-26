// Route table. Views are lazy ES-module imports so the first paint only
// pays for the shell plus the landing view.

export const ROUTES = [
  { id: "overview",     label: "Overview",     icon: "overview",     group: "Platform",     load: () => import("./views/overview.js") },
  { id: "agent",        label: "Agent",        icon: "agent",        group: "Platform",     badge: true, load: () => import("./views/agent.js") },
  { id: "knowledge",    label: "Knowledge",    icon: "knowledge",    group: "Domains",      load: () => import("./views/knowledge.js") },
  { id: "intelligence", label: "Intelligence", icon: "intelligence", group: "Domains",      load: () => import("./views/intelligence.js") },
  { id: "catalog",      label: "Catalog",      icon: "catalog",      group: "Domains",      load: () => import("./views/catalog.js") },
  { id: "create",       label: "Create",       icon: "create",       group: "Domains",      load: () => import("./views/create.js") },
  { id: "system",       label: "System",       icon: "system",       group: "Operations",   load: () => import("./views/system.js") },
];
