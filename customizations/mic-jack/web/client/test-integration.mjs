import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';

// Execute exact extracted compiled components with injected React/media/network
// stand-ins. No real browser, device, microphone, or external network is used.
if (!process.env.NANOKVM_CANDIDATE_WEB) {
  test('compiled component and graph fixtures', {
    skip: 'set NANOKVM_CANDIDATE_WEB to explicit output from build-web-candidate.py'
  }, () => {});
} else {
const candidate = path.resolve(process.env.NANOKVM_CANDIDATE_WEB);
const { useMicBridge } = await import(pathToFileURL(path.join(candidate, 'assets/mic-react-20261015.mjs')).href);
const desktopPath = path.join(candidate, 'assets/index-Bpc3D8Fr-mic20261015.js');
const desktop = fs.readFileSync(desktopPath, 'utf8');
function extract(name, boundary) {
  const start = desktop.indexOf(`${name}=()=>{`), end = desktop.indexOf(boundary, start);
  assert(start >= 0 && end > start, `missing exact extraction boundary ${name}`);
  return desktop.slice(start + name.length + 1, end);
}
const functions = { b7: extract('b7', ',x7='), w7: extract('w7', ',S7='), NE: extract('NE', ',_r=') };
const sha = text => crypto.createHash('sha256').update(text).digest('hex');
const epoch = 'e'.repeat(32);
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };

