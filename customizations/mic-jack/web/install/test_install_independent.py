"""Independent installer fixtures. Only temporary files and a fake systemd backend."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('web_setup',HERE/'setup.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
sha=lambda data:hashlib.sha256(data).hexdigest()

class Backend:
    def __init__(self,root):
        self.root=root;self.calls=[];self.active=False;self.loaded=False;self.fail_start=False;self.bad_ready=False
        self.parent={'ActiveState':'active','MainPID':'123','InvocationID':'a'*32}
    def parent_identity(self):
        if self.parent['ActiveState']!='active': raise mod.InstallError('fixture parent inactive')
        return dict(self.parent)
    def unit_state(self):
        return {'LoadState':'loaded' if self.loaded else 'not-found','ActiveState':'active' if self.active else 'inactive','Result':'success'}
    def validate_python(self): self.calls.append('validate_python')
    def reload(self): self.calls.append('reload');self.loaded=(self.root/mod.UNIT_PATH.lstrip('/')).exists()
    def enable(self):
        self.calls.append('enable');p=self.root/mod.WANTS.lstrip('/');p.parent.mkdir(parents=True,exist_ok=True);p.symlink_to(self.root/mod.UNIT_PATH.lstrip('/'))
    def start(self):
        self.calls.append('start')
        if self.fail_start: raise mod.InstallError('fixture start failed')
        self.active=True
        (self.root/(mod.GADGET+'/functions/uac2.usb0/p_jack_connected').lstrip('/')).write_text('0\n')
    def stop(self):
        self.calls.append('stop');self.active=False
        (self.root/(mod.GADGET+'/functions/uac2.usb0/p_jack_connected').lstrip('/')).write_text('0\n')
    def disable(self): self.calls.append('disable');(self.root/mod.WANTS.lstrip('/')).unlink(missing_ok=True)
    def lease_status(self,_socket):
        return {'version':1,'ok':True,'phase':'idle','jack':1 if self.bad_ready else 0,'leaseSeq':None,'ttlMs':0,'bootEpoch':'b'*32}

PACKAGE = Path(os.environ['NANOKVM_WEB_PACKAGE']).resolve() if os.environ.get('NANOKVM_WEB_PACKAGE') else None


@unittest.skipUnless(PACKAGE is not None,
                     'set NANOKVM_WEB_PACKAGE to an offline staged package from tools/stage-web-payload.py')
@unittest.skipUnless(os.name == 'posix', 'installer fixtures require POSIX symlinks and file permissions')
class InstallerFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='mic-web-installer-',dir='/var/tmp')
        self.base=Path(self.tmp.name);self.root=self.base/'root';self.root.mkdir()
        self.pkg=self.base/'package';shutil.copytree(PACKAGE/'payload',self.pkg/'payload')
        self.manifest=json.loads((PACKAGE/'manifest.json').read_text(encoding='utf-8'))
        self.expected=copy.deepcopy(mod.EXPECTED)
        self.expected['server_sha256']=sha(b'fixture-server')
        self.expected['runtime_helper_sha256']=sha(b'fixture-helper')
        self.original={rel:('fixture-original:'+rel).encode() for rel in self.expected['web_baseline']}
        self.expected['web_baseline']={rel:sha(data) for rel,data in self.original.items()}
        self.manifest['expected']=self.expected
        (self.pkg/'manifest.json').write_text(json.dumps(self.manifest))
        def put(absolute,data):
            p=self.p(absolute);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        self.put=put
        for app in mod.ROOTS:
            put(app+'/version',b'1.2.15\n');put(app+'/server/NanoKVM-Server',b'fixture-server')
            put(app+'/scripts/mic-jack.sh',b'fixture-helper');put(app+'/scripts/usbdev.sh',b'fixture-usb-script')
            for rel,data in self.original.items(): put(app+'/server/web/'+rel,data)
        for attr,_ in mod.IDENTITIES: put(mod.GADGET+'/'+attr,('fixture-'+attr).encode())
        for attr,value in [('UDC',b'8000000.dwc3\n'),('functions/uac2.usb0/p_jack_detect',b'1\n'),('functions/uac2.usb0/p_jack_connected',b'0\n')]: put(mod.GADGET+'/'+attr,value)
        link=self.p(mod.GADGET+'/configs/c.1/uac2.usb0');link.parent.mkdir(parents=True);link.symlink_to('../../functions/uac2.usb0')
        put('/proc/version',b'fixture-kernel');put('/boot/uac2.jack_detect',b'');put('/boot/usb.uac2',b'')
        for d in ['/root','/run','/etc/kvm/scripts','/etc/systemd/system']: self.p(d).mkdir(parents=True,exist_ok=True)
        self.backend=Backend(self.root)
        self.inst=mod.Installer(self.pkg,root=self.root,backend=self.backend,trusted_expected=self.expected,readiness_seconds=0,sleep=lambda _:None)
    def tearDown(self): self.tmp.cleanup()
    def p(self,path): return self.root/path.lstrip('/')
    def tree(self):
        return {p.relative_to(self.root).as_posix():('link',str(p.readlink())) if p.is_symlink() else ('file',sha(p.read_bytes())) for p in self.root.rglob('*') if p.is_symlink() or p.is_file()}
    def test_install_status_rollback(self):
        before=self.inst.snapshot();self.assertEqual(self.inst.install()['status'],'installed')
        self.assertEqual(self.inst.snapshot(),before);self.assertEqual(self.backend.calls,['validate_python','reload','enable','start'])
        tree=self.tree();calls=list(self.backend.calls)
        self.assertTrue(self.inst.status()['ok']);self.assertEqual(self.tree(),tree);self.assertEqual(self.backend.calls,calls)
        self.assertEqual(self.inst.rollback()['status'],'rolled_back');self.assertEqual(self.inst.snapshot(),before)
        for app in mod.ROOTS:
            self.assertEqual(self.p(app+'/server/web/index.html').read_bytes(),self.original['index.html'])
            self.assertFalse(self.p(app+'/'+mod.DAEMON_REL).exists())
            self.assertTrue(list(self.p(app+'/server/web/assets').glob('*-mic20261015.js')))
        self.assertFalse(self.p(mod.WRAPPER).exists());self.assertFalse(self.p(mod.UNIT_PATH).exists())
    def test_unknown_ram_baseline_stops_before_backup(self):
        self.p('/dev/shm/kvmapp/server/web/assets/index-D8MmPk1r.js').write_bytes(b'external')
        before=self.tree()
        with self.assertRaises(mod.InstallError): self.inst.install()
        self.assertFalse(self.p(mod.BACKUP).exists());self.assertNotIn('start',self.backend.calls)
        after=self.tree();after.pop(mod.LOCK.lstrip('/'),None);self.assertEqual(before,after)
    def test_tampered_payload_rejected(self):
        (self.pkg/'payload/mic_lease.py').write_text('tampered')
        with self.assertRaises(mod.InstallError): self.inst.install()
        self.assertFalse(self.p(mod.BACKUP).exists());self.assertEqual(self.backend.calls,[])
    def test_preexisting_wrapper_never_overwritten(self):
        self.put(mod.WRAPPER,b'owner file')
        with self.assertRaises(mod.InstallError): self.inst.install()
        self.assertEqual(self.p(mod.WRAPPER).read_bytes(),b'owner file');self.assertFalse(self.p(mod.BACKUP).exists())
    def test_start_failure_keeps_indexes_original_and_explicit_rollback_works(self):
        self.backend.fail_start=True
        with self.assertRaises(mod.InstallError): self.inst.install()
        for app in mod.ROOTS: self.assertEqual(self.p(app+'/server/web/index.html').read_bytes(),self.original['index.html'])
        self.assertEqual(self.inst.status()['status'],'incomplete')
        self.assertEqual(self.inst.rollback()['status'],'rolled_back')
    def test_readiness_failure_does_not_switch_index(self):
        self.backend.bad_ready=True
        with self.assertRaises(mod.InstallError): self.inst.install()
        for app in mod.ROOTS: self.assertEqual(self.p(app+'/server/web/index.html').read_bytes(),self.original['index.html'])
        self.assertTrue(self.backend.active);self.assertEqual(self.inst.rollback()['status'],'rolled_back')
    def test_second_index_failure_keeps_journal_and_can_rollback(self):
        original_write=mod.atomic_write
        target=self.p('/dev/shm/kvmapp/server/web/index.html')
        def fail(path,*args,**kwargs):
            if path==target and kwargs.get('replace'): raise OSError('fixture power interruption')
            return original_write(path,*args,**kwargs)
        with mock.patch.object(mod,'atomic_write',side_effect=fail):
            with self.assertRaises(OSError): self.inst.install()
        state=json.loads(self.p(mod.BACKUP+'/state.json').read_text())
        self.assertEqual(state['indexes']['disk']['stage'],'candidate');self.assertEqual(state['indexes']['ram']['stage'],'replace_intent')
        self.assertEqual(self.inst.rollback()['status'],'rolled_back')
    def test_parent_restart_before_commit_is_detected(self):
        old=self.backend.lease_status
        def changed(socket):
            result=old(socket);self.backend.parent['InvocationID']='c'*32;return result
        self.backend.lease_status=changed
        with self.assertRaises(mod.InstallError): self.inst.install()
        for app in mod.ROOTS: self.assertEqual(self.p(app+'/server/web/index.html').read_bytes(),self.original['index.html'])
    def test_rollback_refuses_modified_control_before_any_restore(self):
        self.inst.install();self.p(mod.WRAPPER).write_bytes(b'external edit')
        before=self.tree();calls=list(self.backend.calls)
        with self.assertRaises(mod.InstallError): self.inst.rollback()
        self.assertEqual(self.tree(),before);self.assertEqual(self.backend.calls,calls)
    def test_symlink_ancestor_is_not_followed(self):
        assets=self.p('/kvmapp/server/web/assets');other=self.base/'external';assets.rename(other);assets.symlink_to(other,target_is_directory=True)
        with self.assertRaises(mod.InstallError): self.inst.install()
        self.assertFalse(self.p(mod.BACKUP).exists())
    def test_installed_cannot_be_reinstalled(self):
        self.inst.install();before=self.tree()
        with self.assertRaises(mod.InstallError): self.inst.install()
        self.assertEqual(self.tree(),before)
    def test_rollback_stop_without_jack_off_keeps_control_files(self):
        self.inst.install();old_stop=self.backend.stop
        def not_off():
            old_stop();self.put(mod.GADGET+'/functions/uac2.usb0/p_jack_connected',b'1\n')
        self.backend.stop=not_off
        with self.assertRaises(mod.InstallError): self.inst.rollback()
        self.assertTrue(self.p(mod.WRAPPER).exists());self.assertTrue(self.p(mod.UNIT_PATH).exists())
        self.assertEqual(self.inst.status()['status'],'rollback_incomplete')
    def test_fresh_device_missing_script_directory_can_bootstrap_then_install(self):
        directory=self.p('/etc/kvm/scripts');directory.rmdir()
        with self.assertRaisesRegex(mod.InstallError,'missing target directory'):
            self.inst.install()
        self.assertFalse(self.p(mod.BACKUP).exists())
        self.assertFalse(self.p(mod.UNIT_PATH).exists())
        self.assertNotIn('start',self.backend.calls)
        for app in mod.ROOTS:
            self.assertEqual(self.p(app+'/server/web/index.html').read_bytes(),self.original['index.html'])
        directory.mkdir(mode=0o755)
        self.assertEqual(self.inst.install()['status'],'installed')

if __name__=='__main__': unittest.main(verbosity=2)
