import assert from 'node:assert/strict';
import { useMicBridge } from './react-adapter.mjs';

globalThis.window = new EventTarget();
globalThis.WebSocket = { OPEN: 1 };
let nextCapture;
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: {
  mediaDevices: { getUserMedia: () => nextCapture() }
} });
const epoch = 'b'.repeat(32);
const flush = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };

function stream() {
  const track = new EventTarget();
  track.readyState = 'live'; track.stops = 0;
  track.stop = () => { track.readyState = 'ended'; track.stops++; };
  return { track, getTracks: () => [track], getAudioTracks: () => [track] };
}
function peers() {
  const audio = new EventTarget();
  audio.connectionState = 'new'; audio.signalingState = 'stable'; audio.senders = new Set();
  audio.addTrack = track => { const sender = { track }; audio.senders.add(sender); return sender; };
  audio.removeTrack = sender => audio.senders.delete(sender);
  audio.state = state => { audio.connectionState = state; audio.dispatchEvent(new Event('connectionstatechange')); };
  const ws = new EventTarget(); ws.readyState = 1;
  ws.close = () => { ws.readyState = 3; ws.dispatchEvent(new Event('close')); };
  return { audio, ws };
}
function harness(initial = false) {
  let enabled = initial, state = null, cursor = 0;
  const slots = [], pending = [], requests = [], states = [];
  let phase = 'idle', jack = 0, leaseSeq = null;
  const React = {
    useRef(value) { const n = cursor++; return slots[n] ??= { current: value }; },
    useEffect(fn, deps) {
      const n = cursor++, old = slots[n];
      if (!old || deps.some((v, i) => v !== old.deps[i])) {
        slots[n] = { deps, cleanup: old?.cleanup };
        pending.push(() => { slots[n].cleanup?.(); slots[n].cleanup = fn(); });
      }
    }
  };
  const setEnabled = value => { enabled = value; };
  const setState = value => { state = value; states.push(value); };
  const post = async (_path, body) => {
    requests.push(body.name);
    const [, action, , seq] = body.name.split(' ');
    if (action === 'claim') { phase = 'reserved'; jack = 0; leaseSeq = Number(seq); }
    if (action === 'commit' || action === 'renew') { phase = 'active'; jack = 1; leaseSeq = Number(seq); }
    if (action === 'release') { phase = 'idle'; jack = 0; leaseSeq = null; }
    const code = { status: 'status', claim: 'claimed', commit: 'committed', renew: 'renewed', release: 'released' }[action];
    return { code: 0, data: { log: JSON.stringify({ version: 1, ok: true, code, phase, jack, ttlMs: 10000, leaseSeq, bootEpoch: epoch }) } };
  };
  function render() {
    cursor = 0;
    const adapter = useMicBridge(React, enabled, setEnabled, setState, post);
    pending.splice(0).forEach(f => f());
    return adapter;
  }
  return {
    render, setEnabled, requests, states,
    get enabled() { return enabled; }, get state() { return state; },
    unmount() { for (const slot of slots) slot?.cleanup?.(); }
  };
}

{
  const h = harness(), p = peers();
  h.render().ready(p.audio, p.ws);
  await flush();
  assert.equal(h.requests.length, 0, 'idle page must not send global off or status');
  h.unmount();
}
{
  const h = harness(true), p = peers(), s = stream();
  nextCapture = async () => s;
  h.render().ready(p.audio, p.ws);
  await flush();
  assert.equal(h.enabled, true, 'first ready must preserve an early enable intent');
  assert.equal(h.state.phase, 'on');
  h.setEnabled(false); h.render(); await flush();
  assert.equal(s.track.readyState, 'ended');
  assert.equal(p.audio.senders.size, 0);
  assert.equal(h.state.phase, 'off');
  h.unmount();
}
{
  const h = harness(true), p = peers(), s = stream();
  nextCapture = async () => s;
  h.render().ready(p.audio, p.ws); await flush();
  p.ws.close(); await flush();
  assert.equal(h.enabled, false);
  assert.equal(s.track.readyState, 'ended');
  assert.equal(p.audio.senders.size, 0);
  assert.equal(h.state.phase, 'off', 'disconnect must not leave the toolbar green');
  h.unmount();
}
{
  const h = harness(true), p = peers(), s = stream();
  nextCapture = async () => s;
  h.render().ready(p.audio, p.ws); await flush();
  p.audio.state('disconnected'); await flush();
  const count = h.requests.length;
  p.audio.state('connected'); await flush();
  assert.equal(h.enabled, false);
  assert.equal(h.requests.length, count, 'connection recovery must not automatically reopen the mic');
  h.unmount();
}
{
  const h = harness(true), p = peers(), s = stream();
  let resolveCapture;
  nextCapture = () => new Promise(resolve => { resolveCapture = resolve; });
  h.render().ready(p.audio, p.ws); await flush();
  h.unmount();
  const count = h.states.length;
  resolveCapture(s); await flush();
  assert.equal(s.track.readyState, 'ended', 'late permission success after unmount must stop its stream');
  assert.equal(h.requests.some(v => v.includes(' claim ')), false);
  assert.equal(h.states.length, count);
}
console.log('PASS: 5 React-adapter lifecycle fixture cases (no real browser or device).');