function harness(t, initialEnabled = false, mode = 'h264-webrtc') {
  const atoms = { mn: Symbol(), _r: Symbol(), $h: Symbol(), ac: Symbol(), oi: Symbol(), __nkmicState: Symbol(), da: Symbol() };
  const values = new Map([[atoms.mn, { scale: 1 }], [atoms._r, 'cursor-default'], [atoms.ac, 0],
    [atoms.oi, initialEnabled], [atoms.da, mode], [atoms.__nkmicState, { phase: 'off', code: 'initial', jack: null }]]);
  const components = new Set(), dirty = new Set(), sockets = [], peers = [], streams = [], requests = [], timers = new Set();
  let rendering = null, capture = async () => makeStream(), lease = null, jack = 0;
  const terminals = new Map(), replyGates = new Map();
  const states = [];
  function write(atom, value) {
    const old = values.get(atom); const next = typeof value === 'function' ? value(old) : value;
    if (Object.is(old, next)) return;
    values.set(atom, next);
    if (atom === atoms.__nkmicState) states.push(next);
    for (const component of components) if (component.mounted) dirty.add(component);
  }
  const React = {
    useRef(value) { const n = rendering.cursor++; return rendering.slots[n] ??= { current: value }; },
    useState(value) {
      const owner = rendering, n = owner.cursor++;
      if (!(n in owner.slots)) owner.slots[n] = { value };
      return [owner.slots[n].value, next => {
        if (!owner.mounted) return;
        const updated = typeof next === 'function' ? next(owner.slots[n].value) : next;
        if (!Object.is(updated, owner.slots[n].value)) { owner.slots[n].value = updated; dirty.add(owner); }
      }];
    },
    useEffect(fn, deps) {
      const n = rendering.cursor++, old = rendering.slots[n];
      if (!old || deps.length !== old.deps.length || deps.some((value, i) => value !== old.deps[i])) {
        const slot = { deps, cleanup: old?.cleanup };
        rendering.slots[n] = slot;
        rendering.effects.push(() => { slot.cleanup?.(); slot.cleanup = fn(); });
      }
    }
  };
  function makeStream() {
    const track = new EventTarget(); track.readyState = 'live'; track.stops = 0; track.kind = 'audio';
    track.stop = () => { track.readyState = 'ended'; track.stops++; };
    const stream = { track, getTracks: () => [track], getAudioTracks: () => [track] };
    streams.push(stream); return stream;
  }
  class FakeWS extends EventTarget {
    static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
    readyState = 0; closeCalls = 0; sent = [];
    constructor() { super(); sockets.push(this); }
    send(data) { this.sent.push(data); }
    open() { this.readyState = 1; this.onopen?.(new Event('open')); this.dispatchEvent(new Event('open')); }
    close() { this.closeCalls++; this.readyState = 3; this.onclose?.(new Event('close')); this.dispatchEvent(new Event('close')); }
    queuedOpenCallback() { this.onopen?.(new Event('open')); }
  }
  class FakePC extends EventTarget {
    connectionState = 'new'; signalingState = 'stable'; iceConnectionState = 'new'; senders = new Set(); transceivers = [];
    constructor() { super(); peers.push(this); }
    addTransceiver(kind, options) {
      assert.notEqual(this.signalingState, 'closed', 'late callback touched a closed PeerConnection');
      const transceiver = { kind, options, setCodecPreferences() {} }; this.transceivers.push(transceiver); return transceiver;
    }
    addTrack(track, stream) { assert.notEqual(this.signalingState, 'closed'); const sender = { track, stream }; this.senders.add(sender); return sender; }
    removeTrack(sender) { this.senders.delete(sender); }
    close() { this.connectionState = 'closed'; this.signalingState = 'closed'; this.dispatchEvent(new Event('connectionstatechange')); }
    state(next) { this.connectionState = next; this.dispatchEvent(new Event('connectionstatechange')); }
    async createOffer() { return { type: 'offer', sdp: 'mock' }; }
    async setLocalDescription(value) { this.localDescription = value; }
  }
  const browserWindow = new EventTarget();
  const original = { window: globalThis.window, WebSocket: globalThis.WebSocket, navigator: Object.getOwnPropertyDescriptor(globalThis, 'navigator'),
    setInterval: globalThis.setInterval, clearInterval: globalThis.clearInterval };
  globalThis.window = browserWindow; globalThis.WebSocket = FakeWS;
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { mediaDevices: { getUserMedia: () => capture() } } });
  const addTimer = () => { const timer = {}; timers.add(timer); return timer; };
  const removeTimer = timer => timers.delete(timer);
  globalThis.setInterval = addTimer; globalThis.clearInterval = removeTimer;
  const reply = (ok, code) => ({ version: 1, ok, code, phase: lease?.phase ?? 'idle', jack, ttlMs: lease ? 10000 : 0,
    leaseSeq: lease?.seq ?? null, bootEpoch: epoch });
  async function post(url, body, config) {
    assert.equal(url, '/api/vm/script/run'); assert.equal(config.timeout, 3000);
    requests.push(body.name);
    const [wrapper, action, owner, rawSeq, bootEpoch] = body.name.split(' '), seq = Number(rawSeq);
    assert.equal(wrapper, 'mic-lease.sh');
    let result;
    if (action === 'status') result = reply(true, 'status');
    else if (bootEpoch !== epoch) result = reply(false, 'epoch_mismatch');
    else if (action === 'release') {
      terminals.set(owner, Math.max(terminals.get(owner) ?? 0, seq));
      if (lease?.owner === owner && lease.seq <= seq) { lease = null; jack = 0; }
      result = reply(true, 'released');
    } else if (seq <= (terminals.get(owner) ?? 0)) result = reply(false, 'stale');
    else if (lease && (lease.owner !== owner || lease.seq !== seq)) result = reply(false, 'busy');
    else if (action === 'claim') { lease ??= { owner, seq, phase: 'reserved' }; result = reply(true, 'claimed'); }
    else if (action === 'commit') { assert(lease); lease.phase = 'active'; jack = 1; result = reply(true, 'committed'); }
    else if (action === 'renew') result = reply(Boolean(lease), lease ? 'renewed' : 'not_owner');
    else assert.fail(`unexpected action ${action}`);
    const gate = replyGates.get(action);
    if (gate) { replyGates.delete(action); await gate.promise; }
    return { code: 0, data: { log: JSON.stringify(result) } };
  }
  const jsx = (type, props) => ({ type, props });
  const scope = { ...atoms, i: React, Pt: atom => values.get(atom), Ct: atom => [values.get(atom), next => write(atom, next)],
    Xt: atom => next => write(atom, next), s: { jsx, jsxs: jsx }, vt: (...args) => args.filter(Boolean).join(' '),
    mr: { Normal: 1, NoImage: -1, InconsistentVideoMode: -4 }, la: 'Spinner', rn: 'Tooltip', Fh: 'MicOn', t5: 'MicOff',
    gf: () => () => {}, vf: () => () => {}, yf: () => () => {}, bf: () => () => {},
    HE: () => new FakeWS(), KE: () => new FakeWS(), RTCPeerConnection: FakePC, WebSocket: FakeWS,
    RTCRtpReceiver: { getCapabilities: () => ({ codecs: [{ mimeType: 'video/H265' }] }) },
    MediaStream: class {}, console, setInterval: addTimer, clearInterval: removeTimer, setTimeout: addTimer, clearTimeout: removeTimer,
    __nkmicHook: useMicBridge, lt: { post }, Ie: () => ({ t: key => key }) };
  function execute(component) {
    rendering = component; component.cursor = 0; component.effects = [];
    component.tree = component.fn(); rendering = null;
    for (const effect of component.effects) effect();
  }
  function mount(name) {
    const component = { name, slots: [], effects: [], cursor: 0, mounted: true,
      fn: new Function(...Object.keys(scope), `return (${functions[name]});`)(...Object.values(scope)) };
    components.add(component); execute(component); return component;
  }
  function unmount(component) {
    component.mounted = false; dirty.delete(component);
    for (const slot of component.slots) slot?.cleanup?.();
  }
  async function flush() {
    for (let i = 0; i < 80; i++) {
      await Promise.resolve(); const batch = [...dirty]; dirty.clear();
      for (const component of batch) if (component.mounted) execute(component);
    }
  }
  t.after(async () => {
    for (const component of components) if (component.mounted) unmount(component);
    await flush();
    globalThis.window = original.window; globalThis.WebSocket = original.WebSocket;
    globalThis.setInterval = original.setInterval; globalThis.clearInterval = original.clearInterval;
    if (original.navigator) Object.defineProperty(globalThis, 'navigator', original.navigator); else delete globalThis.navigator;
  });
  return { mount, unmount, flush, requests, sockets, peers, streams, states, timers, browserWindow, makeStream,
    captureWith(fn) { capture = fn; }, setEnabled(value) { write(atoms.oi, value); }, setMode(value) { write(atoms.da, value); },
    holdReply(action) { const gate = deferred(); replyGates.set(action, gate); return gate; },
    setState(value) { write(atoms.__nkmicState, value); }, get enabled() { return values.get(atoms.oi); },
    get state() { return values.get(atoms.__nkmicState); }, get jack() { return jack; } };
}

