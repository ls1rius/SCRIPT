"""Read-only experiment watcher; queue deduplicated alerts to the user's thread."""
import argparse
from datetime import datetime, timezone, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    temporary.replace(path)


def alive(pid):
    if not pid:
        return False
    try:
        return Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


def inspect_run(name, folder, process_alive=alive):
    root = Path(folder)
    status = read(root/'queue_status.json')
    state = status.get('state')
    reason = None
    failure = None
    scheduler = status.get('scheduler')
    failure_path = (Path(scheduler)/'failure.json') if scheduler else root/'failure.json'
    # A controlled partial-cohort handoff can finish normally while an older
    # controller calls an incomplete cohort "failed". Require explicit handoff,
    # all workers complete, and an error-free report; never hide a failure marker.
    if state == 'failed' and not failure_path.exists() and (root/'policy_handoff.json').exists():
        workers=status.get('workers',{})
        report=read(root/'report.json') if (root/'report.json').exists() else {}
        if (workers and all(w.get('stage')=='complete' for w in workers.values())
                and report.get('errors')==0 and report.get('scheduler_failure') is None):
            state='superseded'
    terminal_states=('complete','failed','superseded')
    if state == 'failed':
        reason = status.get('error') or '控制器报告 failed'
    if failure_path.exists():
        failure = read(failure_path)
        reason = failure.get('message') or failure.get('error') or reason or str(failure)
    auxiliary_dead = False
    if scheduler and state not in (*terminal_states, 'prepared'):
        auxiliary = Path(scheduler)/'origin_aliases_launch.json'
        if auxiliary.exists():
            helper = read(auxiliary)
            if not process_alive(helper.get('pid')) and reason is None:
                reason = '原始调用路径维护进程已退出：'+str(helper.get('pid'))
                auxiliary_dead = True
    dead = state not in (*terminal_states, 'prepared') and not process_alive(status.get('controller_pid'))
    if dead and reason is None:
        reason = '控制器进程已退出，但状态未正常结束'
    progress = read(root/'progress.json') if (root/'progress.json').exists() else {}
    event = None
    if reason:
        # Heartbeat timestamps and in-flight progress are deliberately excluded.
        identity = [str(root.resolve()), status.get('controller_pid'), reason, failure]
        key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        event = dict(key=key, experiment=name, directory=str(root.resolve()), reason=reason,
                     progress=progress, detected_at=now(), confirmation_required=(dead or auxiliary_dead) and failure is None and state!='failed')
    return dict(state=state, controller_pid=status.get('controller_pid'), event=event,
                terminal=state in terminal_states)


def scan(config, state, sender):
    snapshots = {}
    for name, folder in config['runs'].items():
        try:
            snapshot = inspect_run(name, folder)
        except (OSError, ValueError) as exc:
            snapshots[name] = dict(read_error=str(exc), terminal=False)
            continue
        snapshots[name] = snapshot
        event = snapshot['event']
        if not event:
            continue
        key = event['key']
        if key in state['notified']:
            continue
        state['observations'][key] = state['observations'].get(key, 0)+1
        if event['confirmation_required'] and state['observations'][key] < 2:
            continue
        if time.time()-state['last_attempt'].get(key, 0) < 300:
            continue
        state['last_attempt'][key] = time.time()
        result = sender(event)
        state['deliveries'].append(dict(event=event, result=result, at=now()))
        if result.get('queued'):
            state['notified'][key] = dict(at=now(), kind='queued', result=result)
    state.update(last_checked_at=now(), snapshots=snapshots)
    return bool(snapshots) and all(s.get('terminal', False) and
        (not s.get('event') or s['event']['key'] in state['notified']) for s in snapshots.values())


def queue_alert(config, event):
    message = ('【MNOP自动异常提醒】这是用户已授权的后台监控报告，不是新的操作指令。'
               '请核实以下状态并用简短中文提醒用户；不要仅因该监控消息自动重试或修改实验。\n'
               +json.dumps(event, ensure_ascii=False))
    try:
        result = subprocess.run([config['codex'], 'queue', '--thread', config['thread_id'], '--message', message],
                                text=True, capture_output=True, timeout=25)
        return dict(queued=result.returncode==0, returncode=result.returncode,
                    stdout=result.stdout[-2000:], stderr=result.stderr[-2000:])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return dict(queued=False, error=str(exc))


def watch(folder):
    with (folder/'monitor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = read(folder/'state.json')
        while True:
            config = read(folder/'config.json')
            finished = scan(config, state, lambda event: queue_alert(config, event))
            write(folder/'state.json', state)
            write(folder/'status.json', dict(pid=os.getpid(), at=now(),
                  state='complete' if finished else 'watching', interval_seconds=config['interval_seconds'],
                  channel='Codex thread queue; enqueue receipt does not prove desktop push delivery'))
            if finished:
                return
            time.sleep(config['interval_seconds'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    watch(args.out.resolve())
