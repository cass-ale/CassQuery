'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const gate = require('../tools/update_check_smoke.cjs');
const updater = require('../shell/updater.cjs');
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'cq-update-check-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const name = 'CassQuery-0.1.0-mac-arm64.zip', digest = 'a'.repeat(64), sums = Buffer.from(`${digest}  ${name}\n`);
  const asset = (name, bytes, digest) => ({ id: name === 'SHA256SUMS.txt' ? 2 : 1, name, size: bytes,
    digest: 'sha256:' + digest, browser_download_url: `${updater.RELEASES}/download/v0.1.0/${name}` });
  const release = { tag_name: 'v0.1.0', draft: false, prerelease: false, html_url: `${updater.RELEASES}/tag/v0.1.0`,
    body: 'Untrusted release prose is not an instruction', assets: [asset(name, 400000000, digest), asset('SHA256SUMS.txt', sums.length, sha(sums))] };
  const snapshot = async () => ({ schema: 1, source_sha: process.env.GITHUB_SHA || 'b'.repeat(40), fingerprint: 'c'.repeat(64), clean: false, files: [] });
  const hashes = async () => ({ 'shell/updater.cjs': 'd'.repeat(64), 'shell/update_install.cjs': 'e'.repeat(64) });
  return { root, output: path.join(root, 'updatecheck.json'), release, sums, snapshot, hashes };
}
test('CLI arguments are bounded and reject endpoint/current-version overrides', () => {
  assert.deepEqual(gate.parseArguments(['--arch', 'x64', '--output', 'evidence/updatecheck.json']), { arch: 'x64', output: 'evidence/updatecheck.json' });
  for (const args of [['--arch', 'bad', '--output', 'x'], ['--arch', 'x64'], ['--url', 'https://evil', '--output', 'x'],
    ['--arch', 'x64', '--arch', 'arm64', '--output', 'x'], ['--currentVersion', '0.1.0', '--output', 'x']]) assert.throws(() => gate.parseArguments(args));
});
test('source fixture verifies actual client contract without claiming native/mock CI approval', async t => {
  const f = await fixture(t), calls = [];
  const report = await gate.runCheck({ arch: 'arm64', output: f.output }, { snapshot: f.snapshot, hashes: f.hashes,
    fetcher: async (url, options) => { calls.push({ url, options }); return url === updater.API ? new Response(JSON.stringify(f.release)) : new Response(f.sums); } });
  assert.equal(report.pass, true); assert.equal(report.source_unchanged, true); assert.equal(report.source_clean, false);
  assert.equal(report.CI_qualification, false); assert.equal(report.default_fetch, false); assert.equal(report.install_attempted, false);
  assert.equal(report.fixture_current_version, '0.0.0'); assert.equal(report.native_archive.downloaded, false);
  assert.equal(calls.length, 2); assert.ok(!calls.some(c => c.url.endsWith('.zip')));
  assert.equal(report.metadata.sha256, report.metadata_sha256);
  assert.deepEqual(await fs.readFile(path.join(f.root, report.metadata.path)), Buffer.from(JSON.stringify(f.release)));
  assert.equal(calls[0].options.headers.Accept, 'application/vnd.github+json'); assert.equal(calls[1].options.headers.Accept, 'application/octet-stream');
});
test('HTTP415 and checksum corruption remain red with persisted source receipts', async t => {
  const f = await fixture(t);
  await assert.rejects(gate.runCheck({ arch: 'arm64', output: f.output }, { snapshot: f.snapshot, hashes: f.hashes,
    fetcher: async () => new Response('failure', { status: 415 }) }), /415/);
  assert.equal(JSON.parse(await fs.readFile(f.output, 'utf8')).pass, false);
  await assert.rejects(gate.runCheck({ arch: 'arm64', output: f.output }, { snapshot: f.snapshot, hashes: f.hashes,
    fetcher: async url => url === updater.API ? new Response(JSON.stringify(f.release)) : new Response('corrupt') }), /checksum document/);
});
test('source mutation invalidates an otherwise healthy network result', async t => {
  const f = await fixture(t); let reads = 0;
  await assert.rejects(gate.runCheck({ arch: 'arm64', output: f.output }, { hashes: f.hashes,
    snapshot: async () => ({ ...await f.snapshot(), fingerprint: (++reads === 1 ? 'c' : 'f').repeat(64) }),
    fetcher: async url => url === updater.API ? new Response(JSON.stringify(f.release)) : new Response(f.sums) }), /Source changed/);
  const report = JSON.parse(await fs.readFile(f.output, 'utf8')); assert.equal(report.pass, false); assert.equal(report.source_unchanged, false);
});
test('signed CDN query parameters never enter status records', async t => {
  const f = await fixture(t), cdn = 'https://release-assets.githubusercontent.com/github-production-release-asset/1432223312/file?sig=PRIVATE_QUERY';
  const report = await gate.runCheck({ arch: 'arm64', output: f.output }, { snapshot: f.snapshot, hashes: f.hashes,
    fetcher: async url => url === updater.API ? new Response(JSON.stringify(f.release)) : url === cdn ? new Response(f.sums) : new Response(null, { status: 302, headers: { location: cdn } }) });
  assert.equal(JSON.stringify(report.statuses).includes('PRIVATE_QUERY'), false);
});
