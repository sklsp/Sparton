// A small DOM fake for node --test, just enough for app/web's builders (ui.js, board.js,
// plans.js and i18n.js) to run in Node. Import env.mjs first; importing this file installs
// the document on globalThis next to env's fakes BEFORE an app module is imported. No jsdom:
// only what those modules call.

/** The event object click() builds and dispatchEvent passes to listeners. */
export class Event {
  constructor(type) {
    this.type = type;
    this.defaultPrevented = false;
  }
  preventDefault() {
    this.defaultPrevented = true;
  }
}

function detach(node) {
  const parent = node._parent;
  if (!parent) return;
  parent._childNodes.splice(parent._childNodes.indexOf(node), 1);
  node._parent = null;
}

/** A text node: nodeType 3, one string. */
class TextNode {
  constructor(text) {
    this.nodeType = 3;
    this._text = String(text);
    this._parent = null;
  }
  get textContent() { return this._text; }
  remove() { detach(this); }
}

// dataset.i18nAttr maps to the data-i18n-attr attribute, as in the browser.
function dashCase(name) {
  return name.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase());
}

class Element {
  constructor(tag, namespace = null) {
    this.nodeType = 1;
    this._tag = String(tag);
    this._namespace = namespace;
    this._attributes = new Map();
    this._classes = new Set();
    this._childNodes = [];
    this._listeners = new Map();
    this._parent = null;
    this._innerHTML = "";
    // Reflected properties the builders assign directly (b.lang and b.type in i18n.js,
    // el.id from h()). Kept as own properties so `key in el` behaves like a real element.
    this.id = "";
    this.lang = "";
    this.type = "";
    this.value = "";
    this.disabled = false;
    this.style = {};
  }

  // HTML tags come back uppercase, SVG keeps its lowercase name, as in the browser.
  get tagName() { return this._namespace ? this._tag : this._tag.toUpperCase(); }

  get className() { return [...this._classes].join(" "); }
  set className(value) { this._classes = new Set(String(value).split(/\s+/).filter(Boolean)); }

  get classList() {
    const classes = this._classes;
    return {
      add: (...names) => names.filter(Boolean).forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      contains: (name) => classes.has(name),
      toggle: (name, force) => {
        const want = force === undefined ? !classes.has(name) : Boolean(force);
        if (want) classes.add(name); else classes.delete(name);
        return want;
      },
    };
  }

  get dataset() {
    const el = this;
    return new Proxy({}, {
      get: (_target, prop) => {
        if (typeof prop !== "string") return undefined;
        const attr = `data-${dashCase(prop)}`;
        return el._attributes.has(attr) ? el._attributes.get(attr) : undefined;
      },
      set: (_target, prop, value) => {
        el.setAttribute(`data-${dashCase(String(prop))}`, String(value));
        return true;
      },
      deleteProperty: (_target, prop) => {
        el.removeAttribute(`data-${dashCase(String(prop))}`);
        return true;
      },
    });
  }

  setAttribute(name, value) { this._attributes.set(String(name), String(value)); }
  getAttribute(name) { return this._attributes.has(name) ? this._attributes.get(name) : null; }
  hasAttribute(name) { return this._attributes.has(name); }
  removeAttribute(name) { this._attributes.delete(name); }

  get childNodes() { return [...this._childNodes]; }
  // children holds elements only, so text nodes never count against a tile row's length.
  get children() { return this._childNodes.filter((node) => node.nodeType === 1); }
  get lastChild() { return this._childNodes.at(-1) ?? null; }

  append(...nodes) { for (const node of nodes) this.appendChild(node); }

  appendChild(node) {
    const child = typeof node === "string" ? new TextNode(node) : node;
    if (!child || (child.nodeType !== 1 && child.nodeType !== 3)) throw new TypeError(`appendChild: not a node (${String(node)})`);
    detach(child); // moving a node detaches it from its old parent first
    this._childNodes.push(child);
    child._parent = this;
    return child;
  }

  replaceChildren(...nodes) {
    for (const child of this._childNodes) child._parent = null;
    this._childNodes.length = 0;
    this.append(...nodes);
  }

  remove() { detach(this); }

  get textContent() {
    let out = "";
    for (const child of this._childNodes) out += child.textContent;
    return out;
  }
  set textContent(value) {
    const text = String(value);
    this.replaceChildren();
    if (text !== "") this.appendChild(new TextNode(text));
  }

