'use strict';
const { mountBox, broadcast, turn, equal, ok, includes } = require('./harness');

function request(fields = {}) {
  return Object.assign({
    id: 'input-1', blocking: true, expires_at: '2026-11-02T00:00:00Z',
    questions: [{ id: 'q-1', header: 'Color', question: 'Which color?',
      options: [{ id: 'o-1', label: 'Blue', description: 'The sky' },
        { id: 'o-2', label: 'Green', description: 'The forest' }],
      multi_select: false, allow_text: true, secret: false }],
  }, fields);
}
function setup(req = request()) {
  const rig = mountBox();
  rig.socket.deliver(broadcast(1, { type: 'input_request', request: req, turn: turn('t-1') }));
  rig.form = rig.box.inputForms[req.id];
  return rig;
}
function submit(form) { form.el.dispatch('submit'); }

test('input: single-choice form preserves labels and sends an answer without a turn', () => {
  const { form, socket, box } = setup();
  includes(form.el.textContent, 'Which color?', 'question');
  includes(form.el.textContent, 'The sky', 'description');
  includes(form.el.textContent, 'Color', 'header');
  equal(form.fields[0].choices[0].type, 'radio', 'single selection');
  ok(!form.fields[0].choices[0].checked, 'no default selection');
  submit(form);
  equal(socket.ofType('answer').length, 0, 'missing answer refused');
  form.fields[0].choices[1].checked = true;
  submit(form);
  equal(socket.ofType('answer'), [{ type: 'answer', request: 'input-1',
    answers: { 'q-1': { choices: ['o-2'] } } }], 'answer wire shape');
  equal(socket.ofType('submit').length, 0, 'no prompt submitted');
  ok(form.submit.disabled, 'double submit disabled');
  submit(form);
  equal(socket.ofType('answer').length, 1, 'no duplicate');
  ok(!box.inputEl.disabled, 'non-owned request alone does not invent an open turn');
});

test('input: multi-question batch allows multiple choices and masks secret text', () => {
  const req = request();
  req.questions[0].multi_select = true;
  req.questions.push({ id: 'q-2', header: 'Token', question: 'Your token?', options: [],
    multi_select: false, allow_text: true, secret: true });
  const { form, socket } = setup(req);
  equal(form.fields[0].choices[0].type, 'checkbox', 'multi selection');
  equal(form.fields[1].text.type, 'password', 'secret masked');
  form.fields[0].choices.forEach(choice => { choice.checked = true; });
  submit(form);
  equal(socket.ofType('answer').length, 0, 'partial batches not sent');
  form.fields[1].text.value = 'my secret';
  submit(form);
  equal(socket.ofType('answer')[0].answers, {
    'q-1': { choices: ['o-1', 'o-2'] }, 'q-2': { text: 'my secret' },
  }, 'entire answer batch');
});

test('input: free text is exclusive with choices and absent when not permitted', () => {
  const { form, socket } = setup();
  const field = form.fields[0];
  field.choices[0].checked = true;
  field.text.value = 'Teal';
  field.text.dispatch('input');
  ok(!field.choices[0].checked && field.other.checked, 'text deselects choices');
  submit(form);
  equal(socket.ofType('answer')[0].answers, { 'q-1': { text: 'Teal' } }, 'text shape');
  const req = request();
  req.questions[0].allow_text = false;
  const other = setup(req).form;
  equal(other.fields[0].text, null, 'no disallowed text field');
});

test('input: dismissal sends no answer selection', () => {
  const { form, socket } = setup();
  form.dismiss.dispatch('click');
  equal(socket.ofType('answer')[0], { type: 'answer', request: 'input-1', dismiss: true }, 'dismiss');
});

