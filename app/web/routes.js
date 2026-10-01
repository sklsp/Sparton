// Route table for the customer dashboard. Labels come from i18n (`nav.<id>`).
// Views are lazy ES-module imports, so the first paint only pays for the shell.

export const ROUTES = [
  { id: "overview", load: () => import("./views/overview.js") },
  { id: "alerts",   load: () => import("./views/alerts.js") },
  { id: "shops",    load: () => import("./views/shops.js") },
  { id: "reports",  load: () => import("./views/reports.js") },
  { id: "billing",  load: () => import("./views/billing.js") },
  { id: "settings", load: () => import("./views/settings.js") },
  // Onboarding: reached after signup and from empty states, not from the nav.
  { id: "start", nav: false, load: () => import("./views/start.js") },
  { id: "product", nav: false, parent: "overview", load: () => import("./views/product.js") },
];
