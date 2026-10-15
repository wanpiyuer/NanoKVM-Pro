"""Run source-only/offline checks. External asset checks report their own skips."""
from pathlib import Path
import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent

def static_checks():
    lock = json.loads((ROOT / 'sources-lock.json').read_text(encoding='utf-8'))
    for entry in lock['copied_files']:
        path = ROOT / entry['path']
        assert path.stat().st_size == entry['bytes'], path
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry['sha256'], path
    for name in ('mic_lease.py','mic-lease.sh'):
        assert (ROOT/'web/controller'/name).read_bytes() == (ROOT/'web/install/payload'/name).read_bytes()
    count = 0
    for path in ROOT.rglob('*.py'):
        if '__pycache__' in path.parts:
            continue
        ast.parse(path.read_text(encoding='utf-8-sig'),filename=str(path.relative_to(ROOT)))
        count += 1
    print(f'SOURCE_LOCK_AND_PYTHON_SYNTAX_OK ({count} Python files)',flush=True)

def run(command, env):
    print('\nRUN '+subprocess.list2cmdline(list(map(str,command))),flush=True)
    subprocess.run(list(map(str,command)),cwd=ROOT,env=env,check=True)

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--static-only',action='store_true')
    args=ap.parse_args()
    static_checks()
    if args.static_only:
        return
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',NANOKVM_PYTHON=sys.executable)
    py=sys.executable
    suites=[
        [py,'-m','unittest','discover','-s',ROOT/'web/controller','-p','test_mic_lease.py'],
        [py,ROOT/'installer/src/test-verify-original-backup.py'],
        [py,ROOT/'installer/test_build_install_kit.py'],
        [py,ROOT/'web/install/test_install_independent.py'],
    ]
    if os.name != 'nt':
        suites += [[py,ROOT/'installer/test_preflight.py'],[py,ROOT/'installer/test_device_actions.py']]
        bash=shutil.which('bash')
        if bash:
            for path in sorted(ROOT.rglob('*.sh')):
                suites.append([bash,'-n',path])
    else:
        print('SKIP Linux generated-fixture suites on Windows; run this checker under Linux/WSL.',flush=True)
        powershell=shutil.which('powershell.exe')
        if powershell:
            for filename in ('test-start.ps1','test-launch.ps1','test-gui-audit.ps1'):
                suites.append([powershell,'-NoProfile','-STA','-ExecutionPolicy','Bypass','-File',ROOT/'installer/tests'/filename])
        else:
            print('SKIP Windows PowerShell 5.1 tests: powershell.exe unavailable.',flush=True)
    reproduction=ROOT/'tools/test_web_reproduction.py'
    if reproduction.exists():
        suites.append([py,reproduction])
    node=shutil.which('node')
    if node:
        for name in ('mic-lease.test.mjs','test-react-adapter.mjs','test-integration.mjs','test-cross-language.mjs'):
            suites.append([node,'--test',ROOT/'web/client'/name])
    else:
        print('SKIP JavaScript suites: node unavailable.',flush=True)
    for suite in suites:
        run(suite,env)
    print('\nAVAILABLE_OFFLINE_CHECKS_PASSED (see explicit platform/external-input skips above).',flush=True)

if __name__=='__main__':
    main()
