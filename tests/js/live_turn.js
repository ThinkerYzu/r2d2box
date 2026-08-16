// Drive one real turn through the real chat box, and print what it drew.
//
//     node tests/js/live_turn.js ws://127.0.0.1:8790/chat demo "Reply with: PONG"
//
// The first argument is the endpoint the router was mounted under, exactly as
// a host page passes it — the box appends `/ws` itself.
//
// Everything is real except the DOM: a real WebSocket to a running r2d2box,
// a real agent-proxy behind it, and `r2d2box.js` itself doing the attaching,
// the reconciling and the rendering. `tests/test_live_chat_box.py` runs it
// against a server it starts, and reads the JSON this prints on the last line.
//
// The point is the half the unit tests cannot reach: that the box's own
// protocol assumptions survive contact with the server that actually
// implements them.

'use strict';

const path = require('path');
const { Document } = require('./minidom');

const R2D2Box = require(path.join(__dirname, '..', '..', 'src', 'r2d2box', 'static', 'r2d2box.js'));

const [url, topic, prompt] = process.argv.slice(2);
const TIMEOUT_MS = Number(process.env.R2D2BOX_LIVE_TIMEOUT_MS || 240000);

const doc = new Document();
// Real timers, not the fake clock the unit tests drive: this waits out a real
// model answering a real question.
doc.defaultView.setTimeout = setTimeout;
doc.defaultView.clearTimeout = clearTimeout;
doc.defaultView.WebSocket = WebSocket;

const element = doc.createElement('div');
doc.body.appendChild(element);

const box = R2D2Box.mount(element, { endpoint: url, topic, session: null });

let ended = false;
box.on('turn_end', () => { ended = true; });
box.on('attached', () => {
  // One submit, once there is a session to submit into.
  if (box.el.querySelector('.r2d2-input').value === '') {
    box.el.querySelector('.r2d2-input').value = prompt;
    box.submit();
  }
});

const deadline = Date.now() + TIMEOUT_MS;
const poll = setInterval(() => {
  if (!ended && Date.now() < deadline) return;
  clearInterval(poll);
  // With no `marked` or DOMPurify loaded here, the box takes its plain-text
  // path (DESIGN Decision 10) and the answer is text rather than markup.
  const answers = element
    .querySelectorAll('.r2d2-markdown')
    .map((el) => el.innerHTML || el.textContent);
  console.log(JSON.stringify({
    ended,
    session: box.session,
    questions: element.querySelectorAll('.r2d2-message-user .r2d2-content').map((el) => el.textContent),
    answers,
    tools: element.querySelectorAll('.r2d2-tool-name').map((el) => el.textContent),
    notes: element.querySelectorAll('.r2d2-note').map((el) => el.textContent),
    composerEnabled: !element.querySelector('.r2d2-input').disabled,
  }));
  box.destroy();
  process.exit(ended ? 0 : 1);
}, 250);