test('input: acceptance disables other tabs until resolution', () => {
  const { form, socket, box } = setup();
  socket.deliver(broadcast(2, { type: 'input_answering', request: 'input-1' }));
  ok(form.submit.disabled && form.dismiss.disabled, 'both actions disabled');
  ok(form.controls.every(control => control.disabled), 'all fields disabled');
  socket.deliver(broadcast(3, { type: 'input_resolved', request: 'input-1', reason: 'answered',
    turn: turn('t-1') }));
  equal(Object.keys(box.inputForms), [], 'form removed');
  equal(box.turnOrder.length, 0, 'input lifecycle does not create a turn view');
});

test('input: status preserves drafts and restores live forms after EOT', () => {
  const req = request({ state: 'pending', turn: turn('t-1'), blocking: false });
  const { box, socket } = mountBox({ state: { input_requests: [req] } });
  const form = box.inputForms[req.id];
  form.fields[0].text.value = 'draft';
  socket.deliver({ type: 'status', seq: 0, input_requests: [req], turn_ids: [], tasks: [] });
  ok(box.inputForms[req.id] === form, 'same form object');
  equal(form.fields[0].text.value, 'draft', 'draft preserved');
  socket.deliver({ type: 'status', seq: 0, input_requests: [], turn_ids: [], tasks: [] });
  equal(Object.keys(box.inputForms), [], 'authoritative status removes stale form');
});

test('input: historical requests never become actionable after reconnect', () => {
  const { box } = mountBox({ transcript: [{ id: 't-1', kind: 'user', user: 'hello', events: [
    { type: 'input_request', request: request(), turn: turn('t-1') },
    { type: 'turn_end', turn: turn('t-1'), outcome: 'success' },
  ] }], state: { input_requests: [] } });
  equal(Object.keys(box.inputForms), [], 'no historic live forms');
  equal(Object.keys(box.openTurns), [], 'turn stays ended');
});

test('input: disconnect and reattach disable stale forms and never retry an answer', () => {
  const { box, form, socket, clock } = setup();
  socket.drop();
  ok(form.submit.disabled && form.controls[0].disabled, 'offline disabled');
  form.dismiss.dispatch('click');
  equal(socket.ofType('answer').length, 0, 'no offline answer');
  clock.advance(500);
  const next = box.socket;
  next.open();
  ok(form.submit.disabled, 'still disabled before attached');
  next.deliver({ type: 'attached', session: 's1', seq: 0, turns: [], input_requests: [], tasks: [] });
  equal(Object.keys(box.inputForms), [], 'stale form discarded');
  equal(next.ofType('answer').length, 0, 'no answer retried');
});

test('input: validation errors unlock only the affected pending form', () => {
  const { box, form, socket } = setup();
  form.fields[0].choices[0].checked = true;
  submit(form);
  socket.deliver({ type: 'error', scope: 'connection', request: 'input-1', error: 'Invalid choice' });
  ok(!form.submit.disabled, 'retry enabled');
  includes(form.error.textContent, 'Invalid choice', 'inline error');
  ok(box.inputForms['input-1'] === form, 'draft retained');
});

test('input: process exit removes questions and arbitrary labels stay plain text', () => {
  const req = request();
  req.questions[0].options[0].label = '<img src=x onerror=alert(1)>';
  const { box, socket, form } = setup(req);
  const label = form.el.querySelector('.r2d2-question-option-label');
  equal(label.textContent, req.questions[0].options[0].label, 'literal label');
  equal(label.innerHTML, '', 'no HTML rendering');
  socket.deliver(broadcast(2, { type: 'process_exited', returncode: 1 }));
  equal(Object.keys(box.inputForms), [], 'exit clears forms');
});

test('input: arbitrary backend question ids are preserved as answer keys', () => {
  const req = request();
  req.questions[0].id = '__proto__';
  const { form, socket } = setup(req);
  form.fields[0].choices[0].checked = true;
  submit(form);
  const answers = socket.ofType('answer')[0].answers;
  ok(Object.prototype.hasOwnProperty.call(answers, '__proto__'), 'question id is an own key');
  equal(answers.__proto__, { choices: ['o-1'] }, 'answer preserved');
});
