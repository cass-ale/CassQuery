// Plain text editing only. Python is executed by the certified worker, never by this editor.
export function changeBetween(before, after) {
  let start = 0;
  while (start < before.length && start < after.length && before[start] === after[start]) start++;
  let a = before.length, b = after.length;
  while (a > start && b > start && before[a - 1] === after[b - 1]) { a--; b--; }
  return { start, before: before.slice(start, a), after: after.slice(start, b) };
}
export function linePosition(text, offset) {
  offset = Math.max(0, Math.min(text.length, offset));
  const prefix = text.slice(0, offset);
  return { line: prefix.split("\n").length, column: offset - prefix.lastIndexOf("\n") };
}
export function lineOffset(text, line) {
  let offset = 0;
  for (let i = 1; i < line; i++) { const next = text.indexOf("\n", offset); if (next < 0) return text.length; offset = next + 1; }
  return offset;
}
export function indentSelection(text, start, end, tabSize = 4, unindent = false) {
  if (![2, 4].includes(tabSize)) throw new Error("Tab size must be 2 or 4");
  if (start === end && !unindent) {
    const column = start - (text.lastIndexOf("\n", start - 1) + 1), width = tabSize - column % tabSize;
    return { text: text.slice(0, start) + " ".repeat(width) + text.slice(end), start: start + width, end: end + width };
  }
  const from = start === 0 ? 0 : text.lastIndexOf("\n", start - 1) + 1;
  const last = end > start && text[end - 1] === "\n" ? end - 1 : end;
  const next = text.indexOf("\n", last), to = next < 0 ? text.length : next;
  const lines = text.slice(from, to).split("\n"), deltas = [];
  const replacement = lines.map((line) => {
    const remove = unindent ? (line.startsWith("\t") ? 1 : Math.min(tabSize, (line.match(/^ */) || [""])[0].length)) : 0;
    deltas.push(unindent ? -remove : tabSize);
    return unindent ? line.slice(remove) : " ".repeat(tabSize) + line;
  }).join("\n");
  return { text: text.slice(0, from) + replacement + text.slice(to),
    start: Math.max(from, start + deltas[0]), end: Math.max(from, end + deltas.reduce((a, b) => a + b, 0)) };
}
export function findMatch(text, query, offset = 0, backwards = false, sensitive = false) {
  if (!query) return null;
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const expression = new RegExp(escaped, sensitive ? "g" : "gi");
  let first = null, last = null, prior = null, match;
  while ((match = expression.exec(text))) {
    const found = { start: match.index, end: match.index + match[0].length };
    first ||= found; last = found;
    if (backwards && found.start < offset) prior = found;
    if (!backwards && found.start >= offset) return found;
  }
  return backwards ? (prior || last) : first;
}