  // The fake stores the string instead of parsing it: nothing under test reads back a tree.
  get innerHTML() { return this._innerHTML; }
  set innerHTML(value) { this._innerHTML = String(value); }

  // board.js reads offsetWidth to restart the step animation; a constant is enough.
  get offsetWidth() { return 0; }

  addEventListener(type, listener, options = {}) {
    const list = this._listeners.get(type) ?? [];
    list.push({ listener, once: Boolean(options.once) });
    this._listeners.set(type, list);
  }

  dispatchEvent(event) {
    const type = event ? event.type : "";
    for (const entry of [...(this._listeners.get(type) ?? [])]) {
      entry.listener.call(this, event);
      if (entry.once) {
        const list = this._listeners.get(type) ?? [];
        const i = list.indexOf(entry);
        if (i >= 0) list.splice(i, 1);
      }
    }
    return !(event && event.defaultPrevented);
  }

  click() { return this.dispatchEvent(new Event("click")); }

  // Simple selectors only: tag, .class, #id, [attr], [attr="v"], freely combined.
  querySelectorAll(selector) {
    const spec = parseSelector(selector);
    const found = [];
    const walk = (node) => {
      for (const child of node._childNodes) {
        if (child.nodeType !== 1) continue;
        if (matchesSpec(child, spec)) found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }

  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
}

function parseSelector(selector) {
  const sel = String(selector).trim();
  const tagMatch = /^[a-z][a-z0-9-]*/i.exec(sel);
  const spec = { tag: tagMatch ? tagMatch[0].toLowerCase() : null, id: null, classes: [], attrs: [] };
  for (const m of sel.matchAll(/\.([a-zA-Z0-9_-]+)/g)) spec.classes.push(m[1]);
  const idMatch = /#([a-zA-Z0-9_-]+)/.exec(sel);
  if (idMatch) spec.id = idMatch[1];
  for (const m of sel.matchAll(/\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]/g)) spec.attrs.push([m[1], m[2] ?? null]);
  return spec;
}

function matchesSpec(el, spec) {
  if (spec.tag && el.tagName.toLowerCase() !== spec.tag) return false;
  if (spec.id != null && el.id !== spec.id) return false;
  for (const name of spec.classes) if (!el.classList.contains(name)) return false;
  for (const [name, value] of spec.attrs) {
    if (!el.hasAttribute(name)) return false;
    if (value != null && el.getAttribute(name) !== value) return false;
  }
  return true;
}

function makeDocument() {
  const documentElement = new Element("html");
  const body = new Element("body");
  documentElement.appendChild(body);
  return {
    nodeType: 9,
    documentElement,
    body,
    createElement: (tag) => new Element(tag),
    createTextNode: (text) => new TextNode(text),
    createElementNS: (namespace, tag) => new Element(tag, namespace),
    querySelectorAll: (selector) => documentElement.querySelectorAll(selector),
    querySelector: (selector) => documentElement.querySelector(selector),
  };
}

// board.js and ui.js only read .matches; setMedia flips it for the reduced-motion paths.
const mediaMatches = new Map(); // query -> matches

/** Flip a media query for tests, e.g. setMedia("(prefers-reduced-motion: reduce)", true). */
export function setMedia(query, matches) {
  mediaMatches.set(String(query), Boolean(matches));
}

function matchMedia(query) {
  return { media: String(query), get matches() { return mediaMatches.get(String(query)) ?? false; } };
}

let current = null;

/** Put the document and matchMedia on globalThis next to env.mjs's fakes. */
export function installDom() {
  const doc = makeDocument();
  Object.defineProperty(globalThis, "document", { value: doc, writable: true, configurable: true });
  Object.defineProperty(globalThis, "matchMedia", { value: matchMedia, writable: true, configurable: true });
  current = doc;
  return doc;
}

/** Empty the body, clear <html lang> and the media queries; call between tests. */
export function resetDom() {
  if (!current) installDom();
  current.body.replaceChildren();
  current.documentElement.lang = "";
  mediaMatches.clear();
}

// Like env.mjs: importing the file is enough before an app module loads, because static
// imports evaluate in order and the app modules read document at their own import.
installDom();
