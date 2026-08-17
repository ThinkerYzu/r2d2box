// The chat box, driven through the DOM the way a person and a server drive it.
//
// Every test here mounts a real `r2d2box.js` on `tests/js/minidom.js` and a
// fake socket, so what is exercised is the shipped file rather than a copy of
// its logic. The rules being pinned are the four in that file's header — the
// server owns the composer, `attached` replaces rather than merges, a message
// with no `seq` belongs to the connection, and markdown is sanitized — plus
// the rendering itself.

'use strict';

const {
  mountBox, fakeMarkdown, broadcast, turn, ok, equal, includes, textsOf, FakeSocket,
} = require('./harness');

// ---- mounting ---------------------------------------------------------------

test('mount lays out a message list and a composer inside the host element', () => {
  const { element } = mountBox();

  ok(element.classList.contains('r2d2-box'), 'the mount element is the box root');
  ok(element.querySelector('.r2d2-messages'), 'there is a message list');
  ok(element.querySelector('.r2d2-input'), 'there is an input');
  ok(element.querySelector('.r2d2-send'), 'there is a send button');
});

test('mounting attaches to the topic and session it was given', () => {
  const { socket } = mountBox({ attached: false });

  equal(socket.lastSent, { type: 'attach', topic: 'bug-1992198', session: 's1' },
    'the attach names both keys');
});

test('a box with no session asks for one instead of naming it', () => {
  const { socket, box } = mountBox({ session: null, attached: false });

  equal(socket.lastSent, { type: 'attach', topic: 'bug-1992198' },
    'no session is named');

  socket.deliver({
    type: 'attached', topic: 'bug-1992198', session: 'freshly-made', seq: 0,
    turns: [], turn_active: false, task_ids: [], process_alive: true,
  });
  equal(box.session, 'freshly-made', 'the name comes back in the answer');
});

// ---- one turn ---------------------------------------------------------------

test('a turn draws the question, the answer, and the tools in between', () => {
  const { socket, element } = mountBox(fakeMarkdown());

  socket.deliver(broadcast(1, { type: 'turn_prompt', turn: turn('t-1'), text: 'why does it crash?' }));
  socket.deliver(broadcast(2, { type: 'turn_start', turn: turn('t-1') }));
  socket.deliver(broadcast(3, { type: 'text', turn: turn('t-1'), text: 'Looking at it.' }));
  socket.deliver(broadcast(4, {
    type: 'tool_use', turn: turn('t-1'), tool: 'Read',
    input: { file_path: '/etc/hostname' }, tool_use_id: 'tu-1',
  }));
  socket.deliver(broadcast(5, {
    type: 'tool_result', turn: turn('t-1'), tool_use_id: 'tu-1',
    content: 'r2d2', is_error: false,
  }));
  socket.deliver(broadcast(6, {
    type: 'turn_end', turn: turn('t-1'), basis: 'marker:turn_duration', outcome: 'success',
  }));

  equal(textsOf(element, '.r2d2-message-user .r2d2-content'), ['why does it crash?'],
    'the question is on screen');
  includes(element.querySelector('.r2d2-markdown').innerHTML, 'Looking at it.',
    'the answer is rendered');
  equal(textsOf(element, '.r2d2-tool-name'), ['Read'], 'the tool call is shown by name');
  equal(textsOf(element, '.r2d2-tool-result'), ['r2d2'], 'its result is inside the block');
  ok(element.querySelector('.r2d2-tool').classList.contains('r2d2-tool-ok'),
    'a tool that succeeded says so');
});

test('a failed tool is marked, and its result still lands in its own block', () => {
  const { socket, element } = mountBox(fakeMarkdown());

  socket.deliver(broadcast(1, {
    type: 'tool_use', turn: turn('t-1'), tool: 'Bash',
    input: { command: 'false' }, tool_use_id: 'tu-1',
  }));
  socket.deliver(broadcast(2, {
    type: 'tool_result', turn: turn('t-1'), tool_use_id: 'tu-1',
    content: [{ type: 'text', text: 'exit 1' }], is_error: true,
  }));

  const block = element.querySelector('.r2d2-tool');
  ok(block.classList.contains('r2d2-tool-error'), 'the block is marked failed');
  equal(textsOf(element, '.r2d2-tool-result'), ['exit 1'], 'a content list is flattened to text');
});

