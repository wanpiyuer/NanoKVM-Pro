import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { MicSession, createRunScriptTransport } from './mic-lease.mjs';

// Set NANOKVM_PYTHON (or PYTHON) when Python 3 is not named python3.
const child=spawn(process.env.NANOKVM_PYTHON || process.env.PYTHON || 'python3',['-u','-X','utf8',fileURLToPath(new URL('./test-python-host.py',import.meta.url))],{stdio:['pipe','pipe','inherit']});
const queued=[];
const lines=createInterface({input:child.stdout});
lines.on('line',line=>queued.shift()?.resolve(JSON.parse(line)));
child.on('error',error=>{ while(queued.length) queued.shift().reject(error); });
child.on('exit',code=>{ while(queued.length) queued.shift().reject(new Error(`fixture exited ${code}`)); });
const post=async(_path,body)=>new Promise((resolve,reject)=>{
  queued.push({resolve,reject}); child.stdin.write(JSON.stringify(body)+'\n');
});
const lease=createRunScriptTransport(post);
const makeStream=()=>{
  const track={readyState:'live',stop(){this.readyState='ended';}};
  return {track,getTracks:()=>[track],getAudioTracks:()=>[track]};
};
const aStream=makeStream(),bStream=makeStream();
let renew;
const a=new MicSession({owner:'a'.repeat(32),lease,capture:async()=>aStream,attach:()=>()=>{},every:(_ms,fn)=>{renew=fn;return()=>{};}});
const b=new MicSession({owner:'c'.repeat(32),lease,capture:async()=>bStream,attach:()=>()=>{},every:()=>()=>{}});
try {
  await a.enable();
  assert.equal(a.getState().phase,'on');
  assert.equal((await lease.status()).jack,1);
  await b.enable();
  assert.equal(b.getState().phase,'error');
  assert.equal(bStream.track.readyState,'ended');
  assert.equal((await lease.status()).jack,1,'second owner cannot close first');
  renew(); await lease.status();
  await a.disable();
  assert.equal(a.getState().phase,'off');
  assert.equal(aStream.track.readyState,'ended');
  assert.equal((await lease.status()).jack,0);
  console.log('PASS: actual TypeScript client ↔ Python controller protocol, claim/commit/renew/release and second-owner rejection. Fake jack only.');
} finally {
  await a.dispose(); await b.dispose(); child.stdin.end(); lines.close();
}