for (const [name, mode] of [['b7', 'h264-webrtc'], ['w7', 'h265-webrtc']]) {
  test(`${name}: actual compiled ready hook accepts early intent, on ACK, and clean unmount`, async t => {
    const h = harness(t, true, mode), screen = h.mount(name);
    await h.flush(); assert.equal(h.requests.length, 0); assert.equal(h.state.phase, 'starting');
    h.sockets[0].open(); await h.flush();
    assert.equal(h.state.phase, 'on'); assert.equal(h.jack, 1); assert.equal(h.peers[1].senders.size, 1);
    h.unmount(screen); assert.equal(h.streams[0].track.readyState, 'ended'); await h.flush();
    assert.equal(h.jack, 0); assert.equal(h.enabled, false); assert.equal(h.timers.size, 0);
  });
  test(`${name}: cancel before socket ready clears pending UI and cannot capture on late open`, async t => {
    const h = harness(t, true, mode); h.mount(name); await h.flush();
    h.setEnabled(false); await h.flush();
    assert.equal(h.state.phase, 'off', 'cancel before ready must clear the spinner');
    h.sockets[0].open(); await h.flush();
    assert.equal(h.streams.length, 0); assert.equal(h.requests.length, 0);
  });
  test(`${name}: CONNECTING socket closes at unmount and queued onopen cannot touch closed PC`, async t => {
    const h = harness(t, false, mode), screen = h.mount(name), socket = h.sockets[0];
    h.unmount(screen); assert.equal(socket.closeCalls, 1, 'CONNECTING websocket must close during cleanup');
    assert.doesNotThrow(() => socket.queuedOpenCallback()); await h.flush();
    assert.equal(h.streams.length, 0); assert.equal(h.requests.length, 0);
  });
  test(`${name}: pagehide stops synchronously and never reopens on connection recovery`, async t => {
    const h = harness(t, true, mode); h.mount(name); h.sockets[0].open(); await h.flush();
    h.browserWindow.dispatchEvent(new Event('pagehide'));
    assert.equal(h.streams[0].track.readyState, 'ended'); await h.flush();
    assert.equal(h.state.phase, 'off'); assert.equal(h.enabled, false); assert.equal(h.jack, 0);
    const claims = h.requests.filter(value => value.includes(' claim ')).length;
    h.browserWindow.dispatchEvent(new Event('pageshow')); h.peers[1].state('connected'); await h.flush();
    assert.equal(h.requests.filter(value => value.includes(' claim ')).length, claims);
  });
  test(`${name}: audio connection loss while permission is pending stops late capture`, async t => {
    const h = harness(t, true, mode), capture = deferred(); h.captureWith(() => capture.promise);
    h.mount(name); h.sockets[0].open(); await h.flush(); h.peers[1].state('failed'); await h.flush();
    const late = h.makeStream(); capture.resolve(late); await h.flush();
    assert.equal(late.track.readyState, 'ended'); assert.equal(h.enabled, false); assert.equal(h.jack, 0);
    assert.equal(h.requests.some(value => value.includes(' claim ')), false);
  });
  test(`${name}: explicit click after page restore can reuse a still-open audio connection`, async t => {
    const h = harness(t, true, mode); h.mount(name); h.sockets[0].open(); await h.flush();
    h.browserWindow.dispatchEvent(new Event('pagehide')); await h.flush();
    h.browserWindow.dispatchEvent(new Event('pageshow')); h.setEnabled(true); await h.flush();
    assert.equal(h.sockets[0].readyState, 1); assert.notEqual(h.peers[1].connectionState, 'closed');
    assert.equal(h.state.phase, 'on', 'explicit re-enable after restore must not wait forever for a second WS onopen');
    assert.equal(h.jack, 1);
  });
  test(`${name}: disconnect during an in-flight manual close eventually leaves closing state`, async t => {
    const h = harness(t, true, mode); h.mount(name); h.sockets[0].open(); await h.flush();
    const gate = h.holdReply('release'); h.setEnabled(false); await h.flush();
    assert.equal(h.state.phase, 'closing'); h.sockets[0].close(); await h.flush();
    gate.resolve(); await h.flush();
    assert.equal(h.state.phase, 'off', 'dispose must await an already-pending disable acknowledgement');
    assert.equal(h.jack, 0);
  });
}

