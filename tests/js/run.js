// Run the chat box's tests under plain `node`, with no dependencies.
//
//     node tests/js/run.js            # everything
//     node tests/js/run.js reconnect  # only tests whose name contains that
//
// `tests/test_chat_box.py` runs this from the Python suite, so the front-end
// is covered by the same `pytest` as everything else. Exits non-zero on the
// first failure's account, and prints every failure it found.

'use strict';

const fs = require('fs');
const path = require('path');

const cases = [];

/** Register one test. Every file this runner loads calls it. */
function test(name, fn) {
  cases.push({ name, fn });
}

global.test = test;

const files = fs
  .readdirSync(__dirname)
  .filter((name) => name.endsWith('.test.js'))
  .sort();
for (const file of files) require(path.join(__dirname, file));

const filter = process.argv[2];
const selected = filter ? cases.filter((one) => one.name.includes(filter)) : cases;

let failed = 0;
for (const one of selected) {
  try {
    one.fn();
    process.stdout.write('.');
  } catch (error) {
    failed += 1;
    process.stdout.write('\n' + one.name + '\n    ' + (error.stack || error.message) + '\n');
  }
}

process.stdout.write(`\n${selected.length - failed} passed, ${failed} failed\n`);
process.exit(failed ? 1 : 0);
