"""Linux-only generated fixture tests. Production script has no fixture/path options."""
import gzip
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
SOURCE=(HERE/'src/check-new-device.sh').read_text()
sha=lambda data:hashlib.sha256(data).hexdigest()

@unittest.skipUnless(sys.platform == 'linux', 'Requires Linux bash and fixture symlinks; run under WSL/Linux.')
class Checks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='nanokvm-preflight-fixture-')
        self.root=Path(self.tmp.name)
        self.mock=self.root/'mockbin'; self.mock.mkdir()
        self.data={
            'db7a02d686e321f2a5f138118754fc602e68845347b49096767e10619f550e41':b'original-usb',
            '38a837323445e288197b40b6f465deb380a089885fc67e67d2211efa3e5f6098':b'runtime-usb',
            '8789885534908087b8083a3bb206a626151be95ef0b437fc0a8a42f4956e3bc1':b'runtime-helper',
            'b8d8a69ee9ad9db8a5b0a6852b00f0df70008b65b84f35f02cf7e69067ba0502':b'server',
            '2ae02b31927c46689bd8fae700cb1b14b411e32d6417e36927f42de083e69058':b'config',
            'dd44f437b87b87933bcd74febaff2bfc815f6b5e1fcbaa5c647d1ae880777f78':b'kernel-stock',
            '957be761f6821e43810bde25d02f08ac449dd69aa9b64a2aff477914c66538f2':b'dtb-stock',
            '4fbd88326897cf71679f2a68ad8852af7e8a8300c0c52ee8d38a807dfdca60d1':b'candidate-signed',
        }
        self.web=re.findall(r'^([0-9a-f]{64}) (index.html|assets/\S+)$',SOURCE,re.M)
        for digest,relative in self.web:self.data[digest]=('web:'+relative).encode()
        self.stock=re.search(r'\[ "\$\(cat /proc/version\)" = \'([^\']+)\'',SOURCE)[1]
        for base in ('/kvmapp','/dev/shm/kvmapp'):
            self.write(base+'/version','1.2.15\n')
            self.write(base+'/scripts/usbdev.sh',b'original-usb')
            self.write(base+'/server/NanoKVM-Server',b'server')
            for digest,relative in self.web:self.write(base+'/server/web/'+relative,self.data[digest])
        self.write('/proc/version',self.stock+'\n')
        self.write('/proc/config.gz',gzip.compress(b'config'))
        self.write('/proc/cmdline',f'root={self.root}/dev/mmcblk0p17 board_id=0x5')
        self.write('/proc/mounts',f'{self.root}/dev/mmcblk0p16 {self.root}/boot vfat rw,relatime 0 0\n')
        self.write('/proc/device-tree/model',b'NanoKVM-Pro\0')
        self.write('/sys/class/block/mmcblk0/device/cid','0123456789abcdef0123456789abcdef\n')
        for num in (12,13,14,15):
            self.write(f'/sys/class/block/mmcblk0p{num}/size','2048' if num<14 else '131072')
            self.write(f'/dev/mmcblk0p{num}',b'dtb-stock' if num<14 else b'kernel-stock')
        self.write('/boot/usb.uac2',''); self.write('/boot/usb.ncm','')
        self.gadget='/sys/kernel/config/usb_gadget/g0'
        for name in ('hid.GS0','hid.GS1','hid.GS2','uac2.usb0','ncm.usb0'):
            self.p(self.gadget+'/functions/'+name).mkdir(parents=True)
            link=self.p(self.gadget+'/configs/c.1/'+name);link.parent.mkdir(parents=True,exist_ok=True)
            link.symlink_to(self.p(self.gadget+'/functions/'+name))
            if name.startswith('hid.'):self.write(self.gadget+'/functions/'+name+'/no_out_endpoint','1')
        self.write(self.gadget+'/UDC','8000000.dwc3')
        for key,value in {'idVendor':'0x3346','idProduct':'0x1009','bcdDevice':'0x0623',
            'strings/0x409/serialnumber':'0123456789ABCDEF','strings/0x409/manufacturer':'sipeed',
            'strings/0x409/product':'NanoKVMPro','functions/uac2.usb0/function_name':'NanoKVMPro Audio'}.items():
            self.write(self.gadget+'/'+key,value+'\n')
        self.write(self.gadget+'/functions/ncm.usb0/ifname','usb0')
        for key,value in {'c_chmask':0,'p_chmask':3,'p_srate':48000,'p_ssize':2,'c_terminal_type':513,'p_terminal_type':769}.items():
            self.write(self.gadget+'/functions/uac2.usb0/'+key,str(value))
        self.service='\n'.join((
            'ExecStartPre={ path=/kvmapp/scripts/nanokvm_pre.sh ; argv[]=/kvmapp/scripts/nanokvm_pre.sh ; ignore_errors=no ; }',
            'ExecStart={ path=/dev/shm/kvmapp/scripts/nanokvm.sh ; argv[]=/dev/shm/kvmapp/scripts/nanokvm.sh ; ignore_errors=no ; }',
            'FragmentPath=/etc/systemd/system/nanokvm.service','DropInPaths='))+'\n'
        self.write('/service-fixture',self.service)
        self.write('/route-fixture','192.168.2.1 dev wlan0 src 192.168.2.132\n')
        for name,body in {'uname':'echo aarch64','id':'echo 0',
            'systemctl':f'cat {self.root}/service-fixture',
            'ip':f'cat {self.root}/route-fixture'}.items():
            mock=self.mock/name;mock.write_text('#!/bin/sh\n'+body+'\n');mock.chmod(0o755)
        self.p('/usr/bin').mkdir(parents=True)
        self.p('/usr/bin/python3').symlink_to('/usr/bin/python3')
        # Explicit test-only materialization. Production contains no injection API.
        fixture=SOURCE
        for original,data in self.data.items():fixture=fixture.replace(original,sha(data))
        fixture=fixture.replace('[ -b "/dev/mmcblk0p$part" ]','[ -f "/dev/mmcblk0p$part" ]')
        fixture=fixture.replace('head -c 8786728','head -c 16').replace('== (3,13)',f'== {tuple(sys.version_info[:2])!r}')
        fixture=re.sub(r'/(?:dev/shm|usr/bin|sys|proc|dev|boot|kvmapp|root|etc)(?=[/"\s])',lambda m:str(self.root)+m[0],fixture)
        fixture=fixture.replace(str(self.root)+'/dev/null','/dev/null')
        self.script=self.root/'check-fixture.sh';self.script.write_text(fixture)
        # Match command stdout to rewritten expected paths without executing real systemctl.
        self.write('/service-fixture',re.sub(r'/(?:dev/shm|kvmapp|etc)(?=/)',lambda m:str(self.root)+m[0],self.service))

    def tearDown(self):self.tmp.cleanup()
    def p(self,path):return self.root/path.lstrip('/')
    def write(self,path,data):
        p=self.p(path);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data if isinstance(data,bytes) else data.encode())
    def runcheck(self,mode='prepare',error=None):
        before={str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file() and not p.is_symlink()}
        run=subprocess.run(['bash',str(self.script),mode],env={**os.environ,'PATH':str(self.mock)+':'+os.environ['PATH'],'SSH_CONNECTION':'192.168.2.1 1234 192.168.2.132 22'},text=True,capture_output=True)
        after={str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file() and not p.is_symlink()}
        self.assertEqual(before,after,'preflight wrote files')
        if error:self.assertNotEqual(run.returncode,0,run.stdout);self.assertIn('ERROR '+error,run.stderr)
        else:self.assertEqual(run.returncode,0,run.stderr);self.assertEqual(run.stdout.strip().splitlines()[-1],'BASELINE_QUICK_OK' if mode=='quick' else 'BASELINE_OK')
        self.assertNotIn('0123456789abcdef0123456789abcdef',run.stdout+run.stderr)
        return run
    def candidate(self,state=None):
        self.write('/proc/version','Linux version 4.19.125 (codex@nanokvm-mic-candidate) (gcc version fixture) #3 SMP PREEMPT Wed Oct 14 10:30:53 UTC 2026\n')
        self.write('/dev/mmcblk0p14',b'candidate-signed'+b'trailing-data')
        self.write(self.gadget+'/functions/uac2.usb0/p_jack_detect','1' if state=='active' else '0')
        self.write(self.gadget+'/functions/uac2.usb0/p_jack_connected','0' if state=='active' else '1')
        if state:
            for base in ('/kvmapp','/dev/shm/kvmapp'):
                self.write(base+'/scripts/usbdev.sh',b'runtime-usb');self.write(base+'/scripts/mic-jack.sh',b'runtime-helper')
            for name,data in [('state',state),('backup.complete','complete'),('backup.sha256','fixture')]:
                self.write('/root/nanokvm-mic-runtime-20261015/'+name,data)
            if state=='active':self.write('/boot/uac2.jack_detect','')
    def test_prepare(self):self.runcheck()
    def test_quick(self):self.runcheck('quick')
    def test_candidate(self):
        frozen=(HERE.parent/'kernel/modified/f_uac2.c').read_text()
        self.assertIn('opts->p_jack_detect = false;',frozen)
        self.assertIn('opts->p_jack_connected = true;',frozen)
        self.candidate();self.runcheck('candidate')
    def test_candidate_active(self):self.candidate('active');self.runcheck('candidate')
    def test_candidate_installed(self):self.candidate('installed');self.runcheck('candidate')
    def test_candidate_initial_wrong_connected(self):self.candidate();self.write(self.gadget+'/functions/uac2.usb0/p_jack_connected','0');self.runcheck('candidate',error='CANDIDATE_JACK')
    def test_candidate_installed_wrong_connected(self):self.candidate('installed');self.write(self.gadget+'/functions/uac2.usb0/p_jack_connected','0');self.runcheck('candidate',error='CANDIDATE_JACK')
    def test_candidate_active_not_closed(self):self.candidate('active');self.write(self.gadget+'/functions/uac2.usb0/p_jack_connected','1');self.runcheck('candidate',error='CANDIDATE_JACK')
    def test_candidate_initial_wrong_detect(self):self.candidate();self.write(self.gadget+'/functions/uac2.usb0/p_jack_detect','1');self.runcheck('candidate',error='CANDIDATE_JACK')
    def test_candidate_active_wrong_detect(self):self.candidate('active');self.write(self.gadget+'/functions/uac2.usb0/p_jack_detect','0');self.runcheck('candidate',error='CANDIDATE_JACK')
    def test_unknown_app(self):self.write('/kvmapp/version','1.2.16');self.runcheck(error='APP_VERSION')
    def test_kernel_version(self):self.write('/proc/version','Linux 4.19.125\n');self.runcheck(error='STOCK_KERNEL')
    def test_model(self):self.write('/proc/device-tree/model','Other');self.runcheck(error='WRONG_MODEL')
    def test_missing_cid(self):self.p('/sys/class/block/mmcblk0/device/cid').unlink();self.runcheck(error='DEVICE_CID')
    def test_config_mismatch(self):self.write('/proc/config.gz',gzip.compress(b'changed'));self.runcheck(error='KERNEL_CONFIG')
    def test_ram_web_mismatch(self):self.write('/dev/shm/kvmapp/server/web/index.html','other');self.runcheck(error='WEB_BASELINE')
    def test_script_mismatch(self):self.write('/kvmapp/scripts/usbdev.sh','other');self.runcheck(error='USB_SCRIPT_HASH')
    def test_dropin(self):self.write('/service-fixture',self.p('/service-fixture').read_text().replace('DropInPaths=','DropInPaths=/tmp/override'));self.runcheck(error='SERVICE_DROPIN')
    def test_extra_function(self):self.p(self.gadget+'/configs/c.1/acm.usb0').symlink_to(self.p(self.gadget+'/functions/uac2.usb0'));self.runcheck(error='USB_ENDPOINT_BUDGET')
    def test_missing_uac(self):self.p(self.gadget+'/configs/c.1/uac2.usb0').unlink();self.runcheck(error='MIC_NOT_MOUNTED')
    def test_mic_bootflag_missing(self):self.p('/boot/usb.uac2').unlink();self.runcheck(error='MIC_NOT_MOUNTED')
    def test_usb_ssh(self):self.write('/route-fixture','192.168.2.1 dev usb0 src 192.168.2.132\n');self.runcheck(error='SSH_ROUTE')
    def test_renamed_usb_ssh(self):self.write(self.gadget+'/functions/ncm.usb0/ifname','eth0');self.write('/route-fixture','192.168.2.1 dev eth0\n');self.runcheck(error='SSH_USB_ROUTE')
    def test_partsize(self):self.write('/sys/class/block/mmcblk0p14/size','2048');self.runcheck(error='PARTITION_LAYOUT')
    def test_boot_mount_missing(self):self.write('/proc/mounts','');self.runcheck(error='BOOT_MOUNT')
    def test_boot_readonly(self):self.write('/proc/mounts',f'{self.root}/dev/mmcblk0p16 {self.root}/boot vfat ro,relatime 0 0\n');self.runcheck(error='BOOT_READONLY')
    def test_dtb(self):self.write('/dev/mmcblk0p12',b'other');self.runcheck(error='DTB_HASH')
    def test_kernel_bytes(self):self.write('/dev/mmcblk0p14',b'other');self.runcheck(error='ORIGINAL_KERNEL_HASH')
    def test_candidate_bytes(self):self.candidate();self.write('/dev/mmcblk0p14',b'other');self.runcheck('candidate',error='CANDIDATE_IMAGE_HASH')
    def test_candidate_p15_changed(self):self.candidate();self.write('/dev/mmcblk0p15',b'other');self.runcheck('candidate',error='ORIGINAL_SLOT_HASH')
    def test_incomplete_runtime(self):self.candidate('activating');self.runcheck('candidate',error='RUNTIME_STATE')
    def test_custom_identity(self):
        self.write('/boot/usb.serialnumber','private-serial\n');self.write('/boot/uac2.function_name','Custom Mic\n')
        self.write(self.gadget+'/strings/0x409/serialnumber','private-serial\n');self.write(self.gadget+'/functions/uac2.usb0/function_name','Custom Mic\n')
        result=self.runcheck();self.assertNotIn('private-serial',result.stdout+result.stderr)
    def test_pending_boot_identity(self):self.write('/boot/usb.product','New Name\n');self.runcheck(error='PENDING_USB_IDENTITY')
    def test_live_only_identity(self):self.write(self.gadget+'/strings/0x409/product','Temporary Name\n');self.runcheck(error='PENDING_USB_IDENTITY')
    def test_numeric_identity_equivalent(self):self.write('/boot/usb.vid','0X3346\n');self.write(self.gadget+'/idVendor','13126\n');self.runcheck()
    def test_pending_numeric_identity(self):self.write('/boot/usb.pid','0xFFFF\n');self.runcheck(error='PENDING_USB_IDENTITY')
    def test_pending_identity_preserves_spaces(self):self.write('/boot/usb.product','NanoKVMPro \n');self.runcheck(error='PENDING_USB_IDENTITY')
    def test_identity_symlink(self):self.p('/boot/usb.product').symlink_to(self.p('/kvmapp/version'));self.runcheck(error='FILE_TYPE')
    def test_existing_web(self):self.p('/root/nanokvm-mic-web-20261015-backup').mkdir(parents=True);self.runcheck(error='ALREADY_MODIFIED')

if __name__=='__main__':unittest.main(verbosity=2)
