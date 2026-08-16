// r2d2box — the browser half: one chat box, mounted into a host's element.
//
//     const box = R2D2Box.mount(document.getElementById('chat'), {
//       endpoint: '/chat', topic: 'bug-1992198', session: 's1',
//     });
//
// The box draws the conversation and nothing else. Session pickers, close
// buttons and panel headers are the host's, built on the same router's REST
// endpoints.
//
// Four things here are less obvious than they look, and each is a bug one of
// the two applications this library was extracted from already shipped:
//
// **The server owns the composer's disabled state.** `turn_active` and
// `task_ids` arrive from the session and are replaced, never accumulated. A
// turn another tab started disables this tab's input, and a task that finished
// while the socket was down cannot leave the input stuck.
//
// **An `attached` message is a reset point, not an update.** The transcript in
// it is authoritative and `seq` may have restarted, so it replaces what is on
// screen rather than merging into it.
//
// **A message with no `seq` is about this connection, not the conversation.**
// It is shown and then forgotten: recording it would put one tab's complaint
// into a transcript every tab shares.
//
// **Markdown is sanitized before it reaches innerHTML, always.** `marked` then
// DOMPurify, and plain text if either is missing. It is the one line most
// likely to be "simplified" away, because the renderer this one is modelled on
// did not have it.

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.R2D2Box = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // How many tool and thinking blocks stay visible at the end of a turn.
  // Older ones fold into one collapsed container, so a turn that ran twenty
  // greps does not bury the answer it produced.
  var VISIBLE_BLOCKS = 3;

  // Scroll is anchored to the bottom only while the reader is already there,
  // within this many pixels. Anything more and they have scrolled up to read
  // something, and moving the view under them is the rudest thing a live
  // panel can do.
  var BOTTOM_SLACK_PX = 50;

  // Reconnect backoff, in milliseconds. A dropped socket is usually a server
  // restart or a laptop lid, so the first retry is quick and the ceiling is
  // low enough that a returning browser reattaches without the reader waiting.
  var RECONNECT_MIN_MS = 500;
  var RECONNECT_MAX_MS = 10000;

  // How often a blocked composer asks the session what is still running.
  // Nothing should need this — the stream reports every turn and task — but a
  // message lost to a dropped socket would otherwise leave the input disabled
  // until a reload, which is a bug one of the original implementations shipped
  // twice.
  var STATUS_POLL_MS = 15000;

  // ---- small DOM helpers ----------------------------------------------------

  /** Build one element, with a class and optional text, in `doc`. */
  function make(doc, tag, className, text) {
    var el = doc.createElement(tag);
    if (className) el.className = className;
    if (text != null) el.textContent = text;
    return el;
  }

  /** True when `el` is scrolled to within `BOTTOM_SLACK_PX` of its bottom. */
  function atBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_SLACK_PX;
  }

  /**
   * Run `fn`, then keep the message list pinned to the bottom if it already was.
   *
   * Every insertion that changes the list's height goes through this. The
   * check has to happen before the mutation — afterwards the new content has
   * already moved the numbers it reads.
   */
  function withScrollAnchor(listEl, fn) {
    var pinned = atBottom(listEl);
    fn();
    if (pinned) listEl.scrollTop = listEl.scrollHeight;
  }

  /**
   * A global the page may or may not have loaded, or null.
   *
   * `marked` and DOMPurify are vendored scripts a host includes with its own
   * tags, so either can be absent — a blocked CDN, a typo'd path, a page that
   * chose not to ship them. Looking them up through the mount element's own
   * window rather than a bare identifier is also what lets a test hand the box
   * a stubbed pair.
   */
  function globalIn(doc, name) {
    var view = doc.defaultView;
    if (view && view[name]) return view[name];
    if (typeof globalThis !== 'undefined' && globalThis[name]) return globalThis[name];
    return null;
  }

  // ---- the conversation's own shapes ---------------------------------------

  /**
   * The plain text inside a `tool_result`'s `content`, whatever shape it came in.
   *
   * It is either a string or a list of content blocks, and an unrecognized
   * shape is JSON rather than `[object Object]` — a tool that returns
   * something new should still be readable in the panel.
   */
  function flattenContent(content) {
    if (content == null) return '';
    if (typeof content === 'string') return content;
    if (Array.isArray(content)) {
      var parts = [];
      for (var i = 0; i < content.length; i++) {
        var block = content[i];
        if (typeof block === 'string') parts.push(block);
        else if (block && block.type === 'text' && typeof block.text === 'string') {
          parts.push(block.text);
        } else if (block) {
          try { parts.push(JSON.stringify(block)); } catch (e) { /* skip */ }
        }
      }
      return parts.join('\n');
    }
    try { return JSON.stringify(content); } catch (e) { return String(content); }
  }

  /** A one-line summary of a tool's input for its collapsed header. */
  function summarizeInput(input) {
    if (input == null) return '';
    if (typeof input === 'string') return input;
    var interesting = input.command || input.pattern || input.file_path || input.prompt;
    var text = typeof interesting === 'string' ? interesting : null;
    if (text == null) {
      try { text = JSON.stringify(input); } catch (e) { text = String(input); }
    }
    return text.length > 120 ? text.slice(0, 117) + '...' : text;
  }

  /** The turn id a message names, or null for one that names no turn. */
  function turnIdOf(message) {
    return message && message.turn && typeof message.turn.id === 'string'
      ? message.turn.id
      : null;
  }

  /**
   * The WebSocket URL for a mounted router's `endpoint`.
   *
   * `endpoint` is the prefix the host mounted the router under — `/chat` — so
   * the socket is one path segment below it. An absolute `ws://` or `wss://`
   * endpoint is taken as given, which is what a test or a cross-origin host
   * passes.
   */
  function socketUrl(doc, endpoint) {
    var prefix = String(endpoint == null ? '' : endpoint).replace(/\/+$/, '');
    if (/^wss?:\/\//.test(prefix)) return prefix + '/ws';
    if (prefix && prefix.charAt(0) !== '/') prefix = '/' + prefix;
    var location = doc.defaultView && doc.defaultView.location;
    if (!location) return 'ws://localhost' + prefix + '/ws';
    var scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return scheme + '//' + location.host + prefix + '/ws';
  }

  // ---- one turn on screen ---------------------------------------------------

  /**
   * The elements one turn draws into, created on the first message about it.
   *
   * A turn is the unit the whole panel is organized around: the question, the
   * prose answering it, and the tool and thinking blocks underneath. The box
   * keeps one of these per turn id, so a message can find its turn however
   * long after the last one it arrives — and a transcript replayed at attach
   * builds exactly the same objects as a turn watched live.
   */
  function TurnView(box, id, kind) {
    this.box = box;
    this.id = id;
    this.kind = kind || 'user';
    this.doc = box.doc;
    this.promptEl = null;
    this.rawMarkdown = '';
    this.contentEl = null;
    this.blocks = [];
    this.foldEl = null;
    this.toolBlocks = {};

    this.el = make(this.doc, 'div', 'r2d2-message r2d2-message-assistant');
    if (this.kind !== 'user') this.el.className += ' r2d2-message-background';
    this.el.setAttribute('data-turn', id);
    var label = this.kind === 'user' ? 'Agent' : 'Background task';
    this.el.appendChild(make(this.doc, 'div', 'r2d2-role', label));
  }

  /**
   * Draw the question this turn answers, above the turn's own block.
   *
   * Called twice for the same turn in the ordinary case — once from the
   * `turn_prompt` broadcast and once if the turn is later replayed from the
   * transcript's `user` field — so it does nothing the second time.
   */
  TurnView.prototype.setPrompt = function (text) {
    if (this.promptEl || text == null || !this.el.parentNode) return;
    this.promptEl = make(this.doc, 'div', 'r2d2-message r2d2-message-user');
    this.promptEl.appendChild(make(this.doc, 'div', 'r2d2-role', 'You'));
    this.promptEl.appendChild(make(this.doc, 'div', 'r2d2-content', text));
    this.el.parentNode.insertBefore(this.promptEl, this.el);
  };

  /**
   * Append one `text` message to this turn's prose.
   *
   * The whole turn's Markdown is re-rendered from the accumulated source
   * rather than appended to as HTML, because a fenced code block or a list
   * only parses correctly once its later lines have arrived. Successive `text`
   * messages are separated by a rule: a turn that answers, calls a tool and
   * answers again is three statements, and running them together reads as one
   * wall of prose.
   */
  TurnView.prototype.appendText = function (text) {
    if (!text) return;
    if (!this.contentEl) {
      this.contentEl = make(this.doc, 'div', 'r2d2-content r2d2-markdown');
      this.el.appendChild(this.contentEl);
    }
    this.rawMarkdown += (this.rawMarkdown ? '\n\n---\n\n' : '') + text;
    this.box.renderMarkdownInto(this.contentEl, this.rawMarkdown);
  };

  /**
   * Add one tool or thinking block, folding away whatever it pushed out of sight.
   *
   * Blocks are collapsed by default and expand on a click. Only the last
   * `VISIBLE_BLOCKS` stay in place; the rest move into one collapsed container
   * that sits where the oldest of them was, so the turn keeps its order.
   */
  TurnView.prototype.addBlock = function (block) {
    this.blocks.push(block);
    this.el.appendChild(block);
    if (this.blocks.length <= VISIBLE_BLOCKS) return;

    var stale = this.blocks[this.blocks.length - 1 - VISIBLE_BLOCKS];
    if (!this.foldEl) {
      this.foldEl = make(this.doc, 'div', 'r2d2-fold');
      var toggle = make(this.doc, 'div', 'r2d2-fold-toggle');
      var fold = this.foldEl;
      toggle.addEventListener('click', function () {
        fold.classList.toggle('r2d2-expanded');
        updateFoldToggle(fold);
      });
      this.foldEl.appendChild(toggle);
      this.el.insertBefore(this.foldEl, stale);
    }
    this.foldEl.appendChild(stale);
    updateFoldToggle(this.foldEl);
  };

  /** Relabel a fold container with how many blocks it is currently hiding. */
  function updateFoldToggle(foldEl) {
    var toggle = foldEl.firstChild;
    var hidden = foldEl.children.length - 1;   // the toggle itself is a child
    var noun = hidden === 1 ? 'item' : 'items';
    toggle.textContent = foldEl.classList.contains('r2d2-expanded')
      ? 'Hide ' + hidden + ' older ' + noun
      : '+' + hidden + ' older ' + noun;
  }

  /** Draw a `tool_use`, and remember it so its result can find it. */
  TurnView.prototype.addToolUse = function (message) {
    var block = make(this.doc, 'div', 'r2d2-tool r2d2-tool-running');
    var header = make(this.doc, 'div', 'r2d2-tool-header');
    var status = make(this.doc, 'span', 'r2d2-tool-status', '⋯');
    header.appendChild(status);
    header.appendChild(make(this.doc, 'span', 'r2d2-tool-name', message.tool || 'tool'));
    header.appendChild(
      make(this.doc, 'span', 'r2d2-tool-summary', summarizeInput(message.input))
    );
    header.addEventListener('click', function () {
      block.classList.toggle('r2d2-expanded');
    });
    block.appendChild(header);

    var body = make(this.doc, 'div', 'r2d2-tool-body');
    var input = make(this.doc, 'pre', 'r2d2-tool-input');
    try { input.textContent = JSON.stringify(message.input, null, 2); }
    catch (e) { input.textContent = String(message.input); }
    body.appendChild(input);
    block.appendChild(body);

    block._statusEl = status;
    block._bodyEl = body;
    if (typeof message.tool_use_id === 'string') {
      this.toolBlocks[message.tool_use_id] = block;
    }
    this.addBlock(block);
    return block;
  };

  /**
   * Attach a `tool_result` to the call it answers.
   *
   * Matched by `tool_use_id`, falling back to the most recent tool still
   * waiting: a `tool_result` can arrive with no turn of its own when the
   * transcript runs ahead of agent-proxy's turn accounting, and a result shown
   * against the wrong call is still better than one dropped. With no candidate
   * at all it becomes a note, which is what a client that attached mid-turn
   * sees.
   */
  TurnView.prototype.addToolResult = function (message) {
    var block = this.toolBlocks[message.tool_use_id];
    if (!block) block = this.lastRunningTool();
    if (!block) {
      this.box.appendNote(message.is_error ? 'tool error (no matching call)'
                                           : 'tool result (no matching call)');
      return;
    }
    block.classList.remove('r2d2-tool-running');
    block.classList.add(message.is_error ? 'r2d2-tool-error' : 'r2d2-tool-ok');
    block._statusEl.textContent = message.is_error ? '✗' : '✓';
    var result = make(this.doc, 'pre', 'r2d2-tool-result', flattenContent(message.content));
    block._bodyEl.appendChild(result);
  };

  /** The newest tool block whose result has not arrived, or null. */
  TurnView.prototype.lastRunningTool = function () {
    for (var i = this.blocks.length - 1; i >= 0; i--) {
      if (this.blocks[i].classList.contains('r2d2-tool-running')) return this.blocks[i];
    }
    return null;
  };

  /** Draw a `thinking` message as its own collapsed block. */
  TurnView.prototype.addThinking = function (text) {
    var block = make(this.doc, 'div', 'r2d2-thinking');
    var header = make(this.doc, 'div', 'r2d2-thinking-header', 'Thinking');
    header.addEventListener('click', function () {
      block.classList.toggle('r2d2-expanded');
    });
    block.appendChild(header);
    block.appendChild(make(this.doc, 'div', 'r2d2-thinking-body', text));
    this.addBlock(block);
  };

  /** Mark the turn finished, and say so when it failed. */
  TurnView.prototype.end = function (message) {
    this.el.classList.add('r2d2-turn-ended');
    if (message.outcome === 'error') {
      this.el.classList.add('r2d2-turn-failed');
      var reason = message.error ? ': ' + message.error : '';
      this.el.appendChild(make(this.doc, 'div', 'r2d2-note', 'turn ended in error' + reason));
    }
  };

  // ---- the box --------------------------------------------------------------

  /**
   * One mounted chat box. Built by `R2D2Box.mount`, never with `new` directly.
   *
   * `options` needs `endpoint` (the prefix the host mounted the router under)
   * and `topic`; `session` is optional, and leaving it out asks the server for
   * a new one and reports its name through the `attached` event. `attach:
   * false` builds the box without opening a socket, for a host that wants to
   * choose the conversation first.
   */
  function Box(element, options) {
    this.el = element;
    this.doc = element.ownerDocument;
    this.options = options;
    this.endpoint = options.endpoint || '';
    this.topic = options.topic || null;
    this.session = options.session || null;

    this.SocketImpl = options.WebSocket || globalIn(this.doc, 'WebSocket');
    this.context = null;
    this.handlers = {};
    this.destroyed = false;

    this.socket = null;
    this.reconnectDelay = RECONNECT_MIN_MS;
    this.reconnectTimer = null;
    this.statusTimer = null;
    // A resync is in flight: an `attach` sent because the stream lost
    // messages. It is cleared by the `attached` that answers it, and stops a
    // storm of resyncs while that answer is on its way.
    this.resyncing = false;
    this.lastSeq = null;
    this.pendingText = null;

    this.turns = {};
    this.turnOrder = [];
    this.openTurns = {};
    this.taskIds = {};
    this.processAlive = false;
    this.replayTurn = null;

    this.build();
    if (options.attach !== false && this.topic) this.connect();
  }

  /**
   * Lay out the panel inside the host's element.
   *
   * The element itself becomes the box's root, so a host styles the outside of
   * it and overrides the custom properties on it. Anything
   * already inside is cleared: mounting twice into one element should not
   * leave the first box's messages behind it.
   */
  Box.prototype.build = function () {
    var self = this;
    this.el.className = (this.el.className ? this.el.className + ' ' : '') + 'r2d2-box';
    this.el.textContent = '';

    this.messagesEl = make(this.doc, 'div', 'r2d2-messages');
    this.composerEl = make(this.doc, 'div', 'r2d2-composer');
    this.badgeEl = make(this.doc, 'div', 'r2d2-context-badge');
    this.badgeEl.style.display = 'none';
    this.inputEl = make(this.doc, 'textarea', 'r2d2-input');
    this.inputEl.setAttribute('rows', '3');
    this.inputEl.setAttribute('placeholder', 'Ask the agent…');
    this.sendEl = make(this.doc, 'button', 'r2d2-send', 'Send');
    this.sendEl.setAttribute('type', 'button');

    this.composerEl.appendChild(this.badgeEl);
    this.composerEl.appendChild(this.inputEl);
    this.composerEl.appendChild(this.sendEl);
    this.el.appendChild(this.messagesEl);
    this.el.appendChild(this.composerEl);

    this.sendEl.addEventListener('click', function () { self.submit(); });
    this.inputEl.addEventListener('keydown', function (event) {
      // Enter sends and Shift+Enter breaks the line, which is what both apps
      // this replaces already do. `isComposing` is what keeps an IME's
      // confirming Enter from sending a half-typed sentence.
      if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
      if (event.preventDefault) event.preventDefault();
      self.submit();
    });
    this.refreshComposer();
  };

  // ---- the socket -----------------------------------------------------------

  /**
   * Open the WebSocket and attach as soon as it is up.
   *
   * Safe to call with one already open — it does nothing. A socket that closes
   * reconnects on a backoff until `destroy`, and the `attached` that follows
   * a reconnect replaces the transcript rather than adding to it.
   */
  Box.prototype.connect = function () {
    if (this.destroyed || this.socket || !this.SocketImpl) return;
    var self = this;
    var socket = new this.SocketImpl(socketUrl(this.doc, this.endpoint));
    this.socket = socket;

    socket.onopen = function () {
      self.reconnectDelay = RECONNECT_MIN_MS;
      self.emit('connected', { type: 'connected' });
      self.sendAttach();
    };
    socket.onmessage = function (event) {
      var message;
      try { message = JSON.parse(event.data); }
      catch (e) { return; }
      if (message && typeof message === 'object') self.handle(message);
    };
    socket.onclose = function () {
      if (self.socket !== socket) return;
      self.socket = null;
      self.emit('disconnected', { type: 'disconnected' });
      self.scheduleReconnect();
    };
    socket.onerror = function () { /* onclose does the recovery */ };
  };

  /** Reopen the socket after a growing delay, until `destroy` stops it. */
  Box.prototype.scheduleReconnect = function () {
    if (this.destroyed || this.reconnectTimer) return;
    var self = this;
    var delay = this.reconnectDelay;
    this.reconnectDelay = Math.min(delay * 2, RECONNECT_MAX_MS);
    this.reconnectTimer = this.setTimeout(function () {
      self.reconnectTimer = null;
      self.connect();
    }, delay);
  };

  /** Send one command, or drop it if the socket is not up. */
  Box.prototype.send = function (command) {
    if (!this.socket || this.socket.readyState !== 1) return false;
    this.socket.send(JSON.stringify(command));
    return true;
  };

  /** Ask to be subscribed to this box's topic and session. */
  Box.prototype.sendAttach = function () {
    if (!this.topic) return;
    var command = { type: 'attach', topic: this.topic };
    if (this.session) command.session = this.session;
    this.send(command);
  };

  /**
   * Switch this box to another conversation, in place.
   *
   * The screen is not cleared here: the `attached` that answers does that, so
   * a failed switch leaves the reader looking at the conversation they still
   * have rather than at nothing. Passing no session asks for a new one.
   */
  Box.prototype.attach = function (topic, session) {
    this.topic = topic;
    this.session = session || null;
    if (!this.socket) this.connect();
    else this.sendAttach();
  };

  // ---- incoming messages ----------------------------------------------------

  /**
   * Route one server→client message: reset, reconcile, draw, then tell the host.
   *
   * The `seq` check comes first because it decides whether the rest of the
   * stream can be trusted. A message with no `seq` is this connection's own
   * business — a refused command — and is shown without touching the
   * conversation.
   */
  Box.prototype.handle = function (message) {
    var type = message.type;
    if (type === 'attached') {
      this.resyncing = false;
      this.lastSeq = message.seq;
      this.reset();
      this.session = message.session || this.session;
      this.renderTranscript(message.turns || []);
      this.applyState(message);
    } else if (type === 'status') {
      // Not a broadcast: the `seq` it carries is the last one the session
      // sent, so anything above what we have read means messages were lost.
      if (typeof message.seq === 'number' && this.lastSeq !== null
          && message.seq > this.lastSeq) {
        this.resync();
      } else {
        this.applyState(message);
      }
    } else if (typeof message.seq === 'number' && message.scope !== 'connection') {
      if (this.lastSeq !== null && message.seq > this.lastSeq + 1) {
        this.resync();
      } else {
        this.lastSeq = message.seq;
        this.record(message);
      }
    } else {
      this.showConnectionMessage(message);
    }
    this.emit(type, message);
  };

  /**
   * Ask for the whole conversation again, because this one has a hole in it.
   *
   * A gap in `seq` means the socket dropped messages, and everything drawn
   * after a missing `tool_use` or `turn_end` would be wrong in a way the
   * reader cannot see. Re-attaching is the only honest repair: the `attached`
   * that answers replaces the screen.
   */
  Box.prototype.resync = function () {
    if (this.resyncing) return;
    this.resyncing = true;
    this.sendAttach();
  };

  /**
   * Fold one broadcast into the conversation: its turn's state, then the screen.
   *
   * Every message here belongs to the shared stream, so an unrecognized type
   * is drawn as nothing and passed to the host's handlers rather than treated
   * as an error — agent-proxy's own versioning guidance, and what keeps a new
   * message type from breaking the panel.
   */
  Box.prototype.record = function (message) {
    var type = message.type;
    if (type === 'turn_start') {
      this.openTurns[turnIdOf(message)] = true;
    } else if (type === 'turn_end') {
      delete this.openTurns[turnIdOf(message)];
    } else if (type === 'task_start') {
      if (message.task && message.task.id) this.taskIds[message.task.id] = true;
    } else if (type === 'task_end') {
      if (message.task && message.task.id) delete this.taskIds[message.task.id];
    } else if (type === 'session_closed') {
      this.handleSessionClosed();
      return;
    } else if (type === 'process_exited') {
      this.processAlive = false;
    } else if (type === 'turn_prompt') {
      // The prompt is on screen now, so there is nothing left to hand back if
      // a later command of this connection's is refused.
      this.pendingText = null;
    }
    this.draw(message);
    this.refreshComposer();
  };

  /**
   * Draw one message into its turn, or as a note when it belongs to no turn.
   *
   * Shared by the live stream and by a transcript replayed at attach, which is
   * what makes a reattached tab look exactly like one that never left.
   */
  Box.prototype.draw = function (message) {
    var self = this;
    withScrollAnchor(this.messagesEl, function () {
      var id = turnIdOf(message);
      if (id === null) {
        self.drawUnturned(message);
        return;
      }
      var turn = self.turnFor(id, message.turn.kind);
      switch (message.type) {
        case 'turn_prompt': turn.setPrompt(message.text); break;
        case 'text': turn.appendText(message.text); break;
        case 'thinking': turn.addThinking(message.text || ''); break;
        case 'tool_use': turn.addToolUse(message); break;
        case 'tool_result': turn.addToolResult(message); break;
        case 'turn_end': turn.end(message); break;
        case 'error': self.appendNote('error: ' + (message.error || 'unknown')); break;
        default: break;   // turn_start, task_*, and anything newer
      }
      self.pinStatus();
    });
  };

  /**
   * Draw a message that names no turn — a session failure, or a lost process.
   *
   * A `tool_result` is the exception agent-proxy documents: it can outrun the
   * proxy's turn accounting and arrive bare, and it belongs to whichever turn
   * is running.
   */
  Box.prototype.drawUnturned = function (message) {
    if (message.type === 'tool_result') {
      var running = this.replayTurn || this.runningTurn();
      if (running) { running.addToolResult(message); return; }
    }
    if (message.type === 'error') {
      this.appendNote('error: ' + (message.error || 'unknown'));
    } else if (message.type === 'process_exited') {
      this.appendNote('the agent stopped; the next message starts it again');
    }
  };

  /** The one turn currently running, or null when none or several are. */
  Box.prototype.runningTurn = function () {
    var open = Object.keys(this.openTurns);
    return open.length === 1 ? this.turns[open[0]] : null;
  };

  /** This turn's view, created and appended if this is the first word of it. */
  Box.prototype.turnFor = function (id, kind) {
    var turn = this.turns[id];
    if (!turn) {
      turn = new TurnView(this, id, kind);
      this.turns[id] = turn;
      this.turnOrder.push(id);
      this.messagesEl.appendChild(turn.el);
    }
    return turn;
  };

  /**
   * Replace the screen with a transcript from an `attached` message.
   *
   * Each stored turn is replayed through the same `draw` the live stream uses,
   * so nothing about a restored conversation is drawn by a second code path.
   * The prompt comes from the turn's `user` field, which is where a turn
   * submitted before this client existed keeps it.
   *
   * `replayTurn` stands in for the running turn while this runs: a stored
   * `tool_result` that arrived with no turn of its own belongs to the turn
   * whose events it is being read from, and live that is decided by which turn
   * is open — which nothing is, until the state arrives after this.
   */
  Box.prototype.renderTranscript = function (turns) {
    for (var i = 0; i < turns.length; i++) {
      var stored = turns[i];
      var view = this.turnFor(stored.id, stored.kind);
      view.setPrompt(stored.user);
      this.replayTurn = view;
      var events = stored.events || [];
      for (var j = 0; j < events.length; j++) {
        try { this.draw(events[j]); }
        catch (e) { /* one bad event costs that event, not the transcript */ }
      }
    }
    this.replayTurn = null;
    this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
  };

  /** Empty the screen and everything derived from the old conversation. */
  Box.prototype.reset = function () {
    this.messagesEl.textContent = '';
    this.turns = {};
    this.turnOrder = [];
    this.openTurns = {};
    this.taskIds = {};
    this.statusEl = null;
  };

  /**
   * Take the session's live state from an `attached` or `status` message.
   *
   * The server holds this state and the client replaces its own with it
   * — never merges, never accumulates. `attached`
   * reports whether a turn is running but not which, so the open-turn set is
   * derived from the transcript: a turn with no `turn_end` among its events is
   * still going.
   */
  Box.prototype.applyState = function (message) {
    this.processAlive = !!message.process_alive;
    this.taskIds = {};
    var tasks = message.task_ids || [];
    for (var i = 0; i < tasks.length; i++) this.taskIds[tasks[i]] = true;

    if (message.turn_ids) {
      this.openTurns = {};
      for (var j = 0; j < message.turn_ids.length; j++) {
        this.openTurns[message.turn_ids[j]] = true;
      }
    } else if (message.turns) {
      this.openTurns = {};
      for (var k = 0; k < message.turns.length; k++) {
        var turn = message.turns[k];
        if (!hasEndEvent(turn)) this.openTurns[turn.id] = true;
      }
    }
    this.refreshComposer();
  };

  /** True when a stored turn's events include the `turn_end` that finished it. */
  function hasEndEvent(turn) {
    var events = turn.events || [];
    for (var i = 0; i < events.length; i++) {
      if (events[i].type === 'turn_end') return true;
    }
    return false;
  }

  /**
   * React to the session being deleted out from under this box.
   *
   * The transcript is gone server-side, so keeping it on screen would show a
   * conversation nobody can continue. The box clears and attaches to a fresh
   * session under the same topic; a host that would rather choose the next one
   * itself listens for `session_closed` and calls `attach` from its handler.
   */
  Box.prototype.handleSessionClosed = function () {
    this.reset();
    this.session = null;
    this.lastSeq = null;
    this.appendNote('this conversation was closed — starting a new one');
    this.sendAttach();
    this.refreshComposer();
  };

  /**
   * Show a message aimed at this connection rather than at the conversation.
   *
   * These carry no `seq` and are the answer to a
   * refused command, so they are shown and then forgotten — recording one
   * would put this tab's complaint in a transcript every tab shares. A refused
   * `submit` also gets the typed text back, since the box cleared the input
   * when it sent it.
   */
  Box.prototype.showConnectionMessage = function (message) {
    if (message.type !== 'error') return;
    this.appendNote(message.error || 'the command failed');
    if (this.pendingText != null && !this.inputEl.value) {
      this.inputEl.value = this.pendingText;
      this.pendingText = null;
      this.refreshComposer();
    }
  };

  /** Add one line of the box's own commentary at the end of the list. */
  Box.prototype.appendNote = function (text) {
    var self = this;
    withScrollAnchor(this.messagesEl, function () {
      self.messagesEl.appendChild(make(self.doc, 'div', 'r2d2-note', text));
      self.pinStatus();
    });
  };

  // ---- the composer ---------------------------------------------------------

  /**
   * Send what is typed, and clear the box for the next thing.
   *
   * Nothing is drawn here. The question appears when the session broadcasts
   * `turn_prompt`, which every attached tab receives — so the tab that typed
   * and the tabs that only watch draw the same conversation from the same
   * message, and a prompt the server refused is never left on screen as though
   * it had been asked.
   */
  Box.prototype.submit = function () {
    var text = this.inputEl.value.trim();
    if (!text || this.inputEl.disabled) return;
    var command = { type: 'submit', text: text };
    if (this.context != null) command.context = this.context;
    if (!this.send(command)) {
      this.appendNote('not connected — the prompt was not sent');
      return;
    }
    this.pendingText = text;
    this.inputEl.value = '';
    this.setContext(null);
    this.refreshComposer();
  };

  /**
   * Enable or disable the composer from the session's state, and say why.
   *
   * The single authority over the input, so nothing else may touch
   * `disabled` — the three producers (a turn here, a turn in another tab, an
   * outstanding background task) cannot then fight over it. Being busy is a
   * property of the session and not of this tab.
   */
  Box.prototype.refreshComposer = function () {
    var busy = Object.keys(this.openTurns).length > 0;
    var waiting = !busy && Object.keys(this.taskIds).length > 0;
    var blocked = busy || waiting;

    this.inputEl.disabled = blocked;
    this.sendEl.disabled = blocked;
    if (busy) this.showStatus('Working', false);
    else if (waiting) this.showStatus('Waiting for background tasks', true);
    else this.hideStatus();
    this.pollStatusWhile(blocked);
  };

  /** Show the working indicator at the foot of the message list. */
  Box.prototype.showStatus = function (text, waiting) {
    if (!this.statusEl) this.statusEl = make(this.doc, 'div', 'r2d2-status');
    this.statusEl.textContent = '';
    var dot = make(this.doc, 'span', 'r2d2-status-dot');
    if (waiting) dot.className += ' r2d2-status-dot-waiting';
    this.statusEl.appendChild(dot);
    this.statusEl.appendChild(make(this.doc, 'span', 'r2d2-status-text', text));
    var self = this;
    withScrollAnchor(this.messagesEl, function () { self.pinStatus(); });
  };

  /** Keep the indicator last in the list, under whatever was just added. */
  Box.prototype.pinStatus = function () {
    if (this.statusEl) this.messagesEl.appendChild(this.statusEl);
  };

  /** Take the working indicator away. */
  Box.prototype.hideStatus = function () {
    if (this.statusEl && this.statusEl.parentNode) {
      this.statusEl.parentNode.removeChild(this.statusEl);
    }
    this.statusEl = null;
  };

  /**
   * Poll the session's status while the composer is blocked, and stop when it is not.
   *
   * The stream already reports every turn and task, so this should never
   * change anything. It exists for the case where it does: a `turn_end` or
   * `task_end` lost to a socket that dropped would otherwise leave the input
   * disabled until the page is reloaded, which is the shape of a bug one of
   * the original implementations has fixed twice.
   */
  Box.prototype.pollStatusWhile = function (blocked) {
    if (!blocked || this.destroyed) {
      if (this.statusTimer) {
        this.clearTimeout(this.statusTimer);
        this.statusTimer = null;
      }
      return;
    }
    if (this.statusTimer) return;
    var self = this;
    var tick = function () {
      self.statusTimer = null;
      if (self.destroyed) return;
      self.send({ type: 'status_query' });
      if (Object.keys(self.openTurns).length || Object.keys(self.taskIds).length) {
        self.statusTimer = self.setTimeout(tick, STATUS_POLL_MS);
      }
    };
    this.statusTimer = this.setTimeout(tick, STATUS_POLL_MS);
  };

  // ---- the host-facing API --------------------------------------------------

  /**
   * Attach arbitrary JSON to the next submit, and show it above the input.
   *
   * The text a reader has selected in a document is the motivating case: the
   * host's `build_prompt` hook receives this as its fourth argument and
   * decides what the agent is actually asked. It is cleared once a submit
   * carries it, since the next question is rarely about the same selection.
   */
  Box.prototype.setContext = function (context) {
    this.context = context == null ? null : context;
    if (this.context == null) {
      this.badgeEl.style.display = 'none';
      this.badgeEl.textContent = '';
      return;
    }
    var label = this.options.describeContext
      ? this.options.describeContext(this.context)
      : defaultContextLabel(this.context);
    this.badgeEl.textContent = '';
    this.badgeEl.appendChild(make(this.doc, 'span', 'r2d2-context-text', label));
    var clear = make(this.doc, 'span', 'r2d2-context-clear', '×');
    var self = this;
    clear.addEventListener('click', function () { self.setContext(null); });
    this.badgeEl.appendChild(clear);
    this.badgeEl.style.display = 'flex';
  };

  /** A readable one-line label for a context object the host did not describe. */
  function defaultContextLabel(context) {
    if (typeof context === 'string') return context;
    var file = context && context.file;
    if (typeof file === 'string') {
      var name = file.split('/').pop();
      if (context.startLine) {
        name += ':' + context.startLine;
        if (context.endLine && context.endLine !== context.startLine) {
          name += '-' + context.endLine;
        }
      }
      return name;
    }
    try { return JSON.stringify(context); } catch (e) { return 'context'; }
  }

  /**
   * Call `handler` for every message of this `type`, as delivered.
   *
   * `type` is any server→client message type, plus `connected` and
   * `disconnected`. Handlers receive the message unwrapped, because there is
   * nothing to unwrap — a host watching for its own tool
   * reads `message.tool` and `message.is_error` straight off it.
   */
  Box.prototype.on = function (type, handler) {
    (this.handlers[type] = this.handlers[type] || []).push(handler);
    return this;
  };

  /** Stop calling `handler` for `type`. */
  Box.prototype.off = function (type, handler) {
    var list = this.handlers[type] || [];
    var at = list.indexOf(handler);
    if (at >= 0) list.splice(at, 1);
    return this;
  };

  /**
   * Run the host's handlers for one message.
   *
   * A handler that throws is logged and the rest still run: a host's worklog
   * refresh failing must not stop the panel drawing the conversation.
   */
  Box.prototype.emit = function (type, message) {
    var list = this.handlers[type] || [];
    for (var i = 0; i < list.length; i++) {
      try { list[i](message); }
      catch (e) {
        if (this.doc.defaultView && this.doc.defaultView.console) {
          this.doc.defaultView.console.error('r2d2box: a ' + type + ' handler failed', e);
        }
      }
    }
  };

  /**
   * Render Markdown into `el`, sanitized — or as plain text if it cannot be.
   *
   * The one place in the box that writes `innerHTML`.
   * `marked` and DOMPurify are vendored beside this file but a host includes
   * them itself, so both are checked every time: with either missing the text
   * is shown verbatim rather than trusted to the browser's parser.
   */
  Box.prototype.renderMarkdownInto = function (el, markdown) {
    var marked = this.options.marked || globalIn(this.doc, 'marked');
    var purify = this.options.DOMPurify || globalIn(this.doc, 'DOMPurify');
    if (!marked || !purify || !marked.parse || !purify.sanitize) {
      el.textContent = markdown;
      return;
    }
    el.innerHTML = purify.sanitize(marked.parse(markdown, { gfm: true, breaks: true }));
  };

  /**
   * Close the socket, stop every timer, and empty the element.
   *
   * A host calls this when the panel goes away — a closed tab, a page the
   * reader navigated off. The conversation itself is untouched: a turn in
   * flight runs to completion server-side and is waiting in the transcript for
   * whoever attaches next.
   */
  Box.prototype.destroy = function () {
    this.destroyed = true;
    if (this.reconnectTimer) this.clearTimeout(this.reconnectTimer);
    if (this.statusTimer) this.clearTimeout(this.statusTimer);
    this.reconnectTimer = this.statusTimer = null;
    var socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onclose = socket.onmessage = socket.onopen = socket.onerror = null;
      try { socket.close(); } catch (e) { /* already gone */ }
    }
    this.handlers = {};
    this.el.textContent = '';
    this.el.className = this.el.className.replace(/\br2d2-box\b/, '').trim();
  };

  // Timers go through the mount element's own window, so a test can drive the
  // reconnect backoff and the status poll without waiting out real seconds.
  Box.prototype.setTimeout = function (fn, ms) {
    var view = this.doc.defaultView;
    return (view && view.setTimeout ? view.setTimeout : setTimeout)(fn, ms);
  };

  Box.prototype.clearTimeout = function (handle) {
    var view = this.doc.defaultView;
    (view && view.clearTimeout ? view.clearTimeout : clearTimeout)(handle);
  };

  /**
   * Mount a chat box into `element` and return it.
   *
   * The element is the host's; everything inside it becomes the box's. See
   * `Box` for the options.
   */
  function mount(element, options) {
    if (!element) throw new Error('R2D2Box.mount needs an element to mount into');
    return new Box(element, options || {});
  }

  return { mount: mount, VISIBLE_BLOCKS: VISIBLE_BLOCKS };
});
