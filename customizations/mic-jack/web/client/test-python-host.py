"""Import-only protocol fixture; never calls production serve or device paths."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'controller'))
from mic_lease import LeaseController, parse_client_args

class FakeJack:
    value=0
    def ready(self): pass
    def read(self): return self.value
    def write(self,value): self.value=value; return value

controller=LeaseController(FakeJack())
for line in sys.stdin:
    name=json.loads(line)['name']
    parts=name.split(' ')
    assert parts[0]=='mic-lease.sh'
    result=controller.handle(parse_client_args(parts[1:]))
    print(json.dumps({'code':0,'data':{'log':json.dumps(result)}}),flush=True)
