// A DOM small enough to run the chat box in, and nothing more.
//
// This is the browser layer's seam, and the fourth in the same family as
// `scripted_proxy.py`, `fake_proxy.py` and `asgi_client.py`: a stand-in for
// the one thing below the code under test, so the suite needs nothing
// installed. `node tests/js/run.js` runs the whole front-end with no browser,
// no jsdom, and no network.
//
// It implements what `r2d2box.js` actually touches — element trees, classes,
// text, attributes, click handlers, and the scroll numbers the bottom-anchor
// reads — and refuses to guess at anything else. Two deliberate limits:
//
//   * `innerHTML` is stored, never parsed. The box writes it in exactly one
//     place, sanitized (DESIGN Decision 10), and never reads structure back
//     out of it, so a test asserts on the string that was written.
//   * `querySelector` takes simple selectors only: a tag, a `.class`, or the
//     two joined, optionally separated by spaces for descendants.
//
// A test that needs more than this needs a real browser, and belongs in the
// live tier with the rest of the things that do.

'use strict';

/** The class attribute as a list, backed by the element's `className` string. */
class ClassList {
  constructor(element) {
    this.element = element;
  }

  _parts() {
    return this.element.className.split(/\s+/).filter(Boolean);
  }

  _write(parts) {
    this.element.className = parts.join(' ');
  }

  contains(name) {
    return this._parts().indexOf(name) >= 0;
  }

  add(name) {
    if (!this.contains(name)) this._write(this._parts().concat(name));
  }

  remove(name) {
    this._write(this._parts().filter((part) => part !== name));
  }

  toggle(name) {
    if (this.contains(name)) this.remove(name);
    else this.add(name);
  }
}

/** One node in the tree: an element, or the text inside one. */
class Node {
  constructor(doc) {
    this.ownerDocument = doc;
    this.parentNode = null;
    this.childNodes = [];
  }

  get children() {
    return this.childNodes.filter((node) => node instanceof Element);
  }

  get firstChild() {
    return this.childNodes[0] || null;
  }

  /**
   * Put `node` at the end, taking it out of wherever it was.
   *
   * Moving rather than copying is what the box relies on when it folds an
   * older tool block into the collapse container — the block keeps its
   * handlers and its state and simply changes parents.
   */
  appendChild(node) {
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  insertBefore(node, reference) {
    if (reference == null) return this.appendChild(node);
    const at = this.childNodes.indexOf(reference);
    if (at < 0) return this.appendChild(node);
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.childNodes.splice(at, 0, node);
    return node;
  }

  removeChild(node) {
    const at = this.childNodes.indexOf(node);
    if (at >= 0) this.childNodes.splice(at, 1);
    node.parentNode = null;
    return node;
  }
}

/** A run of text inside an element. */
class TextNode extends Node {
  constructor(doc, text) {
    super(doc);
    this.nodeType = 3;
    this.data = String(text);
  }

  get textContent() {
    return this.data;
  }
}

class Element extends Node {
  constructor(doc, tagName) {
    super(doc);
    this.nodeType = 1;
    this.tagName = tagName.toUpperCase();
    this.className = '';
    this.classList = new ClassList(this);
    this.attributes = {};
    this.style = {};
    this.listeners = {};
    this.value = '';
    this.disabled = false;
    this._innerHTML = '';
    // What `withScrollAnchor` reads. A test sets them to say where the reader
    // is; left alone they read as a list short enough to need no scrolling,
    // which counts as being at the bottom.
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
  }

  // Text set through `innerHTML` is not part of this: it was written as
  // markup, and a test asking what the box rendered should read `innerHTML`
  // and see the markup it wrote.
  get textContent() {
    return this.childNodes.map((node) => node.textContent).join('');
  }

  set textContent(text) {
    this.childNodes = [];
    this._innerHTML = '';
    if (text !== '' && text != null) {
      this.appendChild(new TextNode(this.ownerDocument, text));
    }
  }

  get innerHTML() {
    return this._innerHTML;
  }

  set innerHTML(html) {
    this.childNodes = [];
    this._innerHTML = String(html);
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name)
      ? this.attributes[name]
      : null;
  }

  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }

  removeEventListener(type, handler) {
    const list = this.listeners[type] || [];
    const at = list.indexOf(handler);
    if (at >= 0) list.splice(at, 1);
  }

  /** Fire one event on this element, the way a person's click or key would. */
  dispatch(type, event) {
    const full = Object.assign({ type, target: this, preventDefault() {} }, event);
    for (const handler of (this.listeners[type] || []).slice()) handler(full);
  }

  matches(selector) {
    for (const part of selector.split(/(?=[.#])/)) {
      if (part.startsWith('.')) {
        if (!this.classList.contains(part.slice(1))) return false;
      } else if (part && this.tagName !== part.toUpperCase()) {
        return false;
      }
    }
    return true;
  }

  querySelectorAll(selector) {
    const steps = selector.trim().split(/\s+/);
    let matched = [this];
    for (const step of steps) {
      const next = [];
      for (const element of matched) {
        for (const descendant of descendantsOf(element)) {
          if (descendant.matches(step)) next.push(descendant);
        }
      }
      matched = next;
    }
    return matched;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

/** Every element under `element`, in document order. */
function descendantsOf(element) {
  const found = [];
  for (const child of element.children) {
    found.push(child);
    found.push(...descendantsOf(child));
  }
  return found;
}

/**
 * A document with one `<body>`, and the window a mounted box reads its globals from.
 *
 * The box looks up `WebSocket`, `marked`, `DOMPurify` and the timers through
 * `document.defaultView` rather than as bare identifiers, so everything a test
 * wants to control is a property set here.
 */
class Document {
  constructor() {
    this.defaultView = {
      location: { protocol: 'http:', host: 'localhost:8790' },
      console,
      setTimeout: (fn, ms) => this.clock.set(fn, ms),
      clearTimeout: (handle) => this.clock.clear(handle),
    };
    this.clock = new Clock();
    this.body = new Element(this, 'body');
  }

  createElement(tagName) {
    return new Element(this, tagName);
  }
}

/**
 * Timers that only move when a test says so.
 *
 * The box's reconnect backoff and its status poll are measured in seconds; a
 * suite that waited them out would take minutes to prove something that is
 * really about ordering. `advance` runs whatever is due, in the order it was
 * scheduled.
 */
class Clock {
  constructor() {
    this.now = 0;
    this.pending = new Map();
    this.nextHandle = 1;
  }

  set(fn, ms) {
    const handle = this.nextHandle++;
    this.pending.set(handle, { at: this.now + (ms || 0), fn, order: handle });
    return handle;
  }

  clear(handle) {
    this.pending.delete(handle);
  }

  advance(ms) {
    const until = this.now + ms;
    while (true) {
      const due = [...this.pending.entries()]
        .filter(([, timer]) => timer.at <= until)
        .sort((a, b) => a[1].at - b[1].at || a[1].order - b[1].order);
      if (!due.length) break;
      const [handle, timer] = due[0];
      this.pending.delete(handle);
      this.now = timer.at;
      timer.fn();
    }
    this.now = until;
  }
}

module.exports = { Document, Element, TextNode, Clock };
