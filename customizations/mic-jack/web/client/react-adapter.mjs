// Local adapter for the audited 1.2.15 desktop chunk. No global API replacement.
import { MicSession, createRunScriptTransport } from './mic-lease.mjs';

function randomOwner() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
}

export function useMicBridge(React, enabled, setEnabled, setState, post) {
  const ref = React.useRef(null);
  if (!ref.current) {
    ref.current = { alive: false, desired: enabled, session: null, binding: null, revision: 0, everReady: false };
  }
  const current = ref.current;
  current.desired = enabled;

  function stop(reason) {
    const session = current.session;
    current.session = null;
    current.desired = false;
    const revision = ++current.revision;
    setEnabled(false);
    if (session) {
      if (current.alive) setState({ phase: 'closing', code: reason, jack: null });
      void session.dispose().then(() => {
        if (current.alive && current.revision === revision && !current.session) {
          setState(session.getState());
        }
      });
    } else if (current.alive) setState({ phase: 'off', code: reason, jack: null });
  }

  function unbind(reason) {
    const binding = current.binding;
    current.binding = null;
    if (binding) {
      binding.ws.removeEventListener('close', binding.lost);
      binding.ws.removeEventListener('error', binding.lost);
      binding.audio.removeEventListener('connectionstatechange', binding.audioState);
    }
    stop(reason);
  }

  function makeSession(binding) {
    if (!current.alive || current.binding !== binding || current.session) return;
    const audio = binding.audio;
    if (binding.ws.readyState !== WebSocket.OPEN || ['failed', 'closed', 'disconnected'].includes(audio.connectionState)) return;
    const session = new MicSession({
      owner: randomOwner(),
      lease: createRunScriptTransport(post),
      capture: () => navigator.mediaDevices.getUserMedia({
        audio: { sampleRate: 48000, channelCount: 2, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      }),
      attach(stream) {
        if (current.binding !== binding || current.session !== session || audio.connectionState === 'closed') {
          throw new Error('session_closed');
        }
        const track = stream.getAudioTracks()[0];
        if (!track || track.readyState !== 'live') throw new Error('capture_ended');
        const sender = audio.addTrack(track, stream);
        return () => { if (audio.signalingState !== 'closed') audio.removeTrack(sender); };
      },
      onState(state) {
        if (!current.alive || current.session !== session) return;
        setState(state);
        if (state.phase === 'error' || state.phase === 'off') setEnabled(false);
      }
    });
    ++current.revision;
    current.session = session;
    if (current.desired) void session.enable();
  }

  function ready(audio, ws) {
    if (!current.alive) return;
    if (current.binding || current.session) unbind('session_replaced');
    const binding = {
      audio, ws,
      lost: () => unbind('connection_lost'),
      audioState: null
    };
    binding.audioState = () => {
      if (current.binding !== binding) return;
      if (['failed', 'closed', 'disconnected'].includes(audio.connectionState)) {
        stop('connection_lost');
      } else if (audio.connectionState === 'connected' && current.desired) makeSession(binding);
    };
    current.binding = binding;
    current.everReady = true;
    ws.addEventListener('close', binding.lost);
    ws.addEventListener('error', binding.lost);
    audio.addEventListener('connectionstatechange', binding.audioState);
    makeSession(binding);
  }

  React.useEffect(() => {
    current.alive = true;
    const leave = () => stop('page_closed');
    const offline = () => stop('network_offline');
    window.addEventListener('pagehide', leave);
    window.addEventListener('offline', offline);
    return () => {
      current.alive = false;
      window.removeEventListener('pagehide', leave);
      window.removeEventListener('offline', offline);
      unbind('session_closed');
    };
  }, []);

  React.useEffect(() => {
    const session = current.session;
    if (!enabled) {
      if (session) void session.disable();
      else if (!current.everReady) setState({ phase: 'off', code: 'disabled', jack: null });
    } else if (session) {
      void session.enable();
    } else {
      if (current.binding) makeSession(current.binding);
      if (!current.session) {
        if (current.everReady) {
          setState({ phase: 'error', code: 'connection_lost', jack: null });
          setEnabled(false);
        } else setState({ phase: 'starting', code: 'waiting_for_connection', jack: null });
      }
    }
  }, [enabled]);

  return { ready, close: () => unbind('session_closed'), isAlive: () => current.alive };
}
