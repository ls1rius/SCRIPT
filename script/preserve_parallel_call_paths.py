"""Keep absolute raw-call references resolvable after atomic publication."""
import argparse
import fcntl
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

_CHILDREN={}


def preserve(out,control):
    aliases=[]
    with (control/'claims.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        state=json.loads((control/'claims.json').read_text())
        for uid,row in state.items():
            # A later explicit continuation can publish a question that failed
            # in this control directory. Never alias over that old attempt.
            if row['state']!='complete':continue
            target=out/'runs'/uid
            if not (target/'result.json').exists():continue
            original=control/'attempts'/uid/'run'
            if original.is_symlink():
                if original.resolve()!=target.resolve():raise ValueError('Origin alias points to wrong question')
            elif not original.exists():
                original.parent.mkdir(parents=True,exist_ok=True)
                original.symlink_to(target,target_is_directory=True);aliases.append(uid)
            else:raise ValueError('Published question also has a different original directory')
    return aliases


def process_identity(pid):
    try:
        fields=Path('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].split()
        return fields[19] if fields[0]!='Z' else None
    except FileNotFoundError:return None


def helper_preflight(helper,python=None):
    helper=Path(helper).resolve(strict=True)
    if not helper.is_file():raise ValueError('Alias helper must be a regular file')
    source=helper.read_bytes();compile(source,str(helper),'exec')
    checked=subprocess.run([python or sys.executable,str(helper),'--help'],capture_output=True,text=True,timeout=10)
    if checked.returncode:raise ValueError('Alias helper --help failed: '+checked.stderr[-1000:])
    return dict(passed=True,path=str(helper),sha256=hashlib.sha256(source).hexdigest(),syntax_checked=True,help_exit_code=0)


def audit_references(out,control):
    """Audit every successful call in this control's published questions."""
    checked=0;questions=[];missing=[];mismatched=[]
    state=json.loads((control/'claims.json').read_text())
    for uid,row in state.items():
        if row['state']!='complete':continue
        run=out/'runs'/uid
        if not (run/'result.json').exists():raise ValueError('Completed claim has no published result: '+uid)
        questions.append(uid)
        for region in ['directory_calls','partial_search','partial_temporal']:
            path=run/region/'calls.jsonl'
            if not path.exists():continue
            for line in path.read_text().splitlines():
                call=json.loads(line)
                if call.get('error') or not call.get('output_ref'):continue
                reference=Path(call['output_ref']);checked+=1
                if not reference.is_file():missing.append(str(reference));continue
                local=run/region/'raw_calls'/reference.name
                if not local.is_file() or reference.read_bytes()!=local.read_bytes():
                    mismatched.append(str(reference))
    return dict(passed=not missing and not mismatched,questions=questions,successful_call_references=checked,
                missing=missing,content_mismatches=mismatched)


def _write(path,data):
    temp=path.with_name(path.name+f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n');temp.replace(path)


def launch(out,control,supervisor,helper=None,python=None):
    """Explicit auxiliary launch; a PID alone is never treated as readiness."""
    out=Path(out).resolve();control=Path(control).resolve()
    helper=Path(helper or control/'preserve_parallel_call_paths.py')
    checked=helper_preflight(helper,python)
    helper=Path(checked['path'])
    if process_identity(supervisor) is None:raise ValueError('Alias supervisor is not alive')
    record=control/'origin_aliases_launch.json'
    if record.exists():raise ValueError('Prior alias launch must be explicitly archived before another launch')
    first=preserve(out,control);refs=audit_references(out,control)
    if not refs['passed']:raise ValueError('Initial alias/reference audit failed: '+json.dumps(refs))
    command=[python or sys.executable,'-u',str(helper),'--out',str(out),'--control',str(control),'--supervisor',str(supervisor)]
    with (control/'origin_aliases.log').open('a') as log:
        child=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    data=dict(pid=child.pid,command=command,helper_preflight=checked,initial_alias_scan=first,initial_reference_audit=refs,ready=False)
    _write(record,data)
    try:
        for _ in range(100):
            identity=process_identity(child.pid)
            if child.poll() is not None or identity is None:raise RuntimeError('Alias helper exited during startup')
            status=control/'origin_aliases_status.json'
            current=json.loads(status.read_text()) if status.exists() else {}
            if current.get('pid')==child.pid and current.get('process_identity')==identity and current.get('first_scan_complete'):
                data.update(ready=True,process_identity=identity,startup_status=current)
                _write(record,data);_CHILDREN[child.pid]=child;return data
            time.sleep(.1)
        raise RuntimeError('Alias helper did not report an initial successful scan')
    except Exception as exc:
        data['startup_error']=str(exc);_write(record,data)
        if child.poll() is None:child.terminate();child.wait(timeout=5)
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--control',type=Path,required=True);parser.add_argument('--supervisor',type=int,required=True)
    parser.add_argument('--launch',action='store_true')
    args=parser.parse_args();identity=process_identity(args.supervisor)
    if args.launch:
        print(json.dumps(launch(args.out,args.control,args.supervisor)),flush=True);sys.exit(0)
    with (args.control/'aliases.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        while True:
            for uid in preserve(args.out,args.control):print('PRESERVED_RAW_CALL_PATHS',uid,flush=True)
            _write(args.control/'origin_aliases_status.json',dict(pid=os.getpid(),process_identity=process_identity(os.getpid()),
                first_scan_complete=True,supervisor=args.supervisor,last_scan_unix=time.time()))
            if identity is None or process_identity(args.supervisor)!=identity:break
            time.sleep(1)
