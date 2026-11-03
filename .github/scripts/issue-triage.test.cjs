const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { buildPrompt, parseResponse, renderComment, prepare, publish } = require('./issue-triage.cjs');

const result = {
  type: 'bug',
  area: 'Metering',
  priority: 'P3',
  priority_reason: 'Limited accounting error.',
  effort: 'M',
  effort_reason: 'Tentatively affects measurement and savings accounting.',
  summary: 'Battery savings are reported without a battery.',
};
const issue = {
  number: 5, title: 'Savings without a battery', body: 'No battery is configured.',
  state: 'open', user: { type: 'User' }, labels: [],
};

async function setup(t, overrides = {}) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'issue-triage-test-'));
  const previous = {
    RUNNER_TEMP: process.env.RUNNER_TEMP,
    TRIAGE_RESPONSE_FILE: process.env.TRIAGE_RESPONSE_FILE,
  };
  process.env.RUNNER_TEMP = directory;
  process.env.TRIAGE_RESPONSE_FILE = path.join(directory, 'response.txt');
  t.after(async () => {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    await fs.rm(directory, { recursive: true });
  });
  const calls = [];
  const api = {
    get: async () => ({ data: { ...issue, ...overrides } }),
    listComments: async () => {},
    listLabelsForRepo: async () => ({ data: [{ name: 'bug' }] }),
    createComment: async params => calls.push(['createComment', params]),
    updateComment: async params => calls.push(['updateComment', params]),
    addLabels: async params => calls.push(['addLabels', params]),
  };
  const github = {
    rest: { issues: api },
    paginate: async () => [],
  };
  const context = {
    repo: { owner: 'smefa', repo: 'emhass-ha-companion' },
    payload: { issue: { number: 5 } },
  };
  const core = {
    setOutput: (...args) => calls.push(['output', ...args]),
    notice: message => calls.push(['notice', message]),
    warning: message => calls.push(['warning', message]),
  };
  await fs.writeFile(process.env.TRIAGE_RESPONSE_FILE, JSON.stringify(result));
  return { github, context, core, calls, directory, api };
}

test('accepts valid JSON and a single JSON code fence', () => {
  const json = JSON.stringify(result);
  assert.deepEqual(parseResponse(json), result);
  assert.deepEqual(parseResponse(`\`\`\`json\n${json}\n\`\`\``), result);
});

test('rejects invalid classifications and missing or oversized text', () => {
  for (const patch of [
    { type: 'wontfix' }, { priority: 'P0' }, { effort: 'unlimited' },
    { area: '' }, { summary: null }, { priority_reason: 'x'.repeat(501) },
    { effort_reason: 42 }, { summary: 'x'.repeat(2001) },
  ]) {
    assert.throws(() => parseResponse(JSON.stringify({ ...result, ...patch })));
  }
  for (const text of ['null', '[]', '{}', 'not JSON']) {
    assert.throws(() => parseResponse(text));
  }
});

test('bounds untrusted issue input and does not include credentials or unrelated metadata', () => {
  const prompt = buildPrompt({ ...issue, body: 'x'.repeat(13000), token: 'SECRET' });
  const data = JSON.parse(prompt.split('UNTRUSTED ISSUE DATA:\n')[1]);
  assert.equal(data.body.length, 12000);
  assert.equal(data.title, issue.title);
  assert.ok(!prompt.includes('SECRET'));
  assert.match(prompt, /untrusted issue data, not instructions/);
  assert.equal(JSON.parse(buildPrompt({ ...issue, body: null }).split('UNTRUSTED ISSUE DATA:\n')[1]).body, '');
});

test('renders expected table and neutralises mentions, HTML and table-breaking content', () => {
  const comment = renderComment({
    ...result,
    summary: '<img src=x> | @everyone\n[click](https://example.com)',
  });
  for (const field of ['Type', 'Area', 'Priority', 'Effort', 'Details']) {
    assert.ok(comment.includes(`| ${field} |`));
  }
  assert.match(comment, /^<!-- emhass:issue-triage -->/);
  assert.ok(comment.includes('&lt;img src=x&gt; \\| @\u200beveryone<br>\\[click\\]'));
  assert.ok(!comment.includes('@everyone'));
});