test('a tool_result that names no turn lands in the turn that is running', () => {
  const { socket, element } = mountBox(fakeMarkdown());

  socket.deliver(broadcast(1, { type: 'turn_start', turn: turn('t-1') }));
  socket.deliver(broadcast(2, {
    type: 'tool_use', turn: turn('t-1'), tool: 'Read', input: {}, tool_use_id: 'tu-1',
  }));
  socket.deliver(broadcast(3, { type: 'tool_result', tool_use_id: 'tu-1', content: 'found it' }));

  equal(textsOf(element, '.r2d2-tool-result'), ['found it'],
    'the bare result found its call anyway');
});

test('an unowned turn is drawn as a background turn, not as a reply', () => {
  const { socket, element } = mountBox(fakeMarkdown());

  socket.deliver(broadcast(1, { type: 'turn_start', turn: turn('t-bg', 'unowned') }));
  socket.deliver(broadcast(2, { type: 'text', turn: turn('t-bg', 'unowned'), text: 'the build finished' }));

  ok(element.querySelector('.r2d2-message-background'), 'it has its own style');
  equal(textsOf(element, '.r2d2-message-background .r2d2-role'), ['Background task'],
    'and its own label');
});

test('older tool and thinking blocks fold away once there are too many', () => {
  const { socket, element } = mountBox(fakeMarkdown());

  for (let i = 1; i <= 5; i++) {
    socket.deliver(broadcast(i, {
      type: 'tool_use', turn: turn('t-1'), tool: 'Read', input: {}, tool_use_id: 'tu-' + i,
    }));
  }

  const fold = element.querySelector('.r2d2-fold');
  ok(fold, 'a fold container appeared');
  equal(fold.querySelectorAll('.r2d2-tool').length, 2, 'the two oldest are inside it');
  includes(fold.firstChild.textContent, '2 older items', 'the toggle says how many');

  fold.firstChild.dispatch('click');
  ok(fold.classList.contains('r2d2-expanded'), 'clicking the toggle opens it');
  includes(fold.firstChild.textContent, 'Hide 2 older items', 'and the label follows');
});

// ---- the composer, whose state is the session's -----------------------------

test('the composer is blocked while a turn runs and freed when it ends', () => {
  const { socket, element } = mountBox();
  const input = element.querySelector('.r2d2-input');

  socket.deliver(broadcast(1, { type: 'turn_start', turn: turn('t-1') }));
  ok(input.disabled, 'a running turn blocks the input');
  equal(textsOf(element, '.r2d2-status-text'), ['Working'], 'and says why');

  socket.deliver(broadcast(2, { type: 'turn_end', turn: turn('t-1'), outcome: 'success' }));
  ok(!input.disabled, 'the end of the turn frees it');
  equal(element.querySelector('.r2d2-status'), null, 'and takes the indicator away');
});

test('a turn another tab started blocks this tab too', () => {
  const { socket, element } = mountBox();

  // Nothing was typed here: this is what fan-out looks like from the tab that
  // is only watching.
  socket.deliver(broadcast(1, { type: 'turn_prompt', turn: turn('t-1'), text: 'from the other tab' }));
  socket.deliver(broadcast(2, { type: 'turn_start', turn: turn('t-1') }));

  ok(element.querySelector('.r2d2-input').disabled, 'the input is blocked');
  equal(textsOf(element, '.r2d2-message-user .r2d2-content'), ['from the other tab'],
    'and the question it never typed is on screen');
});

test('an outstanding background task keeps the composer blocked after the turn ends', () => {
  const { socket, element } = mountBox();
  const input = element.querySelector('.r2d2-input');

  socket.deliver(broadcast(1, { type: 'turn_start', turn: turn('t-1') }));
  socket.deliver(broadcast(2, { type: 'task_start', turn: turn('t-1'), task: { id: 'bash_3' } }));
  socket.deliver(broadcast(3, { type: 'turn_end', turn: turn('t-1'), outcome: 'success' }));

  ok(input.disabled, 'the task is still running, so the input stays blocked');
  equal(textsOf(element, '.r2d2-status-text'), ['Waiting for background tasks'], 'and says so');

  socket.deliver(broadcast(4, { type: 'task_end', task: { id: 'bash_3' }, exit_code: 0 }));
  ok(!input.disabled, 'the last task finishing frees it');
});