test('compiled toolbar: null/direct/MJPEG modes block intent; pending never displays confirmed green', async t => {
  const h = harness(t, false, null), toolbar = h.mount('NE');
  const button = () => toolbar.tree.props.children;
  for (const mode of [null, 'h264-direct', 'h265-direct', 'mjpeg']) {
    h.setMode(mode); await h.flush(); button().props.onClick(); assert.equal(h.enabled, false);
  }
  h.setMode('h264-webrtc'); await h.flush(); button().props.onClick(); await h.flush(); assert.equal(h.enabled, true);
  h.setState({ phase: 'starting', code: 'starting', jack: null }); await h.flush();
  assert.equal(button().props['aria-pressed'], false); assert.equal(button().props.children.type, 'Spinner');
  h.setState({ phase: 'on', code: 'committed', jack: 1 }); await h.flush();
  assert.equal(button().props['aria-pressed'], true); assert.equal(button().props.children.type, 'MicOn');
});

test('compiled codec replacement: late old capture cannot publish or claim over new screen', async t => {
  const h = harness(t, true, 'h264-webrtc'), oldCapture = deferred();
  h.captureWith(() => oldCapture.promise); const old = h.mount('b7'); h.sockets[0].open(); await h.flush();
  h.unmount(old); h.setMode('h265-webrtc'); h.captureWith(async () => h.makeStream());
  h.mount('w7'); h.sockets[1].open(); await h.flush();
  assert.equal(h.enabled, false); assert.equal(h.requests.some(value => value.includes(' claim ')), false);
  h.setEnabled(true); await h.flush(); assert.equal(h.state.phase, 'on'); assert.equal(h.jack, 1);
  const late = h.makeStream(); oldCapture.resolve(late); await h.flush();
  assert.equal(late.track.readyState, 'ended'); assert.equal(h.state.phase, 'on'); assert.equal(h.jack, 1);
});

