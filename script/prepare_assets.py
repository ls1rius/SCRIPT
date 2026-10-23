"""Download the local SCRIPT model snapshots and official EgoSchema videos.

Run with the project's vts interpreter. All model caches stay in checkpoints;
only EgoSchema files are extracted from the upstream LLoVi annotation archive.
Downloads are resumable and completed files are renamed atomically.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import re
import time
import zipfile

import requests

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault('HF_HOME', str(ROOT/'checkpoints/.cache/huggingface'))
os.environ.setdefault('HF_XET_CACHE', str(ROOT/'checkpoints/.cache/xet'))
MODELS = {
    'BAAI/EVA-CLIP-8B': ['*.json', '*.py', '*.txt', 'pytorch_model-*.bin'],
    'Qwen/Qwen2.5-VL-7B-Instruct': ['*.json', '*.txt', '*.safetensors'],
    'BAAI/bge-m3': ['config.json', 'pytorch_model.bin', 'tokenizer.json',
                    'tokenizer_config.json', 'special_tokens_map.json', 'sentencepiece.bpe.model'],
    'IDEA-Research/grounding-dino-tiny': ['*.json', '*.txt', '*.safetensors'],
}
DATA = ROOT / 'data/EgoSchema/egoschema'


def download(url, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        return
    partial = target.with_name(target.name + '.part')
    for attempt in range(5):
        try:
            offset = partial.stat().st_size if partial.exists() else 0
            headers = {'Accept-Encoding': 'identity'}
            if offset:
                headers['Range'] = f'bytes={offset}-'
            with requests.get(url, headers=headers, stream=True, timeout=(30, 180)) as response:
                if response.status_code == 416:
                    partial.unlink(missing_ok=True)
                response.raise_for_status()
                if 'text/html' in response.headers.get('Content-Type', ''):
                    raise RuntimeError(f'Download returned HTML instead of an asset: {target.name}')
                append = offset > 0 and response.status_code == 206
                if append and not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                    raise RuntimeError('Server returned the wrong resume range')
                expected = int(response.headers.get('Content-Length', 0)) if not response.headers.get('Content-Encoding') else 0
                received = 0
                last_report = time.monotonic()
                with partial.open('ab' if append else 'wb') as output:
                    for chunk in response.iter_content(8 * 1024 * 1024):
                        output.write(chunk)
                        received += len(chunk)
                        if time.monotonic() - last_report >= 30:
                            print(f'DOWNLOAD {target.name} {output.tell()/1e9:.2f} GB', flush=True)
                            last_report = time.monotonic()
                if expected and received != expected:
                    raise RuntimeError(f'Incomplete download: {received}/{expected}')
            partial.replace(target)
            print(f'DOWNLOADED {target} ({target.stat().st_size/1e9:.3f} GB)', flush=True)
            return
        except (requests.RequestException, RuntimeError) as exc:
            print(f'RETRY {target.name} ({attempt+1}/5): {exc}', flush=True)
            if attempt == 4:
                raise
            time.sleep(min(2 ** attempt, 15))


def models():
    from huggingface_hub import HfApi, snapshot_download
    receipts = {}
    for repo, patterns in MODELS.items():
        revision = HfApi().model_info(repo).sha
        print(f'MODEL {repo}@{revision}', flush=True)
        snapshot_download(repo, revision=revision, local_dir=ROOT/'checkpoints'/repo,
                          allow_patterns=patterns, max_workers=4)
        receipts[repo] = {'revision': revision, 'path': str((ROOT/'checkpoints'/repo).resolve())}
        (ROOT/'checkpoints/download_manifest.json').write_text(json.dumps(receipts, indent=2)+'\n')


def annotations():
    archive = DATA/'llovi-data.zip'
    download('https://drive.google.com/uc?export=download&id=13M10CB5ePPVlycn754_ff3CwnpPtDfJA', archive)
    with zipfile.ZipFile(archive) as source:
        for name in source.namelist():
            if '/egoschema/' in name.lower() and not name.endswith('/'):
                (DATA/Path(name).name).write_bytes(source.read(name))
    for name in ('questions.json', 'subset_answers.json'):
        download(f'https://raw.githubusercontent.com/egoschema/EgoSchema/main/{name}', DATA/name)
    for folder in ('egoschema_frames', 'egoschema_features'):
        (DATA.parent/folder).mkdir(parents=True, exist_ok=True)


def videos():
    from huggingface_hub import hf_hub_download
    # The official Google Drive archive can exceed its public download quota.
    # lmms-lab distributes the same UUID-named videos in five public zip files.
    repo = 'lmms-lab/EgoSchema'
    response = requests.get(f'https://huggingface.co/api/datasets/{repo}', timeout=30)
    response.raise_for_status()
    revision = response.json()['sha']
    destination = DATA/'videos'
    destination.mkdir(parents=True, exist_ok=True)
    def chunk(index):
        name = f'videos_chunked_{index:02d}.zip'
        archive = DATA/'archives'/name
        if not archive.is_file():
            hf_hub_download(repo, name, repo_type='dataset', revision=revision,
                            local_dir=DATA/'archives')
        count = 0
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                if not re.fullmatch(r'[0-9a-f-]{36}\.mp4', Path(member.filename).name):
                    continue
                target = destination/Path(member.filename).name
                if not target.exists() or target.stat().st_size != member.file_size:
                    partial = target.with_suffix('.mp4.part')
                    with source.open(member) as incoming, partial.open('wb') as output:
                        while data := incoming.read(8 * 1024 * 1024):
                            output.write(data)
                    partial.replace(target)
                count += 1
        print(f'EXTRACTED {name}: {count} videos', flush=True)
    with ThreadPoolExecutor(max_workers=5) as pool:
        for result in pool.map(chunk, range(1, 6)):
            pass
    expected = {q['q_uid'] for q in json.loads((DATA/'questions.json').read_text())}
    actual = {p.stem for p in destination.glob('*.mp4') if p.stat().st_size > 0}
    missing = sorted(expected-actual)
    report = {'expected': len(expected), 'present': len(actual), 'missing': missing,
              'source': f'https://huggingface.co/datasets/{repo}', 'revision': revision}
    (DATA/'download_report.json').write_text(json.dumps(report, indent=2)+'\n')
    if missing:
        raise RuntimeError(f'Missing {len(missing)} EgoSchema videos')
    print(f'VALIDATED {len(expected)} EgoSchema video files', flush=True)


class RemoteZip(io.RawIOBase):
    """Read zip headers and one member without waiting for the full archive."""
    def __init__(self, url):
        self.url, self.position = url, 0
        response = requests.get(url, headers={'Range': 'bytes=0-0', 'Accept-Encoding': 'identity'}, timeout=60)
        response.raise_for_status()
        self.size = int(response.headers['Content-Range'].split('/')[-1])

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        self.position = offset + (self.position if whence == 1 else self.size if whence == 2 else 0)
        return self.position

    def read(self, size=-1):
        end = self.size if size < 0 else min(self.size, self.position + size)
        if end <= self.position:
            return b''
        response = requests.get(self.url, headers={
            'Range': f'bytes={self.position}-{end-1}', 'Accept-Encoding': 'identity'}, timeout=120)
        response.raise_for_status()
        if response.status_code != 206 or len(response.content) != end-self.position:
            raise RuntimeError('Server did not honor zip byte range')
        self.position = end
        return response.content


def sample_video(uid='0a01d7d0-11d6-4af6-abd9-2025656d3c63'):
    target = DATA/'videos'/f'{uid}.mp4'
    if target.is_file():
        return
    for index in range(1, 6):
        url = f'https://huggingface.co/datasets/lmms-lab/EgoSchema/resolve/main/videos_chunked_{index:02d}.zip'
        with zipfile.ZipFile(RemoteZip(url)) as source:
            member = next((m for m in source.infolist() if Path(m.filename).name == target.name), None)
            if member:
                target.parent.mkdir(parents=True, exist_ok=True)
                partial = target.with_suffix('.mp4.sample-part')
                partial.write_bytes(source.read(member))
                partial.replace(target)
                print(f'SAMPLE_VIDEO {target}', flush=True)
                return
    raise RuntimeError(f'Smoke video {uid} not found in any EgoSchema archive')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('component', choices=['models', 'annotations', 'videos', 'sample-video', 'all'])
    parser.add_argument('--video-id', default='0a01d7d0-11d6-4af6-abd9-2025656d3c63',
                        help='Video UUID for sample-video')
    args = parser.parse_args()
    if args.component in ('annotations', 'all'):
        annotations()
    if args.component == 'all':
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(models), pool.submit(videos)]
            for future in futures:
                future.result()
    elif args.component == 'models':
        models()
    elif args.component == 'videos':
        videos()
    elif args.component == 'sample-video':
        sample_video(args.video_id)


if __name__ == '__main__':
    main()