test('the task set comes from the server and is replaced, never accumulated', () => {
  // a task that finished while this client was away must
  // not leave the input blocked until a reload.
  const { socket, element } = mountBox();
  socket.deliver(broadcast(1, { type: 'task_start', task: { id: 'bash_3' } }));
  ok(element.querySelector('.r2d2-input').disabled, 'blocked by the task it saw start');

  socket.deliver({
    type: 'status', topic: 'bug-1992198', session: 's1', seq: 1,
    turn_active: false, turn_ids: [], task_ids: [], process_alive: true,
  });

  ok(!element.querySelector('.r2d2-input').disabled,
    'the server says nothing is outstanding, so nothing is');
});

test('submitting sends the text and clears the input without drawing anything', () => {
  const { socket, element, box } = mountBox();
  const input = element.querySelector('.r2d2-input');
  box.setContext({ file: 'DESIGN.md', startLine: 12 });

  input.value = '  why does it crash?  ';
  element.querySelector('.r2d2-send').dispatch('click');

  equal(socket.lastSent, {
    type: 'submit', text: 'why does it crash?', context: { file: 'DESIGN.md', startLine: 12 },
  }, 'the trimmed text and the context go out together');
  equal(input.value, '', 'the input is cleared');
  equal(element.querySelectorAll('.r2d2-message-user').length, 0,
    'nothing is drawn until the session broadcasts the prompt');
});

test('Enter sends and Shift+Enter does not', () => {
  const { socket, element } = mountBox();
  const input = element.querySelector('.r2d2-input');

  input.value = 'first';
  input.dispatch('keydown', { key: 'Enter', shiftKey: true });
  equal(socket.ofType('submit').length, 0, 'Shift+Enter is a newline');

  input.dispatch('keydown', { key: 'Enter' });
  equal(socket.ofType('submit').length, 1, 'Enter sends');
});

test('the context badge shows what will ride along, and clears with the submit', () => {
  const { element, box } = mountBox();

  box.setContext({ file: 'src/widget/DESIGN.md', startLine: 12, endLine: 20 });
  equal(textsOf(element, '.r2d2-context-text'), ['DESIGN.md:12-20'], 'the badge names the selection');

  element.querySelector('.r2d2-input').value = 'what is this?';
  element.querySelector('.r2d2-send').dispatch('click');
  equal(box.context, null, 'sending consumes the context');
  equal(element.querySelector('.r2d2-context-text'), null, 'and the badge goes');
});

// ---- attaching, resyncing, and the numbers that decide -----------------------

test('an attached message replaces the screen rather than adding to it', () => {
  const { socket, element } = mountBox(Object.assign(fakeMarkdown(), {
    transcript: [{ id: 't-1', kind: 'user', user: 'the first question', events: [] }],
  }));
  equal(textsOf(element, '.r2d2-message-user .r2d2-content'), ['the first question'],
    'the stored turn is drawn');

  socket.deliver({
    type: 'attached', topic: 'bug-1992198', session: 's1', seq: 0,
    turns: [{ id: 't-9', kind: 'user', user: 'a different conversation', events: [] }],
    turn_active: false, task_ids: [], process_alive: true,
  });

  equal(textsOf(element, '.r2d2-message-user .r2d2-content'), ['a different conversation'],
    'the second attach replaced the first, it did not merge into it');
});