test('offline recovery is manual; a truly closed socket reports connection loss instead of pending forever', async t => {
  const h = harness(t, true), screen = h.mount('b7'); h.sockets[0].open(); await h.flush();
  h.browserWindow.dispatchEvent(new Event('offline')); await h.flush();
  assert.equal(h.enabled, false); assert.equal(h.jack, 0);
  const claims = h.requests.filter(value => value.includes(' claim ')).length;
  h.browserWindow.dispatchEvent(new Event('online')); await h.flush();
  assert.equal(h.requests.filter(value => value.includes(' claim ')).length, claims);
  h.setEnabled(true); await h.flush(); assert.equal(h.state.phase, 'on');
  h.sockets[0].close(); await h.flush(); h.setEnabled(true); await h.flush();
  assert.equal(h.enabled, false); assert.equal(h.state.phase, 'error'); assert.equal(h.state.code, 'connection_lost');
  h.unmount(screen);
});

test('candidate module graph and hashes: no old JS references or mixed entry imports', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(candidate, 'candidate-manifest.json'), 'utf8'));
  const originals = manifest.files.filter(file => file.original_path).map(file => path.basename(file.original_path));
  const files = [...manifest.files, manifest.index];
  for (const file of files) {
    const contents = fs.readFileSync(path.join(candidate, file.path));
    assert.equal(sha(contents), file.sha256, `manifest hash mismatch ${file.path}`);
    const text = contents.toString('utf8');
    for (const old of originals) assert.equal(text.includes(old), false, `old module reference ${old} in ${file.path}`);
    for (const match of text.matchAll(/(?:\.\/|\/assets\/)([A-Za-z0-9_-]+\.(?:js|mjs))/g)) {
      assert(fs.existsSync(path.join(candidate, 'assets', match[1])), `missing candidate JS ${match[1]} from ${file.path}`);
    }
  }
  assert.equal(manifest.files.filter(file => file.only_module_references_changed).length, 14);
  console.log(JSON.stringify({ kind: 'compiled-component-mock-only', desktopSha256: sha(desktop),
    extracted: Object.fromEntries(Object.entries(functions).map(([name, text]) => [name, sha(text)])) }));
});

test('candidate non-desktop modules reverse-map to official input bytes', {
  skip: !process.env.NANOKVM_BASELINE_WEB && 'set NANOKVM_BASELINE_WEB to official extracted 1.2.15 web tree'
}, () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(candidate, 'candidate-manifest.json'), 'utf8'));
  const inverse = new Map(manifest.files.filter(file => file.original_path).map(file => [path.basename(file.path), path.basename(file.original_path)]));
  const escaped = [...inverse.keys()].map(value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const reverse = text => text.replace(new RegExp(escaped.join('|'), 'g'), name => inverse.get(name));
  const baseline = path.resolve(process.env.NANOKVM_BASELINE_WEB);
  for (const file of manifest.files.filter(file => file.only_module_references_changed)) {
    assert.equal(reverse(fs.readFileSync(path.join(candidate, file.path), 'utf8')),
      fs.readFileSync(path.join(baseline, file.original_path), 'utf8'), `non-desktop semantic delta ${file.path}`);
  }
  assert.equal(reverse(fs.readFileSync(path.join(candidate, 'index.html'), 'utf8')), fs.readFileSync(path.join(baseline, 'index.html'), 'utf8'));
});
}
