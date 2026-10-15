"""Offline Linux action sequencing tests. Only generated fixture copies are run."""
import argparse,hashlib,json,os,sys
from pathlib import Path
import re,shutil,subprocess,tempfile,unittest

HERE=Path(__file__).resolve().parent
SOURCE=(HERE/'src/device-actions.sh').read_text()
ARCHIVE_NAMES=('nanokvm-mic-runtime-20261015.tar.gz','nanokvm-mic-web-20261015.tar.gz')
ARCHIVES=Path(os.environ['NANOKVM_INSTALLER_ARCHIVE_DIR']).resolve() if os.environ.get('NANOKVM_INSTALLER_ARCHIVE_DIR') else None
sha=lambda data:hashlib.sha256(data).hexdigest()

@unittest.skipUnless(sys.platform == 'linux', 'Requires Linux bash and fixture symlinks; run under WSL/Linux.')
class Actions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if ARCHIVES is None:
            raise unittest.SkipTest('Set NANOKVM_INSTALLER_ARCHIVE_DIR or --archives-dir to the external verified runtime/web archives.')
        for name in ARCHIVE_NAMES:
            if not (ARCHIVES/name).is_file():
                raise unittest.SkipTest('External archive fixture missing: '+str(ARCHIVES/name))

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='nanokvm-actions-fixture-')
        self.root=Path(self.tmp.name);self.kit=self.root/'kit';self.kit.mkdir()
        self.mock=self.root/'mockbin';self.mock.mkdir()
        (self.mock/'id').write_text('#!/bin/sh\necho 0\n');(self.mock/'id').chmod(0o755)
        self.archive=b'fixture-per-device-backup';self.fp=sha(b'0123456789abcdef0123456789abcdef')
        for d in ('/root','/run','/etc/kvm','/usr/bin'):self.p(d).mkdir(parents=True,exist_ok=True)
        self.write('/sys/class/block/mmcblk0/device/cid','0123456789abcdef0123456789abcdef\n')
        self.p('/usr/bin/python3').symlink_to('/usr/bin/python3')
        script=SOURCE
        script=re.sub(r'/(?:usr/bin|sys|root|etc|run)(?=[/"\s])',lambda m:str(self.root)+m[0],script)
        script=script.replace('bash "$runtime_setup"','bash "$KIT_DIR/runtime-fixture.sh"')
        script=script.replace('"$web_setup" status','"$KIT_DIR/web-fixture.py" status')
        script=script.replace('"$web_setup" install','"$KIT_DIR/web-fixture.py" install')
        script=script.replace('"$web_setup" rollback','"$KIT_DIR/web-fixture.py" rollback')
        self.script=self.kit/'device-actions-fixture.sh';self.script.write_text(script)
        self.write('/event-log','')
        for name in ARCHIVE_NAMES:shutil.copyfile(ARCHIVES/name,self.kit/name)
        self.stub('check-new-device.sh',f'echo preflight-$1 >> {self.p("/event-log")}\necho BASELINE_OK\n')
        self.stub('backup-originals.sh',f'echo backup >> {self.p("/event-log")}\nprintf fixture-per-device-backup > {self.p("/root/nanokvm-mic-originals.tar.gz")}\n')
        (self.kit/'verify-original-backup.py').write_text('''import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[sys.argv.index('--archive')+1]); r={'ok':True,'schema':'nanokvm-original-backup-receipt-v1','baseline_id':'nanokvm-pro-stock-4.19.125-app-1.2.15-20261015','archive_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'device_fingerprint':FP}; print(json.dumps(r))
'''.replace('FP',repr(self.fp)))
        self.stub('runtime-fixture.sh',f'''echo runtime-$1 >> {self.p('/event-log')}
case "$1" in
 install) mkdir {self.p('/root/nanokvm-mic-runtime-20261015')}; echo installed > {self.p('/root/nanokvm-mic-runtime-20261015/state')};;
 activate) echo active > {self.p('/root/nanokvm-mic-runtime-20261015/state')};;
 status) if [ -f {self.p('/root/nanokvm-mic-runtime-20261015/state')} ]; then cat {self.p('/root/nanokvm-mic-runtime-20261015/state')}; else echo absent; fi;;
esac
''')
        (self.kit/'web-fixture.py').write_text(f'''import json,pathlib,sys
r=pathlib.Path({str(self.root)!r}); action=sys.argv[1]
with (r/'event-log').open('a') as h:h.write('web-'+action+'\\n')
p=r/'root/nanokvm-mic-web-20261015-backup/state.json'
if action=='install':
 p.parent.mkdir(); p.write_text(json.dumps({{'status':'installed'}}))
if action=='rollback':p.write_text(json.dumps({{'status':'rolled_back'}}))
print(json.dumps({{'ok':True,'status':json.loads(p.read_text())['status'] if p.exists() else 'not_installed'}}))
''')
    def tearDown(self):self.tmp.cleanup()
    def p(self,path):return self.root/path.lstrip('/')
    def write(self,path,data):
        p=self.p(path);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data.encode() if isinstance(data,str) else data)
    def stub(self,name,body):
        (self.kit/name).write_text('#!/bin/bash\nset -e\n'+body)
    def original(self):self.write('/root/nanokvm-mic-originals.tar.gz',self.archive)
    def runtime(self,state):self.write('/root/nanokvm-mic-runtime-20261015/state',state)
    def web(self,state):self.write('/root/nanokvm-mic-web-20261015-backup/state.json',json.dumps({'status':state}))
    def runaction(self,action,*args,error=None):
        result=subprocess.run(['bash',str(self.script),action,*args],env={**os.environ,'PATH':str(self.mock)+':'+os.environ['PATH']},text=True,capture_output=True)
        if error:self.assertNotEqual(result.returncode,0);self.assertIn('ERROR '+error,result.stderr)
        else:self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(self.p('/run/nanokvm-new-device-kit-20261015.lock').exists())
        return result
    def install(self,**kwargs):self.original();return self.runaction('install',self.fp,sha(self.archive),**kwargs)
    def events(self):return self.p('/event-log').read_text().splitlines()
    def test_prepare_new(self):
        r=self.runaction('prepare');self.assertEqual(self.events(),['preflight-prepare','backup']);self.assertIn('NANOKVM_RECEIPT_JSON=',r.stdout)
    def test_prepare_existing(self):
        self.original();self.runaction('prepare');self.assertEqual(self.events(),['preflight-prepare'])
    def test_install_new(self):
        self.install();self.assertEqual(self.events(),['preflight-candidate','runtime-install','runtime-activate','runtime-status','web-install','web-status']);self.assertTrue(self.p('/etc/kvm/scripts').is_dir())
    def test_resume_installed(self):
        self.runtime('installed');self.install();self.assertEqual(self.events(),['preflight-candidate','runtime-status','runtime-activate','runtime-status','web-install','web-status'])
    def test_resume_active(self):
        self.runtime('active');self.install();self.assertEqual(self.events(),['preflight-candidate','runtime-status','runtime-status','web-install','web-status'])
    def test_installed_status_only(self):
        self.runtime('active');self.web('installed');self.install();self.assertEqual(self.events(),['runtime-status','web-status'])
    def test_incomplete_runtime(self):
        self.runtime('activating');self.install(error='RUNTIME_STATE');self.assertEqual(self.events(),['preflight-candidate'])
    def test_rolledback_web(self):
        self.web('rolled_back');self.install(error='WEB_STATE');self.assertEqual(self.events(),[])
    def test_wrong_device(self):
        self.original();self.runaction('install','a'*64,sha(self.archive),error='WRONG_DEVICE');self.assertEqual(self.events(),[])
    def test_wrong_archive(self):
        self.original();self.runaction('install',self.fp,'a'*64,error='PACKAGE_HASH');self.assertEqual(self.events(),[])
    def test_invalid_receipt_arg(self):self.runaction('install','x;false','x',error='RECEIPT_ARGUMENTS');self.assertEqual(self.events(),[])
    def test_package_tampered(self):
        (self.kit/'nanokvm-mic-web-20261015.tar.gz').write_bytes(b'other');self.install(error='PACKAGE_HASH');self.assertEqual(self.events(),[])
    def test_parent_symlink(self):
        self.p('/etc/kvm').rmdir();self.p('/etc/kvm').symlink_to(self.p('/root'));self.install(error='SCRIPT_PARENT');self.assertNotIn('web-install',self.events())
    def test_status_does_not_activate(self):
        self.runtime('installed');self.web('installed');self.runaction('status');self.assertEqual(self.events(),['runtime-status','web-status'])
    def test_rollback_web_only(self):
        self.original();self.runtime('active');self.web('installed');self.runaction('rollback-web',self.fp,sha(self.archive));self.assertEqual(self.events(),['runtime-status','web-rollback','web-status'])
    def test_activation_error_no_retry(self):
        self.stub('runtime-fixture.sh',f'echo runtime-$1 >> {self.p("/event-log")}\n[ "$1" != activate ]\n')
        self.install(error='ACTION_COMMAND');self.assertEqual(self.events(),['preflight-candidate','runtime-install','runtime-activate'])

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('--archives-dir', type=Path)
    options,remaining=parser.parse_known_args()
    if options.archives_dir is not None:
        ARCHIVES=options.archives_dir.resolve()
    unittest.main(argv=[sys.argv[0],*remaining],verbosity=2)