test('a stored turn replays through the same rendering as a live one', () => {
  const { element } = mountBox(Object.assign(fakeMarkdown(), {
    transcript: [{
      id: 't-1', kind: 'user', user: 'why does it crash?', outcome: 'success',
      events: [
        broadcast(1, { type: 'turn_start', turn: turn('t-1') }),
        broadcast(2, { type: 'text', turn: turn('t-1'), text: 'Because of the resize.' }),
        broadcast(3, {
          type: 'tool_use', turn: turn('t-1'), tool: 'Grep', input: { pattern: 'resize' },
          tool_use_id: 'tu-1',
        }),
        broadcast(4, { type: 'tool_result', turn: turn('t-1'), tool_use_id: 'tu-1', content: 'one hit' }),
        broadcast(5, { type: 'turn_end', turn: turn('t-1'), outcome: 'success' }),
      ],
    }],
  }));

  equal(textsOf(element, '.r2d2-message-user .r2d2-content'), ['why does it crash?'], 'the question');
  includes(element.querySelector('.r2d2-markdown').innerHTML, 'Because of the resize.', 'the answer');
  equal(textsOf(element, '.r2d2-tool-result'), ['one hit'], 'and the tool call it made');
});

test('a transcript that repeats a turn id draws both turns, not one', () => {
  // What a conversation stored before turn ids were the session's own looks
  // like: agent-proxy numbered turns per process, so the turn after a respawn
  // is `t-1` again. Reusing the first turn's view would drop the second
  // question entirely and run both answers together.
  const stored = (user, answer) => ({
    id: 't-1', kind: 'user', user, outcome: 'success',
    events: [
      { type: 'turn_start', turn: turn('t-1') },
      { type: 'text', turn: turn('t-1'), text: answer },
      { type: 'turn_end', turn: turn('t-1'), outcome: 'success' },
    ],
  });
  const { element } = mountBox(Object.assign(fakeMarkdown(), {
    transcript: [stored('before the restart', 'first answer'),
                 stored('after the restart', 'second answer')],
  }));

  equal(textsOf(element, '.r2d2-message-user .r2d2-content'),
    ['before the restart', 'after the restart'], 'both questions are on screen');
  equal(element.querySelectorAll('.r2d2-markdown').map((el) => el.innerHTML),
    ['<p>first answer</p>', '<p>second answer</p>'],
    'and each answer is under its own question, not run together in one block');
});

test('a live turn reusing an ended turn id draws its own question', () => {
  const { socket, element } = mountBox(fakeMarkdown());
  const play = (from, question, answer) => {
    socket.deliver(broadcast(from, { type: 'turn_prompt', turn: turn('t-1'), text: question }));
    socket.deliver(broadcast(from + 1, { type: 'turn_start', turn: turn('t-1') }));
    socket.deliver(broadcast(from + 2, { type: 'text', turn: turn('t-1'), text: answer }));
    socket.deliver(broadcast(from + 3, { type: 'turn_end', turn: turn('t-1'), outcome: 'success' }));
  };

  play(1, 'before the restart', 'first answer');
  socket.deliver(broadcast(5, { type: 'process_exited', code: 0 }));
  play(6, 'after the restart', 'second answer');

  equal(textsOf(element, '.r2d2-message-user .r2d2-content'),
    ['before the restart', 'after the restart'], 'the second question was drawn too');
});

test('a turn still running when the client attaches leaves the composer blocked', () => {
  const { element } = mountBox({
    transcript: [{
      id: 't-1', kind: 'user', user: 'a long one', events: [
        broadcast(1, { type: 'turn_start', turn: turn('t-1') }),
      ],
    }],
    state: { turn_active: true, process_alive: true },
  });

  ok(element.querySelector('.r2d2-input').disabled,
    'the transcript shows a turn with no end, so it is still running');
});

test('a gap in seq makes the box ask for the conversation again', () => {
  const { socket } = mountBox();

  socket.deliver(broadcast(1, { type: 'turn_start', turn: turn('t-1') }));
  socket.deliver(broadcast(5, { type: 'text', turn: turn('t-1'), text: 'a message that lost three' }));

  equal(socket.ofType('attach').length, 2, 'it re-attached');
  socket.deliver(broadcast(7, { type: 'text', turn: turn('t-1'), text: 'still nothing' }));
  equal(socket.ofType('attach').length, 2, 'and does not storm while the answer is on its way');
});

test('a status ahead of what we have read is also a lost message', () => {
  const { socket } = mountBox();
  socket.deliver(broadcast(1, { type: 'turn_start', turn: turn('t-1') }));

  socket.deliver({
    type: 'status', topic: 'bug-1992198', session: 's1', seq: 4,
    turn_active: true, turn_ids: ['t-1'], task_ids: [], process_alive: true,
  });

  equal(socket.ofType('attach').length, 2,
    'the session has broadcast three messages this client never saw');
});

