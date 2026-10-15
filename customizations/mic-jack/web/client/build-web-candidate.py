"""Reproduce the pinned 1.2.15 web module graph offline; never contacts a KVM.

The official extracted web tree is an explicit input, not a checked-in binary.
The output directory must not exist. This is an exact-release reproduction tool,
not a general patcher for newer NanoKVM releases.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--baseline-web', type=Path, required=True,
                    help='official 1.2.15 extracted rootfs/kvmapp/server/web directory')
parser.add_argument('--output', type=Path, required=True, help='new candidate-web directory')
parser.add_argument('--anchors', type=Path, default=HERE.parent / 'baseline/asset-anchor-manifest.json',
                    help='anchor inventory (defaults to the checked-in 1.2.15 inventory)')
args = parser.parse_args()
WEB = args.baseline_web.resolve()
OUT = args.output.absolute()


def require(ok, message):
    if not ok:
        parser.error(message)


require(not OUT.exists() and not OUT.is_symlink(), 'output directory must not already exist')
OUT = OUT.resolve()
require(WEB.is_dir(), 'baseline web directory does not exist')
require(WEB != OUT and WEB not in OUT.parents, 'output must be outside the input web tree')
anchors_doc = json.loads(args.anchors.read_text(encoding='utf-8'))
anchors = {x['label']: x['exact_text'] for x in anchors_doc['anchors']}
sha = lambda b: hashlib.sha256(b).hexdigest()
pinned_manifest = json.loads((HERE.parent / 'install/manifest.json').read_text(encoding='utf-8'))
baseline_hashes = pinned_manifest['expected']['web_baseline']
actual_js = {'assets/' + p.name for p in (WEB / 'assets').glob('*.js')}
require(actual_js == set(baseline_hashes) - {'index.html'}, 'unexpected original JavaScript file set')
for relative, expected in baseline_hashes.items():
    path = WEB / relative
    require(path.is_file() and not path.is_symlink(), 'missing or linked baseline file: ' + relative)
    require(sha(path.read_bytes()) == expected, 'baseline hash mismatch: ' + relative)
for entry in anchors_doc['files']:
    require(sha((WEB / entry['path']).read_bytes()) == entry['sha256'], 'anchor hash mismatch: ' + entry['path'])

desktop_name = 'index-Bpc3D8Fr.js'
entry_name = 'index-D8MmPk1r.js'
desktop = (WEB / 'assets' / desktop_name).read_text(encoding='utf-8')
edits = []

def once(old, new, label):
    global desktop
    require(desktop.count(old) == 1, 'anchor count mismatch: ' + label)
    desktop = desktop.replace(old, new)
    edits.append({'label': label, 'before_sha256': sha(old.encode()), 'after_sha256': sha(new.encode())})

require('__nkmic' not in desktop, 'baseline already contains a microphone bridge')
desktop = 'import {useMicBridge as __nkmicHook} from "./mic-react-20261015.mjs";\n' + desktop
for codec, desired, setter, peer, ws, factory in [
    ('h264', 'o', 'c', 'O', 'M', 'HE'), ('h265', 'a', 'o', 'M', 'S', 'KE')
]:
    tail = anchors[f'{codec}_ref_declaration_tail']
    prefix, rest = tail.split(';return ', 1)
    once(tail, prefix + f';const __nkmic=__nkmicHook(i,{desired},{setter},Xt(__nkmicState),(p,b,c)=>lt.post(p,b,c));return ' + rest,
         codec + '_local_hook')
    ready = anchors[f'{codec}_ws_open_ready_tail']
    once(ready, ready + f',__nkmic.ready({peer},{ws})', codec + '_ready')
    once(f'{ws}.onopen=()=>{{', f'{ws}.onopen=()=>{{if(!__nkmic.isAlive()){{{ws}.close();return}}', codec + '_late_open_guard')
    cleanup = anchors[f'{codec}_cleanup_prefix']
    new_cleanup = cleanup.replace('return()=>{', 'return()=>{__nkmic.close(),', 1)
    new_cleanup = new_cleanup.replace(f'{ws}.readyState===WebSocket.OPEN', f'{ws}.readyState<=WebSocket.OPEN')
    once(cleanup, new_cleanup, codec + '_cleanup')
    effect = anchors[f'{codec}_mic_effect_replace_whole']
    once(effect + ',', '', codec + '_remove_original_capture_effect')

toolbar_start = desktop.index('NE=()=>{const{t:e}=Ie(),[t,n]=Ct(oi),')
toolbar_end = desktop.index(',_r=kn("cursor-default")', toolbar_start)
old_toolbar = desktop[toolbar_start:toolbar_end]
new_toolbar = '''NE=()=>{const{t:e}=Ie(),[t,n]=Ct(oi),r=Pt(__nkmicState),a=Pt(da),o=a==="h264-webrtc"||a==="h265-webrtc",c=r.phase==="starting"||r.phase==="closing",u={waiting_for_connection:"等待音频连接",connection_lost:"连接已断开，请刷新 KVM 页面",starting:"正在连接麦克风",disabled:"麦克风已关闭",remote_close_unconfirmed:"本地采集已停止，远端关闭状态待确认",lease_lost:"麦克风连接已结束",busy:"麦克风正由另一窗口使用",capture_ended:"麦克风采集已结束"},d=o?(r.phase==="on"?"麦克风已接通":u[r.code]||(r.phase==="error"?"无法开启麦克风，请检查权限和连接":e("microphone.title"))):"请先切换到 H.264/H.265 WebRTC 模式";return s.jsx(rn,{title:d+" · Mic bridge 20261015",placement:"bottom",mouseEnterDelay:.6,children:s.jsx("div",{className:vt("flex h-[30px] w-[30px] cursor-pointer items-center justify-center rounded text-neutral-300 hover:bg-neutral-700/80 hover:text-white",!o&&"opacity-50"),role:"button","aria-label":d,"aria-pressed":r.phase==="on","data-nanokvm-mic-bridge":"20261015",onClick:()=>{o&&n(!t)},children:c?s.jsx(la,{size:"small"}):r.phase==="on"?s.jsx(Fh,{size:18,className:"text-green-400"}):s.jsx(t5,{size:18,style:{color:r.phase==="error"?"#fbbf24":void 0}})})})}'''
once(old_toolbar, new_toolbar, 'toolbar_acknowledged_state')
once('oi=kn(!1),NE=', 'oi=kn(!1),__nkmicState=kn({phase:"off",code:"initial",jack:null}),NE=', 'shared_mic_state_atom')
require('navigator.mediaDevices.getUserMedia' not in desktop, 'original capture effect remained')

js_files = sorted((WEB / 'assets').glob('*.js'))
mapping = {p.name: p.stem + '-mic20261015.js' for p in js_files}
pattern = re.compile('|'.join(re.escape(n) for n in sorted(mapping, key=len, reverse=True)))
reverse = re.compile('|'.join(re.escape(n) for n in sorted(mapping.values(), key=len, reverse=True)))
inverse = {v: k for k, v in mapping.items()}
rewrite = lambda text: pattern.sub(lambda m: mapping[m[0]], text)
restore = lambda text: reverse.sub(lambda m: inverse[m[0]], text)

(OUT / 'assets').mkdir(parents=True, exist_ok=True)
manifest = {'status': 'OFFLINE_CANDIDATE_NOT_DEPLOYED', 'runtime_tests': 'separate fixtures',
            'original_server_sha256': 'b8d8a69ee9ad9db8a5b0a6852b00f0df70008b65b84f35f02cf7e69067ba0502',
            'semantic_edit_scope': [desktop_name, 'new mic bridge modules'],
            'graph_policy': 'All JavaScript modules get a versioned filename; all mutual references are remapped together to preserve entry singleton identity.',
            'edits': edits, 'files': []}
for path in js_files:
    original = path.read_bytes()
    source = desktop if path.name == desktop_name else original.decode('utf-8')
    updated = rewrite(source)
    require(restore(updated) == source, 'module remap mismatch: ' + path.name)
    target = OUT / 'assets' / mapping[path.name]
    target.write_text(updated, encoding='utf-8', newline='')
    manifest['files'].append({'path': 'assets/' + target.name, 'sha256': sha(target.read_bytes()),
                              'original_path': 'assets/' + path.name, 'original_sha256': sha(original),
                              'only_module_references_changed': path.name != desktop_name})

index = rewrite((WEB / 'index.html').read_text(encoding='utf-8'))
require(mapping[entry_name] in index, 'entry module missing in candidate index')
require(restore(index) == (WEB / 'index.html').read_text(encoding='utf-8'), 'index reverse-map mismatch')
(OUT / 'index.html').write_text(index, encoding='utf-8', newline='')
manifest['index'] = {'path': 'index.html', 'sha256': sha((OUT / 'index.html').read_bytes()),
                     'original_sha256': sha((WEB / 'index.html').read_bytes())}
for source_name, target_name in [('mic-lease.mjs', 'mic-lease.mjs'), ('react-adapter.mjs', 'mic-react-20261015.mjs')]:
    data = (HERE / source_name).read_bytes()
    (OUT / 'assets' / target_name).write_bytes(data)
    manifest['files'].append({'path': 'assets/' + target_name, 'sha256': sha(data), 'new_bridge_module': True})

# Every static, dynamic and worker JS basename has been remapped consistently.
for path in (OUT / 'assets').iterdir():
    text = path.read_text(encoding='utf-8')
    require(not pattern.search(text), 'old JavaScript module reference remained: ' + path.name)
require(not pattern.search(index), 'old JavaScript module reference remained in index')
for relative, expected in pinned_manifest['payload_sha256'].items():
    if relative.startswith('payload/web/'):
        path = OUT / relative.removeprefix('payload/web/')
        require(sha(path.read_bytes()) == expected, 'candidate differs from validated release: ' + str(path))
(OUT / 'candidate-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(f'Prepared {len(js_files)} versioned original JS modules + 2 bridge modules; only desktop has functional edits.')
print('All other modules reverse-map exactly to their official original source. No device writes or deployment.')
