// Offline draft. No global browser hooks, network defaults, or device access.
export type LeaseAction = 'claim' | 'commit' | 'renew' | 'release';
export interface LeaseToken { owner: string; seq: number; bootEpoch: string }
export interface LeaseReply {
  version: 1; ok: boolean; code: string;
  phase: 'idle' | 'reserved' | 'active' | 'fault';
  jack: 0 | 1 | null; ttlMs: number; leaseSeq: number | null; bootEpoch: string;
}
export interface LeaseTransport {
  status(): Promise<LeaseReply>;
  call(action: LeaseAction, token: LeaseToken): Promise<LeaseReply>;
}
export type RunScriptPost = (path: string, body: { name: string; type: 'foreground' },
  config: { timeout: number }) => Promise<unknown>;
const hex32 = /^[0-9a-f]{32}$/;
const actions = new Set(['claim', 'commit', 'renew', 'release']);

export function validateReply(value: unknown): LeaseReply {
  const r = value as Partial<LeaseReply> | null;
  if (!r || r.version !== 1 || typeof r.ok !== 'boolean' || typeof r.code !== 'string' ||
      !['idle', 'reserved', 'active', 'fault'].includes(r.phase ?? '') ||
      ![0, 1, null].includes(r.jack as 0 | 1 | null) ||
      typeof r.ttlMs !== 'number' || !Number.isFinite(r.ttlMs) || r.ttlMs < 0 ||
      !(r.leaseSeq === null || (Number.isSafeInteger(r.leaseSeq) && Number(r.leaseSeq) > 0)) ||
      typeof r.bootEpoch !== 'string' || !hex32.test(r.bootEpoch)) {
    throw new Error('invalid_lease_reply');
  }
  return r as LeaseReply;
}

// Inject the application's existing authenticated http.post. The actual 1.2.15
// RunScript Name parsing must match the audited sh -c implementation.
export function createRunScriptTransport(post: RunScriptPost): LeaseTransport {
  async function request(name: string): Promise<LeaseReply> {
    const outer = await post('/api/vm/script/run', { name, type: 'foreground' }, { timeout: 3000 }) as
      { code?: number; data?: { log?: unknown } };
    if (!outer || outer.code !== 0 || typeof outer.data?.log !== 'string' || outer.data.log.length > 8192) {
      throw new Error('script_request_failed');
    }
    return validateReply(JSON.parse(outer.data.log.trim()));
  }
  return {
    status: () => request('mic-lease.sh status'),
    call(action, token) {
      if (!actions.has(action) || !hex32.test(token.owner) || !hex32.test(token.bootEpoch) ||
          !Number.isSafeInteger(token.seq) || token.seq < 1) {
        return Promise.reject(new Error('invalid_lease_arguments'));
      }
      return request(`mic-lease.sh ${action} ${token.owner} ${token.seq} ${token.bootEpoch}`);
    }
  };
}

export interface MicTrack {
  readonly readyState?: string;
  stop(): void;
  addEventListener?(name: 'ended', listener: () => void): void;
  removeEventListener?(name: 'ended', listener: () => void): void;
}
export interface MicStream { getTracks(): MicTrack[]; getAudioTracks(): MicTrack[] }
export interface MicState {
  phase: 'off' | 'starting' | 'on' | 'closing' | 'error';
  code: string; jack: 0 | 1 | null;
}
interface Activation<S> {
  seq: number; epoch: string | null; claimed: boolean; canceled: boolean;
  stream: S | null; stopped: boolean; detach: (() => void) | null;
  ended: (() => void) | null; release: Promise<LeaseReply> | null;
  cancelRenew: (() => void) | null; renewing: boolean;
}
export interface MicOptions<S extends MicStream> {
  // New owner per page/PeerConnection session. Never reuse this controller with
  // a replacement audio PeerConnection; dispose it and create a fresh instance.
  owner: string;
  lease: LeaseTransport;
  capture(): Promise<S>;
  // Must return a detacher bound to this exact stream/sender, not a mutable ref.
  attach(stream: S): (() => void) | Promise<() => void>;
  onState?(state: MicState): void;
  every?(milliseconds: number, callback: () => void): () => void;
}

class Canceled extends Error {}
export class MicSession<S extends MicStream> {
  private options: MicOptions<S>;
  private current: Activation<S> | null = null;
  private sequence = 0;
  private revision = 0;
  private disposed = false;
  private lastRelease: Promise<LeaseReply> | null = null;
  private state: MicState = { phase: 'off', code: 'initial', jack: null };

  constructor(options: MicOptions<S>) {
    if (!hex32.test(options.owner)) throw new Error('invalid_owner');
    this.options = options;
  }
  getState(): MicState { return { ...this.state }; }
  private publish(state: MicState): void {
    this.state = state;
    if (!this.disposed) this.options.onState?.({ ...state });
  }
  private check(a: Activation<S>): void {
    if (this.disposed || a.canceled || this.current !== a) throw new Canceled();
  }
  private checkTracks(a: Activation<S>): void {
    const tracks = a.stream?.getAudioTracks() ?? [];
    if (tracks.length === 0) throw new Error('no_audio_track');
    if (tracks.some(t => t.readyState === 'ended')) throw new Error('capture_ended');
  }
  private token(a: Activation<S>): LeaseToken {
    if (!a.epoch) throw new Error('missing_boot_epoch');
    return { owner: this.options.owner, seq: a.seq, bootEpoch: a.epoch };
  }
  private accepted(a: Activation<S>, r: LeaseReply, phase: 'reserved' | 'active'): void {
    if (!r.ok || r.bootEpoch !== a.epoch || r.leaseSeq !== a.seq || r.phase !== phase ||
        r.jack !== (phase === 'active' ? 1 : 0)) throw new Error(r.code || 'lease_not_confirmed');
  }
  private cleanMedia(a: Activation<S>): void {
    a.cancelRenew?.(); a.cancelRenew = null;
    if (a.stream && a.ended) {
      for (const t of a.stream.getAudioTracks()) t.removeEventListener?.('ended', a.ended);
      a.ended = null;
    }
    // Privacy stop is synchronous and happens before awaiting remote release.
    if (a.stream && !a.stopped) {
      a.stopped = true;
      for (const t of a.stream.getTracks()) { try { t.stop(); } catch { /* continue */ } }
    }
    const detach = a.detach; a.detach = null;
    try { detach?.(); } catch { /* local track is already stopped */ }
  }
  private release(a: Activation<S>): Promise<LeaseReply> | null {
    // Do not send global off from an untouched/default-false page.
    if (!a.epoch || !a.claimed) return null;
    // Normalize a synchronous transport failure into the async cleanup path.
    if (!a.release) {
      a.release = Promise.resolve().then(() => this.options.lease.call('release', this.token(a)));
      this.lastRelease = a.release;
    }
    return a.release;
  }

