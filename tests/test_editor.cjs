'use strict';
// Source-editor state tests; no claim of native keyboard or filesystem proof.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const data = (source) => 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
const loaded = import(data(fs.readFileSync(path.join(__dirname, '../viewer/editor.js'), 'utf8')));
const hash = 'a'.repeat(64), nextHash = 'b'.repeat(64);

test('indent and unindent preserve block selection and trailing next line', async () => {
  const { indentSelection } = await loaded;
  const value = indentSelection('one\ntwo\nthree', 0, 8, 4);
  assert.equal(value.text, '    one\n    two\nthree'); assert.deepEqual([value.start, value.end], [4, 16]);
  const back = indentSelection(value.text, 0, 16, 4, true);
  assert.equal(back.text, 'one\ntwo\nthree');
  assert.deepEqual(indentSelection('\nx', 0, 0, 4, true), { text: '\nx', start: 0, end: 0 });
  assert.equal(indentSelection('  x', 2, 2, 4).text, '    x');
});

test('literal find treats regex metacharacters literally and wraps both directions', async () => {
  const { findMatch } = await loaded;
  assert.deepEqual(findMatch('A.* b a.*', 'a.*'), { start: 0, end: 3 });
  assert.deepEqual(findMatch('A.* b a.*', 'a.*', 3), { start: 6, end: 9 });
  assert.deepEqual(findMatch('A.* b a.*', 'a.*', 9), { start: 0, end: 3 });
  assert.deepEqual(findMatch('A.* b a.*', 'a.*', 0, true), { start: 6, end: 9 });
  assert.equal(findMatch('A', 'a', 0, false, true), null); assert.equal(findMatch('a', ''), null);
});

test('line positions are UTF16 editor coordinates; out-of-range line navigation is bounded', async () => {
  const { linePosition, lineOffset } = await loaded;
  assert.deepEqual(linePosition('α\nabc', 4), { line: 2, column: 3 });
  assert.equal(lineOffset('a\nb', 2), 2); assert.equal(lineOffset('a\nb', 20), 3);
});

test('undo redo and edits after undo retain correct deltas and dirty baseline', async () => {
  const { TextDocument } = await loaded;
  const doc = new TextDocument({ path: 'a.py', text: 'a', sha256: hash });
  doc.edit('ab', [2, 2]); doc.edit('abc', [3, 3]); assert.equal(doc.dirty, true);
  assert.equal(doc.undo(), true); assert.equal(doc.text, 'ab'); assert.equal(doc.redo(), true); assert.equal(doc.text, 'abc');
  doc.undo(); doc.edit('abX', [3, 3]); assert.equal(doc.redo(), false); assert.equal(doc.text, 'abX');
  doc.saved(nextHash); assert.equal(doc.dirty, false); doc.undo(); assert.equal(doc.dirty, true);
  assert.ok(doc.revision >= 6); assert.ok(doc.historyBytes >= 0);
});

test('a late successful save never clears edits made while the save was in flight', async () => {
  const { TextDocument } = await loaded;
  const doc = new TextDocument({ path: 'a.py', text: 'a', sha256: hash }); doc.edit('first');
  const snapshot = doc.text; doc.edit('latest'); doc.saved(nextHash, snapshot);
  assert.equal(doc.text, 'latest'); assert.equal(doc.dirty, true); assert.equal(doc.baseSha, nextHash); assert.equal(doc.baseText, 'first');
});

test('CRLF source survives textarea normalization and recovery roundtrip', async () => {
  const { TextDocument } = await loaded;
  const doc = new TextDocument({ path: 'a.py', text: 'a\r\nb\r\n', sha256: hash });
  assert.equal(doc.text, 'a\nb\n'); assert.equal(doc.sourceText, 'a\r\nb\r\n'); assert.equal(doc.dirty, false);
  doc.edit('a\nc\n', [3, 3]); doc.scroll = [100, 20]; const record = doc.record();
  assert.equal(record.text, 'a\r\nc\r\n');
  const recovered = new TextDocument({ path: 'a.py', text: 'a\r\nb\r\n', sha256: hash }); recovered.recover(record);
  assert.equal(recovered.text, doc.text); assert.deepEqual(recovered.scroll, [100, 20]); assert.deepEqual(recovered.selection, [3, 3]); assert.equal(recovered.dirty, true);
});

test('external-baseline recovery is inert and conflicts rather than rebasing the draft', async () => {
  const { TextDocument } = await loaded;
  const doc = new TextDocument({ path: 'a.py', text: 'external', sha256: nextHash });
  doc.recover({ path: 'a.py', text: 'draft', base_sha256: hash, selection: { start: 1, end: 2 }, scroll: { top: 0, left: 0 } });
  assert.equal(doc.text, 'draft'); assert.equal(doc.baseSha, hash); assert.equal(doc.conflicted, true); assert.equal(doc.dirty, true);
});

test('readonly editing and invalid source records fail closed', async () => {
  const { TextDocument } = await loaded;
  const doc = new TextDocument({ path: 'a.py', text: 'a', sha256: hash, read_only: true });
  assert.equal(doc.edit('b'), false); assert.equal(doc.undo(), false); assert.equal(doc.text, 'a');
  assert.throws(() => new TextDocument({ path: 'a.py', text: 'x', sha256: 'bad' }));
});

test('history is bounded without replacing authoritative saved source', async () => {
  const { TextDocument } = await loaded;
  const doc = new TextDocument({ path: 'a.py', text: '', sha256: hash });
  for (let i = 0; i < 300; i++) doc.edit(doc.text + 'x');
  assert.equal(doc.undoStack.length, 200); assert.equal(doc.baseText, ''); assert.equal(doc.text.length, 300);
});
