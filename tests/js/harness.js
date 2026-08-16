// The rig the chat-box tests are written against: a fake socket, a mounted
// box, and a tiny assertion vocabulary.
//
// `mountBox()` gives a test a box that is already attached to a session with
// an empty transcript, which is where nearly every test wants to start. The
// socket underneath it is `FakeSocket` — nothing opens a port, and a test both
// reads what the box sent and decides what the server says back.

'use strict';

const path = require('path');
const { Document } = require('./minidom');

const BOX_SOURCE = path.join(__dirname, '..', '..', 'src', 'r2d2box', 'static', 'r2d2box.js');
const R2D2Box = require(BOX_SOURCE);

/**
 * A WebSocket that keeps what was sent and delivers what a test decides.
 *
 * Constructed by the box itself, so `FakeSocket.latest` is how a test reaches
 * the one its box just opened. `open()` and `deliver()` are the server's side
 * of the conversation.
 */
class FakeSocket {
  constructor(url) {
    FakeSocket.latest = this;
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    this.closed = false;
  }

  send(raw) {
    this.sent.push(JSON.parse(raw));
  }

  close() {
    this.closed = true;
    this.readyState = 3;
  }

  /** Complete the connection, which is what makes the box attach. */
  open() {
    this.readyState = 1;
    if (this.onopen) this.onopen({});
  }

  /** Hand the box one server→client message. */
  deliver(message) {
    if (this.onmessage) this.onmessage({ data: JSON.stringify(message) });
  }

  /** Drop the connection the way a restarted server does. */
  drop() {
    this.readyState = 3;
    if (this.onclose) this.onclose({});
  }

  /** Every command of this type the box has sent, oldest first. */
  ofType(type) {
    return this.sent.filter((command) => command.type === type);
  }

  get lastSent() {
    return this.sent[this.sent.length - 1];
  }
}

/** A `marked`/`DOMPurify` pair that records what it was asked to render. */
function fakeMarkdown() {
  const calls = { parsed: [], sanitized: [] };
  return {
    calls,
    marked: {
      parse(markdown) {
        calls.parsed.push(markdown);
        return '<p>' + markdown + '</p>';
      },
    },
    DOMPurify: {
      sanitize(html) {
        calls.sanitized.push(html);
        return html.replace(/<script[\s\S]*?<\/script>/g, '');
      },
    },
  };
}

/**
 * Mount a box on a fake socket and attach it, and hand back everything a test drives.
 *
 * Returns `{box, socket, doc, element, logged, clock}`. Pass `attached: false`
 * to stop before the server answers — a test about attaching itself wants to
 * see the command go out. Anything else is passed to `R2D2Box.mount`, so a
 * test can supply its own markdown pair or leave it out to exercise the plain
 * text fallback.
 */
function mountBox(options = {}) {
  const { attached = true, transcript = [], state = {}, ...boxOptions } = options;
  const doc = new Document();
  const element = doc.createElement('div');
  doc.body.appendChild(element);
  // The box logs a host handler that threw. Recording it rather than letting
  // it out keeps the runner's output to the tests' own, and lets a test assert
  // that the failure was reported at all.
  const logged = [];
  doc.defaultView.console = { error: (...args) => logged.push(args.join(' ')) };

  doc.defaultView.WebSocket = FakeSocket;
  const box = R2D2Box.mount(element, Object.assign({
    endpoint: '/chat',
    topic: 'bug-1992198',
    session: 's1',
  }, boxOptions));

  const socket = FakeSocket.latest;
  socket.open();
  if (attached) {
    socket.deliver(Object.assign({
      type: 'attached',
      topic: 'bug-1992198',
      session: 's1',
      seq: 0,
      turns: transcript,
      turn_active: false,
      task_ids: [],
      process_alive: false,
    }, state));
  }
  return { box, socket, doc, element, logged, clock: doc.clock };
}

// ---- a turn's worth of messages, for tests that only care about the ends ----

/** One broadcast, with the envelope the router puts on everything it sends. */
function broadcast(seq, message) {
  return Object.assign({ topic: 'bug-1992198', session: 's1', seq }, message);
}

/** The `turn` object every message of one turn carries. */
function turn(id, kind = 'user') {
  return { id, kind };
}

// ---- assertions -------------------------------------------------------------

class AssertionError extends Error {}

function ok(condition, what) {
  if (!condition) throw new AssertionError(what);
}

function equal(actual, expected, what) {
  const same = JSON.stringify(actual) === JSON.stringify(expected);
  if (!same) {
    throw new AssertionError(
      `${what}\n    expected: ${JSON.stringify(expected)}\n    actual:   ${JSON.stringify(actual)}`
    );
  }
}

function includes(haystack, needle, what) {
  if (String(haystack).indexOf(needle) < 0) {
    throw new AssertionError(`${what}\n    ${JSON.stringify(String(haystack))} has no ${JSON.stringify(needle)}`);
  }
}

/** The text of every element matching `selector`, in document order. */
function textsOf(element, selector) {
  return element.querySelectorAll(selector).map((found) => found.textContent);
}

module.exports = {
  R2D2Box,
  FakeSocket,
  AssertionError,
  mountBox,
  fakeMarkdown,
  broadcast,
  turn,
  ok,
  equal,
  includes,
  textsOf,
};