  async enable(): Promise<void> {
    if (this.disposed || this.current) return;
    if (!Number.isSafeInteger(this.sequence + 1)) throw new Error('sequence_exhausted');
    const a: Activation<S> = { seq: ++this.sequence, epoch: null, claimed: false, canceled: false,
      stream: null, stopped: false, detach: null, ended: null, release: null,
      cancelRenew: null, renewing: false };
    this.current = a;
    const previousRelease = this.lastRelease;
    ++this.revision;
    this.publish({ phase: 'starting', code: 'starting', jack: null });
    try {
      const status = validateReply(await this.options.lease.status());
      this.check(a);
      if (!status.ok || status.phase === 'fault' || status.jack === null) throw new Error(status.code);
      a.epoch = status.bootEpoch;
      a.stream = await this.options.capture();
      this.check(a);
      this.checkTracks(a);
      a.ended = () => { if (this.current === a) void this.disable('capture_ended'); };
      for (const t of a.stream.getAudioTracks()) t.addEventListener?.('ended', a.ended);
      // Do not let a quick new claim race ahead of the preceding release.
      // The transport must bound request time (the adapter uses 3 seconds).
      if (previousRelease) {
        try { await previousRelease; } catch { /* let the daemon decide if claim is now safe */ }
        this.check(a); this.checkTracks(a);
      }
      a.claimed = true; // Set before await, so disable can send the tombstone first.
      const claim = validateReply(await this.options.lease.call('claim', this.token(a)));
      this.check(a); this.accepted(a, claim, 'reserved');
      a.detach = await this.options.attach(a.stream);
      this.check(a);
      if (typeof a.detach !== 'function') throw new Error('invalid_media_detacher');
      this.checkTracks(a);
      const commit = validateReply(await this.options.lease.call('commit', this.token(a)));
      this.check(a); this.accepted(a, commit, 'active');
      this.checkTracks(a);
      this.publish({ phase: 'on', code: commit.code, jack: 1 });
      const every = this.options.every ?? ((ms, fn) => {
        const timer = setInterval(fn, ms); return () => clearInterval(timer);
      });
      a.cancelRenew = every(2000, () => { void this.renew(a); });
    } catch (error) {
      this.cleanMedia(a);
      const wasCurrent = this.current === a;
      if (wasCurrent) this.current = null;
      const revision = this.revision;
      let jack: 0 | 1 | null = null;
      try {
        const pending = this.release(a);
        if (pending) {
          const r = validateReply(await pending);
          if (r.ok && r.code === 'released' && r.bootEpoch === a.epoch) jack = r.jack;
        }
      } catch { /* lease TTL remains the remote-close backstop */ }
      if (wasCurrent && this.revision === revision && !(error instanceof Canceled)) {
        this.publish({ phase: 'error', code: error instanceof Error ? error.message : 'enable_failed',
          jack });
      }
    }
  }

  async disable(code = 'disabled'): Promise<void> {
    const a = this.current;
    if (!a) return;
    const revision = ++this.revision;
    a.canceled = true; this.current = null;
    this.cleanMedia(a);
    const pending = this.release(a);
    this.publish({ phase: pending ? 'closing' : 'off', code, jack: null });
    if (!pending) return;
    try {
      const r = validateReply(await pending);
      // A later owner can already be active; successful release acknowledges
      // only this activation. Do not require globally observed jack === 0.
      if (!r.ok || r.code !== 'released' || r.bootEpoch !== a.epoch || r.jack === null) throw new Error(r.code);
      if (this.revision === revision) this.publish({ phase: 'off', code, jack: r.jack });
    } catch {
      if (this.revision === revision) this.publish({ phase: 'error', code: 'remote_close_unconfirmed', jack: null });
    }
  }
  private async renew(a: Activation<S>): Promise<void> {
    if (this.current !== a || a.renewing || a.canceled) return;
    a.renewing = true;
    try {
      const reply = validateReply(await this.options.lease.call('renew', this.token(a)));
      this.check(a); this.accepted(a, reply, 'active');
    } catch (error) {
      if (!(error instanceof Canceled) && this.current === a) await this.disable('lease_lost');
    } finally { a.renewing = false; }
  }
  async dispose(): Promise<void> {
    this.disposed = true;
    await this.disable('session_closed');
    // A manual disable may already have cleared current while its release ACK
    // is pending. Let its earlier continuation publish the final internal state
    // before an adapter reads getState() after disposal.
    if (this.lastRelease) {
      try { await this.lastRelease; } catch { /* disable records unconfirmed close */ }
    }
  }
}