test('a connection-scoped error is shown but never joins the conversation', () => {
  const { socket, element } = mountBox();

  socket.deliver({
    type: 'error', scope: 'connection', topic: 'bug-1992198', session: 's1',
    error: 'the prompt was not accepted: session closed',
  });

  equal(textsOf(element, '.r2d2-note'), ['the prompt was not accepted: session closed'],
    'the reader is told');
  equal(element.querySelectorAll('.r2d2-message').length, 0, 'but no turn was invented for it');
});

test('a refused submit gives the typed text back', () => {
  const { socket, element } = mountBox();
  const input = element.querySelector('.r2d2-input');

  input.value = 'what host is this?';
  element.querySelector('.r2d2-send').dispatch('click');
  equal(input.value, '', 'sent, so the box is empty');

  socket.deliver({
    type: 'error', scope: 'connection', topic: 'bug-1992198', session: 's1',
    error: 'the prompt was not accepted',
  });

  equal(input.value, 'what host is this?', 'the question is back where it can be re-sent');
});

// ---- markdown ---------------------------------------------------------------

test('assistant markdown goes through marked and then DOMPurify', () => {
  const rendering = fakeMarkdown();
  const { socket, element } = mountBox(rendering);

  socket.deliver(broadcast(1, {
    type: 'text', turn: turn('t-1'), text: '# hi\n<script>alert(1)</script>',
  }));

  equal(rendering.calls.parsed, ['# hi\n<script>alert(1)</script>'], 'marked saw the source');
  equal(rendering.calls.sanitized.length, 1, 'and DOMPurify saw what marked produced');
  ok(element.querySelector('.r2d2-markdown').innerHTML.indexOf('<script') < 0,
    'what reached the DOM had been sanitized');
});

test('with no marked or DOMPurify the text is shown verbatim, never as HTML', () => {
  // the line most likely to be "simplified" away.
  const { socket, element } = mountBox();

  socket.deliver(broadcast(1, {
    type: 'text', turn: turn('t-1'), text: '**bold** <img src=x onerror=alert(1)>',
  }));

  const content = element.querySelector('.r2d2-markdown');
  equal(content.innerHTML, '', 'nothing was written as markup');
  equal(content.textContent, '**bold** <img src=x onerror=alert(1)>', 'it is plain text');
});

test('successive text messages in one turn are separated rather than run together', () => {
  const rendering = fakeMarkdown();
  const { socket } = mountBox(rendering);

  socket.deliver(broadcast(1, { type: 'text', turn: turn('t-1'), text: 'first' }));
  socket.deliver(broadcast(2, { type: 'text', turn: turn('t-1'), text: 'second' }));

  equal(rendering.calls.parsed[rendering.calls.parsed.length - 1],
    'first\n\n---\n\nsecond', 'the whole turn is re-rendered from its source, with a rule between');
});

// ---- what the host sees -----------------------------------------------------

test('handlers receive the message as delivered, with nothing to unwrap', () => {
  const { socket, box } = mountBox();
  const seen = [];
  box.on('tool_result', (message) => seen.push(message));

  socket.deliver(broadcast(1, {
    type: 'tool_result', turn: turn('t-1'), tool_use_id: 'tu-1',
    tool: 'mcp__notes__worklog_append', content: 'ok', is_error: false,
  }));

  equal(seen.length, 1, 'the handler ran');
  equal(seen[0].tool, 'mcp__notes__worklog_append', 'and read the field straight off it');
});

test('a handler that throws costs its own call and nothing else', () => {
  const { socket, box, element, logged } = mountBox(fakeMarkdown());
  box.on('text', () => { throw new Error('the host broke'); });

  socket.deliver(broadcast(1, { type: 'text', turn: turn('t-1'), text: 'still drawn' }));

  includes(element.querySelector('.r2d2-markdown').innerHTML, 'still drawn',
    'the panel drew the message anyway');
  equal(logged.length, 1, 'and the host was told its handler failed');
});