export class TextDocument {
  constructor({ path, text, sha256, read_only = false }) {
    if (typeof path !== "string" || typeof text !== "string" || !/^[a-f0-9]{64}$/.test(sha256)) throw new Error("Invalid source document");
    this.path = path; this.eol = text.includes("\r\n") && !text.replace(/\r\n/g, "").includes("\n") ? "\r\n" : "\n";
    this.text = text.replace(/\r\n/g, "\n"); this.baseText = this.text; this.baseSha = sha256;
    this.readOnly = read_only; this.conflicted = false; this.revision = 0;
    this.selection = [0, 0]; this.scroll = [0, 0]; this.undoStack = []; this.redoStack = []; this.historyBytes = 0;
  }
  get dirty() { return this.conflicted || this.text !== this.baseText; }
  get sourceText() { return this.eol === "\r\n" ? this.text.replace(/\n/g, "\r\n") : this.text; }
  edit(text, selection = this.selection) {
    text = text.replace(/\r\n/g, "\n");
    if (this.readOnly) return false;
    if (text === this.text) { this.selection = [...selection]; return false; }
    const edit = { ...changeBetween(this.text, text), selectionBefore: [...this.selection], selectionAfter: [...selection] };
    this.historyBytes -= this.redoStack.reduce((sum, item) => sum + 2 * (item.before.length + item.after.length), 0);
    this.undoStack.push(edit); this.redoStack = [];
    this.historyBytes += 2 * (edit.before.length + edit.after.length);
    // shortcut: bounded delta history, not an unbounded full-text snapshot per keystroke.
    while (this.undoStack.length > 200 || this.historyBytes > 8 * 1024 * 1024) {
      const removed = this.undoStack.shift(); this.historyBytes -= 2 * (removed.before.length + removed.after.length);
    }
    this.text = text; this.selection = [...selection]; this.revision++; return true;
  }
  undo() {
    if (this.readOnly) return false;
    const edit = this.undoStack.pop(); if (!edit) return false;
    this.text = this.text.slice(0, edit.start) + edit.before + this.text.slice(edit.start + edit.after.length);
    this.selection = [...edit.selectionBefore]; this.redoStack.push(edit); this.revision++; return true;
  }
  redo() {
    if (this.readOnly) return false;
    const edit = this.redoStack.pop(); if (!edit) return false;
    this.text = this.text.slice(0, edit.start) + edit.after + this.text.slice(edit.start + edit.before.length);
    this.selection = [...edit.selectionAfter]; this.undoStack.push(edit); this.revision++; return true;
  }
  saved(sha256, savedText = this.text) {
    if (!/^[a-f0-9]{64}$/.test(sha256)) throw new Error("Invalid saved source hash");
    this.baseSha = sha256; this.baseText = savedText.replace(/\r\n/g, "\n"); this.conflicted = false;
  }
  recover(record) {
    if (record.path !== this.path || typeof record.text !== "string" || !/^[a-f0-9]{64}$/.test(record.base_sha256)) throw new Error("Invalid recovery record");
    this.conflicted = record.base_sha256 !== this.baseSha;
    this.baseSha = record.base_sha256; this.text = record.text.replace(/\r\n/g, "\n"); this.revision++;
    this.selection = [record.selection?.start, record.selection?.end].map((v) => Number.isInteger(v) ? Math.max(0, Math.min(this.text.length, v)) : 0);
    this.scroll = [record.scroll?.top, record.scroll?.left].map((v) => Number.isFinite(v) ? Math.max(0, v) : 0);
  }
  record() {
    return { path: this.path, text: this.sourceText, base_sha256: this.baseSha, selection: { start: this.selection[0], end: this.selection[1] },
      scroll: { top: this.scroll[0], left: this.scroll[1] } };
  }
}

