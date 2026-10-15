import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import * as nodeModule from 'node:module';
import { MicSession, createRunScriptTransport, validateReply } from './mic-lease.mjs';

// Mock-only protocol model. No fetch, device, files, configfs, or browser APIs.
test('tested JavaScript matches whitespace-preserving TypeScript type erasure', {
  skip: typeof nodeModule.stripTypeScriptTypes !== 'function' &&
    'type-erasure comparison needs Node >=22.13; all runtime fixtures use checked-in MJS'
}, () => {
  const ts = fs.readFileSync(new URL('./mic-lease.ts', import.meta.url), 'utf8');
  const js = fs.readFileSync(new URL('./mic-lease.mjs', import.meta.url), 'utf8');
  assert.equal(nodeModule.stripTypeScriptTypes(ts, { mode: 'strip' }), js);
});
const OWNER_A = 'a'.repeat(32), OWNER_B = 'b'.repeat(32);
const EPOCH_A = '1'.repeat(32), EPOCH_B = '2'.repeat(32);
function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, bad) => { resolve = ok; reject = bad; });
  return { promise, resolve, reject };
}
async function until(condition) {
  for (let i = 0; i < 30; i++) {
    if (condition()) return;
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.fail('mock condition did not become true');
}
class LeaseModel {
  epoch = EPOCH_A; active = null; terminal = new Map(); calls = []; jack = 0;
  reply(ok, code) {
    return { version: 1, ok, code, phase: this.active?.phase ?? 'idle', jack: this.jack,
      ttlMs: this.active ? 10000 : 0, leaseSeq: this.active?.seq ?? null, bootEpoch: this.epoch };
  }
  async status() { this.calls.push(['status']); return this.reply(true, 'status'); }
  async call(action, token) {
    this.calls.push([action, { ...token }]);
    if (token.bootEpoch !== this.epoch) return this.reply(false, 'epoch_mismatch');
    const ended = this.terminal.get(token.owner) ?? 0;
    if (action === 'release') {
      this.terminal.set(token.owner, Math.max(ended, token.seq));
      if (this.active?.owner === token.owner && this.active.seq <= token.seq) {
        this.active = null; this.jack = 0;
      }
      return this.reply(true, 'released');
    }
    if (token.seq <= ended) return this.reply(false, 'stale');
    if (this.active && (this.active.owner !== token.owner || this.active.seq !== token.seq)) {
      return this.reply(false, 'busy');
    }
    if (action === 'claim') {
      this.active ??= { owner: token.owner, seq: token.seq, phase: 'reserved' };
      return this.reply(true, 'claimed');
    }
    if (!this.active) return this.reply(false, 'stale');
    if (action === 'commit') { this.active.phase = 'active'; this.jack = 1; }
    return this.reply(true, action === 'commit' ? 'committed' : 'renewed');
  }
  restart() { this.epoch = EPOCH_B; this.active = null; this.terminal.clear(); this.jack = 0; }
}
function stream({ ended = false, audio = true } = {}) {
  const listeners = new Set();
  const track = {
    readyState: ended ? 'ended' : 'live', stopped: 0,
    stop() { this.stopped++; this.readyState = 'ended'; },
    addEventListener(name, fn) { assert.equal(name, 'ended'); listeners.add(fn); },
    removeEventListener(name, fn) { assert.equal(name, 'ended'); listeners.delete(fn); },
    end() { this.readyState = 'ended'; for (const fn of [...listeners]) fn(); }
  };
  return { track, getTracks: () => [track], getAudioTracks: () => audio ? [track] : [] };
}
function fixture({ model = new LeaseModel(), lease = model, owner = OWNER_A, capture, attach } = {}) {
  const captured = stream(), states = [], timers = new Set();
  let detached = 0, attached = 0;
  const session = new MicSession({ owner, lease, capture: capture ?? (async () => captured),
    attach: attach ?? (() => { attached++; return () => { detached++; }; }),
    onState: state => states.push(state),
    every(ms, callback) { assert.equal(ms, 2000); timers.add(callback); return () => timers.delete(callback); }
  });
  return { session, model, captured, states, timers, get detached() { return detached; }, get attached() { return attached; } };
}

test('happy path claims jack 0, attaches, commits 1, renews, synchronously stops before release ACK', async () => {
  const f = fixture();
  await f.session.enable();
  assert.deepEqual(f.model.calls.map(c => c[0]), ['status', 'claim', 'commit']);
  assert.equal(f.session.getState().phase, 'on'); assert.equal(f.model.jack, 1);
  for (const callback of f.timers) callback();
  await until(() => f.model.calls.some(c => c[0] === 'renew'));
  const releaseGate = deferred(), original = f.model.call.bind(f.model);
  f.model.call = (action, token) => action === 'release' ? releaseGate.promise.then(() => original(action, token)) : original(action, token);
  const stopping = f.session.disable();
  assert.equal(f.captured.track.stopped, 1); assert.equal(f.detached, 1); assert.equal(f.timers.size, 0);
  assert.equal(f.session.getState().phase, 'closing');
  await f.session.disable(); // Repeated off must not invalidate the first ACK.
  releaseGate.resolve(); await stopping;
  assert.equal(f.model.jack, 0); assert.equal(f.session.getState().phase, 'off');
});

test('permission rejection never claims or commits', async () => {
  const f = fixture({ capture: async () => { throw new Error('NotAllowedError'); } });
  await f.session.enable();
  assert.deepEqual(f.model.calls.map(c => c[0]), ['status']);
  assert.equal(f.session.getState().code, 'NotAllowedError');
  assert.equal(f.model.jack, 0);
});

test('late getUserMedia success after off is stopped without remote claim or attach', async () => {
  const pending = deferred(), late = stream();
  const f = fixture({ capture: () => pending.promise });
  const enabling = f.session.enable(); await until(() => f.model.calls.length === 1);
  await f.session.disable(); pending.resolve(late); await enabling;
  assert.equal(late.track.stopped, 1); assert.equal(f.attached, 0);
  assert.deepEqual(f.model.calls.map(c => c[0]), ['status']);
  assert.equal(f.session.getState().phase, 'off');
});

for (const oldResult of ['success', 'failure']) test(`rapid on/off/on ignores old capture ${oldResult}`, async () => {
  const first = deferred(), old = stream(), fresh = stream(); let count = 0;
  const f = fixture({ capture: () => ++count === 1 ? first.promise : Promise.resolve(fresh) });
  const oldEnable = f.session.enable(); await until(() => count === 1);
  await f.session.disable(); await f.session.enable();
  if (oldResult === 'success') first.resolve(old); else first.reject(new Error('old denied'));
  await oldEnable;
  assert.equal(f.session.getState().phase, 'on'); assert.equal(f.model.active.seq, 2);
  assert.equal(fresh.track.stopped, 0);
  if (oldResult === 'success') assert.equal(old.track.stopped, 1);
  await f.session.disable();
});

for (const held of ['claim', 'commit']) test(`release arrives before delayed ${held}; tombstone prevents resurrection`, async () => {
  const model = new LeaseModel(), gate = deferred(); let waiting = false;
  const lease = { status: () => model.status(), call: async (action, token) => {
    if (action === held) { waiting = true; await gate.promise; }
    return model.call(action, token);
  } };
  const f = fixture({ model, lease }); const enabling = f.session.enable();
  await until(() => waiting); await f.session.disable();
  assert.equal(model.jack, 0); assert.equal(model.terminal.get(OWNER_A), 1);
  gate.resolve(); await enabling;
  assert.equal(model.jack, 0); assert.equal(model.active, null); assert.equal(f.session.getState().phase, 'off');
  assert.equal(f.captured.track.stopped, 1);
});

test('late attach cleanup detaches exact old sender and cannot commit after off', async () => {
  const gate = deferred(); let attached = false, detached = 0;
  const f = fixture({ attach: () => { attached = true; return gate.promise; } });
  const enabling = f.session.enable(); await until(() => attached);
  await f.session.disable(); gate.resolve(() => { detached++; }); await enabling;
  assert.equal(detached, 1); assert.equal(f.model.calls.some(c => c[0] === 'commit'), false);
  assert.equal(f.model.jack, 0);
});

test('second tab busy and default false never turn off first tab', async () => {
  const model = new LeaseModel(), a = fixture({ model }), b = fixture({ model, owner: OWNER_B });
  await a.session.enable(); const before = model.calls.length;
  await b.session.disable(); assert.equal(model.calls.length, before);
  await b.session.enable(); assert.equal(b.session.getState().code, 'busy');
  assert.equal(model.jack, 1); assert.equal(model.active.owner, OWNER_A);
  assert.equal(b.captured.track.stopped, 1); await a.session.disable();
});

test('rapid new enable waits for previous release before claiming a higher seq', async () => {
  const model = new LeaseModel(), gate = deferred();
  let captureCount = 0;
  const f = fixture({ model, capture: async () => { captureCount++; return stream(); } });
  await f.session.enable();
  const call = model.call.bind(model);
  model.call = async (action, token) => { if (action === 'release' && token.seq === 1) await gate.promise; return call(action, token); };
  const stopping = f.session.disable();
  const enabling = f.session.enable(); await until(() => captureCount === 2);
  assert.equal(model.calls.filter(c => c[0] === 'claim').length, 1);
  gate.resolve(); await stopping; await enabling;
  assert.equal(model.active.seq, 2); assert.equal(f.session.getState().phase, 'on');
  await f.session.disable();
});

test('successful release accepts jack 1 belonging to a subsequent owner', async () => {
  const f = fixture(); await f.session.enable();
  const call = f.model.call.bind(f.model);
  f.model.call = async (action, token) => {
    const result = await call(action, token);
    if (action === 'release') {
      await call('claim', { owner: OWNER_B, seq: 1, bootEpoch: EPOCH_A });
      await call('commit', { owner: OWNER_B, seq: 1, bootEpoch: EPOCH_A });
      return f.model.reply(true, 'released');
    }
    return result;
  };
  await f.session.disable();
  assert.equal(f.session.getState().phase, 'off'); assert.equal(f.session.getState().jack, 1);
  assert.equal(f.model.active.owner, OWNER_B);
});

test('daemon restart rejects an old commit and closes local capture', async () => {
  const model = new LeaseModel(), gate = deferred(); let waiting = false;
  const lease = { status: () => model.status(), call: async (action, token) => {
    if (action === 'commit') { waiting = true; await gate.promise; }
    return model.call(action, token);
  } };
  const f = fixture({ model, lease }); const enabling = f.session.enable();
  await until(() => waiting); model.restart(); gate.resolve(); await enabling;
  assert.equal(model.jack, 0); assert.equal(f.session.getState().code, 'epoch_mismatch');
  assert.equal(f.captured.track.stopped, 1); assert.equal(f.detached, 1);
});

test('release transport failure stops immediately and reports remote state unknown', async () => {
  const f = fixture(); await f.session.enable();
  const call = f.model.call.bind(f.model);
  f.model.call = (action, token) => { if (action === 'release') throw new Error('network down'); return call(action, token); };
  const stopping = f.session.disable(); assert.equal(f.captured.track.stopped, 1); await stopping;
  assert.deepEqual(f.session.getState(), { phase: 'error', code: 'remote_close_unconfirmed', jack: null });
  // Server-side autonomous TTL is tested in the daemon suite, not simulated here.
  assert.equal(f.model.jack, 1);
});

test('renew refusal fails closed locally and releases owned lease', async () => {
  const f = fixture(); await f.session.enable();
  const call = f.model.call.bind(f.model);
  f.model.call = async (action, token) => action === 'renew' ? f.model.reply(false, 'lease_error') : call(action, token);
  for (const callback of f.timers) callback();
  await until(() => f.session.getState().phase === 'off');
  assert.equal(f.session.getState().code, 'lease_lost'); assert.equal(f.captured.track.stopped, 1);
  assert.equal(f.model.jack, 0);
});

test('already-ended capture is rejected before claim; ended during attach cannot commit', async () => {
  const dead = stream({ ended: true }), f = fixture({ capture: async () => dead });
  await f.session.enable(); assert.equal(f.session.getState().code, 'capture_ended');
  assert.deepEqual(f.model.calls.map(c => c[0]), ['status']); assert.equal(dead.track.stopped, 1);
  const model = new LeaseModel(), live = stream(), gate = deferred(); let attached = false;
  const g = fixture({ model, capture: async () => live, attach: () => { attached = true; return gate.promise; } });
  const enabling = g.session.enable(); await until(() => attached);
  live.track.end(); gate.resolve(() => {}); await enabling;
  await until(() => g.session.getState().phase === 'off');
  assert.equal(model.calls.some(c => c[0] === 'commit'), false); assert.equal(model.jack, 0);
});

test('dispose cancels pending capture and suppresses all later callbacks', async () => {
  const pending = deferred(), late = stream(); let capturing = false;
  const f = fixture({ capture: () => { capturing = true; return pending.promise; } });
  const enabling = f.session.enable(); await until(() => capturing);
  const count = f.states.length; await f.session.dispose(); pending.resolve(late); await enabling;
  assert.equal(late.track.stopped, 1); assert.equal(f.states.length, count); assert.equal(f.model.jack, 0);
  await f.session.enable(); assert.equal(f.model.calls.length, 1);
});

test('attach failure releases reservation and never commits', async () => {
  const f = fixture({ attach: () => { throw new Error('addTrack failed'); } });
  await f.session.enable(); assert.equal(f.model.jack, 0); assert.equal(f.model.active, null);
  assert.equal(f.captured.track.stopped, 1); assert.equal(f.session.getState().code, 'addTrack failed');
  assert.equal(f.model.calls.some(c => c[0] === 'commit'), false);
});

test('authenticated transport emits only whitelisted command arguments and validates responses', async () => {
  const model = new LeaseModel(), sent = [];
  const transport = createRunScriptTransport(async (...args) => { sent.push(args); return { code: 0, data: { log: JSON.stringify(model.reply(true, 'status')) } }; });
  await transport.status();
  assert.deepEqual(sent[0], ['/api/vm/script/run', { name: 'mic-lease.sh status', type: 'foreground' }, { timeout: 3000 }]);
  await transport.call('claim', { owner: OWNER_A, seq: 7, bootEpoch: EPOCH_A });
  assert.equal(sent[1][1].name, `mic-lease.sh claim ${OWNER_A} 7 ${EPOCH_A}`);
  for (const token of [{ owner: 'a; id', seq: 1, bootEpoch: EPOCH_A }, { owner: OWNER_A, seq: NaN, bootEpoch: EPOCH_A },
    { owner: OWNER_A, seq: 1, bootEpoch: '../bad' }]) await assert.rejects(transport.call('claim', token), /invalid_lease_arguments/);
  await assert.rejects(transport.call('shell', { owner: OWNER_A, seq: 1, bootEpoch: EPOCH_A }), /invalid_lease_arguments/);
  assert.equal(sent.length, 2);
  for (const patch of [{ version: 2 }, { jack: undefined }, { phase: 'something' }, { bootEpoch: '' }, { leaseSeq: 0 }])
    assert.throws(() => validateReply({ ...model.reply(true, 'status'), ...patch }), /invalid_lease_reply/);
  await assert.rejects(createRunScriptTransport(async () => ({ code: -1 })).status(), /script_request_failed/);
  await assert.rejects(createRunScriptTransport(async () => ({ code: 0, data: { log: ' '.repeat(8193) } })).status(), /script_request_failed/);
  await assert.rejects(createRunScriptTransport(async () => ({ code: 0, data: { log: 'non-json' } })).status());
});
