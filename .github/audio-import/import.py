"""One-shot exact-source import. Creates immutable Git objects, never refs."""
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

ROOT = Path.cwd()
META = Path(os.environ['RUNNER_TEMP']) / 'audio-import-verified.json'

def git(*args, data=None):
    return subprocess.check_output(['git', *args], input=data, timeout=30).decode().strip()

def prepare():
    raw = Path('.github/audio-import/manifest.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != os.environ['MANIFEST_SHA256']:
        raise RuntimeError('Import manifest digest mismatch')
    manifest = json.loads(raw)
    if git('rev-parse', 'HEAD^') != manifest['base']:
        raise RuntimeError('Import parent moved: refusing to apply')
    if git('rev-parse', manifest['base'] + '^{tree}') != manifest['base_tree']:
        raise RuntimeError('Base tree mismatch')
    parts = sorted(Path('.github/audio-import').glob('part*.b64'))
    encoded = ''.join(p.read_text().strip() for p in parts)
    patch = gzip.decompress(base64.b64decode(encoded, validate=True))
    if hashlib.sha256(patch).hexdigest() != manifest['patch_sha256']:
        raise RuntimeError('Patch digest mismatch')
    subprocess.run(['git', 'checkout', '--detach', manifest['base']], check=True, timeout=30)
    subprocess.run(['git', 'apply', '--check', '--index', '-'], input=patch, check=True, timeout=30)
    subprocess.run(['git', 'apply', '--index', '-'], input=patch, check=True, timeout=30)
    expected = {f['path']: f for f in manifest['files']}
    paths = git('diff', '--cached', '--name-only').splitlines()
    if set(paths) != set(expected):
        raise RuntimeError('Changed paths differ from reviewed manifest')
    for path in paths:
        mode, sha, stage, _ = git('ls-files', '-s', '--', path).split(None, 3)
        if mode != expected[path]['mode'] or sha != expected[path]['sha'] or stage != '0':
            raise RuntimeError('Source identity mismatch: ' + path)
    manifest['tree_sha'] = git('write-tree')
    META.write_text(json.dumps(manifest))
    print('Verified exact source identities for', len(paths), 'files; tree', manifest['tree_sha'])

def upload():
    manifest = json.loads(META.read_text())
    if git('write-tree') != manifest['tree_sha'] or git('diff', '--name-only'):
        raise RuntimeError('Tests modified source; refusing import')
    repo = os.environ['GITHUB_REPOSITORY']
    if repo not in ('aslater3/LibreEcho-UI', 'aslater3/LibreEcho-Platform'):
        raise RuntimeError('Unexpected repository')
    api = 'https://api.github.com/repos/' + repo + '/git/'
    def post(kind, payload):
        request = urllib.request.Request(api + kind, data=json.dumps(payload).encode(), method='POST',
            headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                     'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                     'X-GitHub-Api-Version': '2022-11-28'})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    elements = []
    for file in manifest['files']:
        data = Path(file['path']).read_bytes()
        identity = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if identity != file['sha']:
            raise RuntimeError('Working file changed: ' + file['path'])
        result = post('blobs', {'encoding': 'base64', 'content': base64.b64encode(data).decode()})
        if result['sha'] != identity:
            raise RuntimeError('Uploaded blob mismatch: ' + file['path'])
        elements.append({'path': file['path'], 'mode': file['mode'], 'type': 'blob', 'sha': identity})
    result = post('trees', {'base_tree': manifest['base_tree'], 'tree': elements})
    if result['sha'] != manifest['tree_sha']:
        raise RuntimeError('Remote tree differs from tested source')
    # No commit, ref, merge, release, repository setting or credential write.
    manifest['repository'] = repo
    (Path(os.environ['RUNNER_TEMP']) / 'audio-verified-tree.json').write_text(json.dumps(manifest, indent=2))
    print('Uploaded immutable tested tree', result['sha'], 'without changing any branch')

if sys.argv[1:] == ['prepare']:
    prepare()
elif sys.argv[1:] == ['upload']:
    upload()
else:
    raise SystemExit('Expected prepare or upload')
