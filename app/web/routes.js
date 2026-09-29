// Route table for the customer dashboard.
//
// The list is filtered at boot against the API's own `GET /billing/plans`-style
// capability report (`GET /health` -> `features`), so a domain that is switched
// off on the server has no navigation entry here at all. A view is never
// merely hidden: if the flag is off, the route does not exist server-side.
//
// Views are lazy ES-module imports, so the first paint only pays for the shell
// plus the landing view.

export const ROUTES = [
  { id: "overview",  label: "Overview",  icon: "overview",     group: "Intelligence", load: () => import("./views/overview.js") },
  { id: "alerts",    label: "Alerts",    icon: "alert",        group: "Intelligence", badge: true, load: () => import("./views/alerts.js") },
  { id: "shops",     label: "Shops",     icon: "shop",         group: "Intelligence", load: () => import("./views/shops.js") },
  { id: "reports",   label: "Reports",   icon: "report",       group: "Intelligence", load: () => import("./views/reports.js") },
  { id: "billing",   label: "Billing",   icon: "billing",      group: "Account",      load: () => import("./views/billing.js") },
  { id: "settings",  label: "Settings",  icon: "settings",     group: "Account",      load: () => import("./views/settings.js") },
];

/** Routes a domain can be restricted to, if that domain is enabled. */
export const DOMAIN_OF = {
  alerts: "commerce",
  shops: "commerce",
  reports: "commerce",
  overview: "commerce",
  billing: "commerce",
  settings: "commerce",
};