test('a message type the box has never heard of is passed on, not crashed on', () => {
  const { socket, box, element } = mountBox();
  const seen = [];
  box.on('something_new', (message) => seen.push(message));

  socket.deliver(broadcast(1, { type: 'something_new', turn: turn('t-1'), whatever: 42 }));

  equal(seen.length, 1, 'the host still hears about it');
  ok(element.querySelector('.r2d2-messages'), 'and the panel is intact');
});

// ---- losing things ----------------------------------------------------------

test('a dropped socket reconnects and attaches again', () => {
  const { socket, clock } = mountBox();
  const first = socket;
  first.drop();

  clock.advance(600);
  const second = FakeSocket.latest;
  ok(second !== first, 'a new socket was opened');
  second.open();
  equal(second.lastSent, { type: 'attach', topic: 'bug-1992198', session: 's1' },
    'and it attached again');
});

test('the transcript a reconnect brings back replaces what was on screen', () => {
  const { socket, element, clock } = mountBox(fakeMarkdown());
  socket.deliver(broadcast(1, { type: 'turn_prompt', turn: turn('t-1'), text: 'asked before the drop' }));
  socket.drop();

  clock.advance(600);
  const second = FakeSocket.latest;
  second.open();
  second.deliver({
    type: 'attached', topic: 'bug-1992198', session: 's1', seq: 12,
    turns: [{ id: 't-1', kind: 'user', user: 'asked before the drop', events: [] }],
    turn_active: false, task_ids: [], process_alive: true,
  });

  equal(textsOf(element, '.r2d2-message-user .r2d2-content'), ['asked before the drop'],
    'the question appears once, not twice');
});

test('a blocked composer keeps asking the session what is still running', () => {
  const { socket, clock } = mountBox();
  socket.deliver(broadcast(1, { type: 'turn_start', turn: turn('t-1') }));

  clock.advance(16000);

  equal(socket.ofType('status_query').length, 1, 'it asked once the poll came due');
});

test('an idle composer asks nothing', () => {
  const { socket, clock } = mountBox();

  clock.advance(60000);

  equal(socket.ofType('status_query').length, 0, 'there is nothing to reconcile');
});

test('the session being closed clears the box and starts a new conversation', () => {
  const { socket, element, box } = mountBox(Object.assign(fakeMarkdown(), {
    transcript: [{ id: 't-1', kind: 'user', user: 'a conversation about to end', events: [] }],
  }));

  socket.deliver(broadcast(1, { type: 'session_closed' }));

  equal(element.querySelectorAll('.r2d2-message').length, 0, 'the deleted conversation is gone');
  equal(box.session, null, 'and the box is no longer holding its name');
  equal(socket.lastSent, { type: 'attach', topic: 'bug-1992198' }, 'it asked for a fresh session');
});

test('the process exiting is reported without ending the conversation', () => {
  const { socket, element } = mountBox();

  socket.deliver(broadcast(1, { type: 'process_exited', returncode: 1 }));

  equal(textsOf(element, '.r2d2-note').length, 1, 'the reader is told');
  ok(!element.querySelector('.r2d2-input').disabled, 'and can ask the next question');
});

test('destroy closes the socket, stops the timers, and hands the element back empty', () => {
  const { socket, element, box, clock } = mountBox();
  socket.deliver(broadcast(1, { type: 'turn_start', turn: turn('t-1') }));

  box.destroy();
  clock.advance(60000);

  ok(socket.closed, 'the socket was closed');
  equal(socket.ofType('status_query').length, 0, 'the status poll stopped');
  equal(element.childNodes.length, 0, 'the element is empty');
  ok(!element.classList.contains('r2d2-box'), 'and no longer claims to be a box');
});

test('attach switches conversations without clearing the one still on screen', () => {
  const { socket, element, box } = mountBox(Object.assign(fakeMarkdown(), {
    transcript: [{ id: 't-1', kind: 'user', user: 'the old conversation', events: [] }],
  }));

  box.attach('bug-1992198', 's2');

  equal(socket.lastSent, { type: 'attach', topic: 'bug-1992198', session: 's2' }, 'it asked');
  equal(textsOf(element, '.r2d2-message-user .r2d2-content'), ['the old conversation'],
    'and kept showing the old one until the answer arrives');
});
