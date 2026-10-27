#!/usr/bin/env node
'use strict';
// Actual public network-contract gate. It never downloads/installs a native ZIP.
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const { promisify } = require('node:util');
const { execFile } = require('node:child_process');
const client = require('../shell/updater.cjs');
const ROOT = path.resolve(__dirname, '..');
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
function requireThat(ok, message) { if (!ok) throw new Error(message); }
async function sourceSnapshot(root = ROOT) {
  // Reuse the repository's canonical input inventory, including dirty development
  // inputs. The report never upgrades a dirty snapshot into a clean CI verdict.
  const code = 'import sys,json;sys.path.insert(0,sys.argv[1]);from release_evidence import source_snapshot;print(json.dumps(source_snapshot(sys.argv[2]),separators=(",",":")))';
  const result = await promisify(execFile)('python3', ['-B', '-c', code, path.join(root, 'tools'), root],
    { cwd: root, timeout: 30000, maxBuffer: 4 * 1024 * 1024 });
  const value = JSON.parse(result.stdout);
  requireThat(value.schema === 1 && /^[a-f0-9]{40}$/.test(value.source_sha) && /^[a-f0-9]{64}$/.test(value.fingerprint) && typeof value.clean === 'boolean', 'Invalid canonical source snapshot');
  return value;
}
async function sourceHashes(root = ROOT) {
  return Object.fromEntries(await Promise.all(['shell/updater.cjs', 'shell/update_install.cjs', 'tools/update_check_smoke.cjs'].map(async name => [name, digest(await fs.readFile(path.join(root, name)))])));
}
function parseArguments(argv) {
  const result = {};
  for (let i = 0; i < argv.length; i += 2) {
    requireThat(['--arch', '--output'].includes(argv[i]) && argv[i + 1] && !argv[i + 1].startsWith('--'), 'Usage: update_check_smoke.cjs --arch arm64|x64 --output evidence/updatecheck.json');
    const key = argv[i].slice(2); requireThat(!(key in result), 'Duplicate update-check argument'); result[key] = argv[i + 1];
  }
  requireThat(['arm64', 'x64'].includes(result.arch) && typeof result.output === 'string', 'Missing/invalid update-check architecture or output');
  return result;
}
async function runCheck({ arch, output }, { fetcher = globalThis.fetch, snapshot = sourceSnapshot, hashes = sourceHashes } = {}) {
  requireThat(['arm64', 'x64'].includes(arch), 'Invalid update-check architecture');
  const file = path.resolve(output), directory = path.dirname(file), stem = path.basename(file, path.extname(file));
  await fs.mkdir(directory, { recursive: true });
  const report = { schema: 1, kind: 'update-network-contract', pass: false, architecture: arch,
    fixture_current_version: '0.0.0', fixture_purpose: 'select the actual published stable release; never install or downgrade',
    runtime: { node: process.version, platform: process.platform, architecture: process.arch },
    default_fetch: fetcher === globalThis.fetch, authenticated: false, install_attempted: false, native_GUI_proof: false,
    source_clean: false, source_unchanged: false, statuses: [], started_ms: Date.now(), error: null };
  let before, beforeHashes, failure;
  const save = async (name, value) => {
    const bytes = Buffer.isBuffer(value) ? value : Buffer.from(JSON.stringify(value, null, 2) + '\n');
    await fs.writeFile(path.join(directory, name), bytes); return { path: name, bytes: bytes.length, sha256: digest(bytes) };
  };
  try {
    before = await snapshot(); beforeHashes = await hashes();
    report.source_before = await save(`${stem}-source-before.json`, before);
    report.source_sha = before.source_sha; report.source_fingerprint = before.fingerprint; report.source_hashes = beforeHashes;
    if (process.env.GITHUB_SHA) requireThat(process.env.GITHUB_SHA === before.source_sha, 'Checkout differs from exact CI SHA');
    const observedFetch = async (url, options) => {
      // Only observe the ordinary transport. Never change routing, TLS, headers,
      // response bodies, status codes, or the client's redirect behavior.
      const response = await fetcher(url, options), address = new URL(url);
      report.statuses.push({ endpoint: address.origin + address.pathname, status: response.status, accept: options.headers.Accept, headers: client.httpHeaders(response) });
      requireThat(!Object.keys(options.headers).some(name => ['authorization', 'cookie'].includes(name.toLowerCase())), 'Unexpected credential-bearing request');
      return response;
    };
    const signal = AbortSignal.timeout(30000); // One aggregate metadata/checksum budget.
    const found = await client.releaseMetadata(arch, observedFetch, signal), metadata = found.bytes;
    report.metadata_sha256 = digest(metadata); report.metadata_bytes = metadata.length;
    report.metadata_source = found.publicSources.length ? 'public-github-release' : 'github-api';
    report.public_sources = [];
    for (const source of found.publicSources) report.public_sources.push({kind: source.kind, endpoint: source.url,
      ...await save(`${stem}-public-${source.kind}.${source.kind === 'assets' ? 'html' : source.kind === 'candidate' ? 'json' : 'txt'}`, source.bytes)});
    if (found.quota) report.api_quota = found.quota;
    const release = found.release;
    const selected = client.selectRelease(release, '0.0.0', arch);
    requireThat(selected && selected.arch === arch, 'No actual published stable native release');
    const checksums = await client.smallBody(selected.checksums.url, 64 * 1024, observedFetch, signal);
    const actualChecksumHash = digest(checksums);
    requireThat(checksums.length === selected.checksums.size && actualChecksumHash === selected.checksums.sha256, 'Actual checksum document differs from GitHub asset metadata');
    requireThat(client.checksumFor(checksums, selected.archive.name) === selected.archive.sha256, 'Actual checksum document and native ZIP API digest disagree');
    report.published_release = release.tag_name; report.release_url = selected.url;
    report.native_archive = { name: selected.archive.name, arch, bytes: selected.archive.size, sha256: selected.archive.sha256, downloaded: false };
    const metadataName = `${stem}-release.json`;
    await fs.writeFile(path.join(directory, metadataName), metadata);
    report.metadata = { path: metadataName, bytes: metadata.length, sha256: report.metadata_sha256 };
    const sumsName = `${stem}-SHA256SUMS.txt`;
    await fs.writeFile(path.join(directory, sumsName), checksums);
    report.checksums = { path: sumsName, bytes: checksums.length, sha256: actualChecksumHash };
    report.network_contract_pass = true;
  } catch (error) {
    failure = error;
    report.error = { name: error.name, code: error.code || null, cause_code: error.cause?.code || null,
      message: String(error.message).replace(/https?:\/\/\S+/g, '[HTTPS endpoint]').slice(0, 512),
      http: error.http || null };
  } finally {
    try {
      const after = await snapshot(), afterHashes = await hashes();
      report.source_after = await save(`${stem}-source-after.json`, after);
      report.after_fingerprint = after.fingerprint; report.after_source_hashes = afterHashes;
      report.source_unchanged = !!before && before.fingerprint === after.fingerprint && before.source_sha === after.source_sha && JSON.stringify(beforeHashes) === JSON.stringify(afterHashes);
      report.source_clean = before?.clean === true && after.clean === true;
      requireThat(report.source_unchanged, 'Source changed during real update-check gate');
      report.pass = report.network_contract_pass === true;
    } catch (error) { failure ||= error; report.error ||= { name: error.name, code: error.code || null, message: error.message }; report.pass = false; }
    report.finished_ms = Date.now();
    // Development may inspect a dirty stable-source report. Only CI clean-source
    // plus pass qualifies promotion; no dirty snapshot is called a clean gate.
    report.CI_qualification = report.pass && report.source_clean && report.default_fetch && process.platform === 'darwin' && process.arch === arch && !!process.env.GITHUB_SHA;
    await fs.writeFile(file, JSON.stringify(report, null, 2) + '\n');
  }
  if (failure) throw Object.assign(new Error(report.error.message), { report, cause: failure });
  return report;
}
module.exports = { parseArguments, runCheck, sourceSnapshot, sourceHashes };
if (require.main === module) {
  runCheck(parseArguments(process.argv.slice(2))).then(report => console.log(JSON.stringify({ pass: report.pass, source_clean: report.source_clean, source_sha: report.source_sha, published_release: report.published_release })))
    .catch(error => { console.error(error.message); process.exitCode = 1; });
}