export class EditorView {
  constructor({ input, gutter, mirror, position, changed, tabSize = 4, lineWrap = false }) {
    this.input = input; this.gutter = gutter; this.mirror = mirror; this.position = position;
    this.changed = changed; this.tabSize = tabSize; this.lineWrap = lineWrap; this.document = null;
    input.addEventListener("input", () => {
      if (!this.document) return;
      this.document.edit(input.value, [input.selectionStart, input.selectionEnd]);
      this.renderGutter(); this.changed(this.document);
    });
    input.addEventListener("keydown", (e) => {
      if (!this.document) return;
      if (e.key === "Tab" && !this.document.readOnly) {
        e.preventDefault(); const value = indentSelection(input.value, input.selectionStart, input.selectionEnd, this.tabSize, e.shiftKey);
        this.document.edit(value.text, [value.start, value.end]); this.render(); this.changed(this.document);
      } else if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "z") {
        e.preventDefault(); e.stopPropagation(); this.undo(e.shiftKey);
      }
    });
    const remember = () => {
      if (!this.document) return;
      this.document.selection = [input.selectionStart, input.selectionEnd];
      this.document.scroll = [input.scrollTop, input.scrollLeft]; this.renderPosition();
    };
    input.addEventListener("select", remember); input.addEventListener("keyup", remember); input.addEventListener("click", remember);
    input.addEventListener("scroll", () => { remember(); this.drawNumbers(); });
    new ResizeObserver(() => this.renderGutter()).observe(input);
  }
  show(doc) {
    if (this.document) { this.document.selection = [this.input.selectionStart, this.input.selectionEnd]; this.document.scroll = [this.input.scrollTop, this.input.scrollLeft]; }
    this.document = doc; this.render();
  }
  render() {
    const doc = this.document;
    this.input.value = doc?.text || ""; this.input.readOnly = !doc || doc.readOnly;
    this.input.setAttribute("aria-label", doc ? `Source: ${doc.path}` : "Source editor");
    if (doc) { this.input.setSelectionRange(...doc.selection); this.input.scrollTop = doc.scroll[0]; this.input.scrollLeft = doc.scroll[1]; }
    this.renderGutter(); this.renderPosition();
  }
  configure({ tabSize, lineWrap }) { this.tabSize = tabSize; this.lineWrap = lineWrap; this.renderGutter(); }
  renderPosition() {
    const p = linePosition(this.input.value, this.input.selectionStart);
    this.position.textContent = `Ln ${p.line}, Col ${p.column}`;
  }
  renderGutter() {
    const lines = this.input.value.split("\n"); this.lines = lines;
    // shortcut: browser-measured wrap mirror is bounded to 5,000 lines; larger files
    // retain accurate unwrapped virtual line numbers instead of a huge mirrored DOM.
    this.wrap = this.lineWrap && lines.length <= 5000;
    this.input.wrap = this.wrap ? "soft" : "off";
    this.input.dataset.wrapLimited = String(this.lineWrap && !this.wrap);
    this.mirror.replaceChildren(); this.offsets = null;
    const style = getComputedStyle(this.input);
    this.lineHeight = parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.6;
    if (this.wrap) {
      this.mirror.style.width = this.input.clientWidth + "px";
      for (const line of lines) { const span = document.createElement("div"); span.textContent = line || "\u200b"; this.mirror.append(span); }
      this.offsets = [...this.mirror.children].map((node) => node.offsetTop);
    }
    this.drawNumbers(); this.renderPosition();
  }
  drawNumbers() {
    const scroll = this.input.scrollTop, height = this.input.clientHeight;
    let first = this.offsets ? this.offsets.findIndex((top, i) => (this.offsets[i + 1] ?? Infinity) > scroll) : Math.floor(scroll / this.lineHeight);
    first = Math.max(0, first); this.gutter.replaceChildren();
    for (let i = first; i < this.lines.length; i++) {
      const top = this.offsets ? this.offsets[i] : i * this.lineHeight + 8;
      if (top > scroll + height + this.lineHeight) break;
      const number = document.createElement("span"); number.textContent = String(i + 1); number.style.top = (top - scroll) + "px";
      this.gutter.append(number);
    }
  }
  undo(redo = false) {
    if (!this.document || !(redo ? this.document.redo() : this.document.undo())) return false;
    this.render(); this.changed(this.document); return true;
  }
  find(query, backwards = false, sensitive = false) {
    const offset = backwards ? this.input.selectionStart : this.input.selectionEnd;
    const found = findMatch(this.input.value, query, offset, backwards, sensitive);
    if (!found) return false;
    this.input.focus(); this.input.setSelectionRange(found.start, found.end);
    const line = linePosition(this.input.value, found.start).line - 1;
    this.input.scrollTop = this.offsets?.[line] ?? line * this.lineHeight;
    this.document.selection = [found.start, found.end]; this.renderPosition(); return true;
  }
  goto(line) {
    const offset = lineOffset(this.input.value, line); this.input.focus(); this.input.setSelectionRange(offset, offset);
    this.input.scrollTop = this.offsets?.[line - 1] ?? (line - 1) * this.lineHeight; this.renderPosition();
  }
}