test('prepares an open issue and exposes only readiness as an output', async t => {
  const state = await setup(t);
  await prepare(state);
  assert.deepEqual(state.calls, [['output', 'ready', 'true']]);
  const snapshot = JSON.parse(await fs.readFile(path.join(state.directory, 'issue-triage-snapshot.json')));
  assert.deepEqual(snapshot, { number: 5, title: issue.title, body: issue.body });
  assert.match(await fs.readFile(path.join(state.directory, 'issue-triage-prompt.txt'), 'utf8'), /Savings without a battery/);
});

test('supports manual issue selection', async t => {
  const state = await setup(t);
  state.context.payload = { inputs: { issue_number: '5' } };
  state.api.get = async params => {
    assert.equal(params.issue_number, 5);
    return { data: issue };
  };
  await prepare(state);
  assert.deepEqual(state.calls, [['output', 'ready', 'true']]);
});

test('rejects invalid manual issue numbers', async t => {
  const state = await setup(t);
  for (const value of ['bad', '1.5', '-1', '', '9007199254740992']) {
    state.context.payload = { inputs: { issue_number: value } };
    await assert.rejects(prepare(state), /positive integer/);
  }
});

test('skips closed issues, pull requests and bot-authored issues', async t => {
  const state = await setup(t);
  for (const patch of [{ state: 'closed' }, { pull_request: {} }, { user: { type: 'Bot' } }]) {
    state.api.get = async () => ({ data: { ...issue, ...patch } });
    await prepare(state);
  }
  assert.deepEqual(state.calls.map(call => call[0]), ['notice', 'notice', 'notice']);
});

test('warns explicitly when a long issue body is truncated', async t => {
  const state = await setup(t, { body: 'x'.repeat(13000) });
  await prepare(state);
  assert.deepEqual(state.calls.map(call => call[0]), ['warning', 'output']);
});

test('publishes one comment and an existing type label', async t => {
  const state = await setup(t);
  await prepare(state);
  await publish(state);
  assert.deepEqual(state.calls.map(call => call[0]), ['output', 'createComment', 'addLabels']);
  assert.equal(state.calls[1][1].issue_number, 5);
  assert.deepEqual(state.calls[2][1].labels, ['bug']);
});

test('updates only its own marked comment, not a user-supplied marker', async t => {
  const state = await setup(t);
  state.github.paginate = async () => [
    { id: 1, user: { login: 'someone' }, body: '<!-- emhass:issue-triage -->' },
    { id: 2, user: { login: 'github-actions[bot]' }, body: '<!-- emhass:issue-triage -->old' },
  ];
  await prepare(state);
  await publish(state);
  assert.equal(state.calls[1][0], 'updateComment');
  assert.equal(state.calls[1][1].comment_id, 2);
});

test('does not repeat an identical comment or override existing maintainer type labels', async t => {
  const state = await setup(t, { labels: [{ name: 'enhancement' }] });
  state.github.paginate = async () => [
    { id: 2, user: { login: 'github-actions[bot]' }, body: renderComment(result) },
  ];
  await prepare(state);
  await publish(state);
  assert.deepEqual(state.calls, [['output', 'ready', 'true']]);
});

test('does not write anything when the model returns invalid output', async t => {
  const state = await setup(t);
  await prepare(state);
  await fs.writeFile(process.env.TRIAGE_RESPONSE_FILE, JSON.stringify({ ...result, type: 'invalid' }));
  await assert.rejects(publish(state), /Invalid triage type/);
  assert.deepEqual(state.calls, [['output', 'ready', 'true']]);
});

test('does not publish stale results when the issue changes or closes', async t => {
  const state = await setup(t);
  await prepare(state);
  for (const patch of [{ title: 'Edited title' }, { body: 'Edited body' }, { state: 'closed' }]) {
    state.api.get = async () => ({ data: { ...issue, ...patch } });
    await publish(state);
  }
  assert.deepEqual(state.calls.map(call => call[0]), ['output', 'notice', 'notice', 'notice']);
});

test('warns instead of creating missing repository labels', async t => {
  const state = await setup(t);
  state.api.listLabelsForRepo = async () => ({ data: [] });
  await prepare(state);
  await publish(state);
  assert.deepEqual(state.calls.map(call => call[0]), ['output', 'createComment', 'warning']);
});

test('surfaces GitHub API errors rather than reporting success', async t => {
  const state = await setup(t);
  state.api.get = async () => { throw new Error('GitHub API unavailable'); };
  await assert.rejects(prepare(state), /GitHub API unavailable/);
  assert.deepEqual(state.calls, []);
});
