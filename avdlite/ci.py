"""Build changed Google images and keep verified bundles in a fixed release."""
import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path
from .sources import INDEX, available_images, fetch

TAG = 'images'


def matrix(images, state):
    entries = []
    for image in images:
        if image['checksum_type'] != 'sha1' or not re.fullmatch('[0-9a-f]{40}', image['checksum']):
            raise ValueError('Daily image caching requires an official SHA-1 checksum')
        cached = state.get(f'{image["api"]}-{image["page_size"]}')
        if cached and cached['sha1'] != image['checksum']:
            cached = None
        entries.append({'image': image, 'cached': cached})
    if not entries:
        raise ValueError('Official repository has no supported stable images')
    return {'include': entries}


def check_bundle(archive, image, expected=None):
    prefix = r'AvdLiteWithoutKsu-Android\d+(?:\.\d+)*-api' + re.escape(image['api'])
    if not re.fullmatch(prefix + '(?:-' + image['page_size'] + r')?\.zip', archive.name):
        raise ValueError('Image bundle name does not match its API and page size')
    checksum, filename = archive.with_suffix('.sha256').read_text().split()
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if filename != archive.name or checksum != digest or (expected is not None and expected != digest):
        raise ValueError('Image bundle SHA-256 verification failed')
    with zipfile.ZipFile(archive) as content:
        manifest = json.loads(content.read('manifest.json'))
        verification = json.loads(content.read('verification.json'))
    if (manifest['ksu'] is not None or verification.get('status') != 'passed'
            or any(manifest['image'][key] != image[key] for key in ('api', 'page_size', 'checksum_type', 'checksum'))):
        raise ValueError('Image bundle source or boot verification does not match')
    return archive, digest


def prepare(work, image, cached):
    work.mkdir(parents=True, exist_ok=True)
    (work / 'manifest.json').write_text(json.dumps({'image': image, 'ksu': None}, indent=2) + '\n')
    if cached is None:
        return False
    asset = cached['asset']
    pattern = r'AvdLiteWithoutKsu-Android\d+(?:\.\d+)*-api' + re.escape(image['api']) + '-' + image['page_size'] + r'\.zip'
    if cached['sha1'] != image['checksum'] or not re.fullmatch(pattern, asset):
        raise ValueError('Cached image identity does not match')
    release = work / 'release'
    release.mkdir()
    subprocess.run(['gh', 'release', 'download', TAG, '--repo', os.environ['GITHUB_REPOSITORY'],
                    '--dir', str(release), '--pattern', asset, '--pattern', asset[:-4] + '.sha256'], check=True)
    check_bundle(release / asset, image, cached['sha256'])
    return True


def record(work, cached):
    image = json.loads((work / 'manifest.json').read_text())['image']
    archives = list((work / 'release').glob('*.zip'))
    if len(archives) != 1:
        raise ValueError('Expected exactly one image bundle')
    archive, digest = check_bundle(archives[0], image, cached['sha256'] if cached else None)
    name = archive.name if archive.name.endswith('-' + image['page_size'] + '.zip') else archive.stem + '-' + image['page_size'] + '.zip'
    item = {'sha1': image['checksum'], 'asset': name, 'sha256': digest}
    if cached is not None and item != cached:
        raise ValueError('Reused image record does not match the published cache')
    artifact = work / 'artifact'
    if cached is None:
        release = artifact / 'release'
        release.mkdir(parents=True)
        archive.rename(release / name)
        (release / name).with_suffix('.sha256').write_text(f'{digest}  {name}\n')
    key = f'{image["api"]}-{image["page_size"]}'
    state = artifact / 'state'
    state.mkdir(parents=True)
    (state / (key + '.json')).write_text(json.dumps(item) + '\n')


def publish(directory, cache, entries):
    state = {path.stem: json.loads(path.read_text()) for path in (directory / 'state').glob('*.json')}
    expected = {f'{entry["image"]["api"]}-{entry["image"]["page_size"]}': entry for entry in entries}
    if not expected or len(expected) != len(entries) or state.keys() != expected.keys():
        raise ValueError('Release image records do not match the planned API and page sizes')
    release = directory / 'release'
    assets = []
    for key, entry in expected.items():
        item, image = state[key], entry['image']
        if (item['sha1'] != image['checksum'] or Path(item['asset']).name != item['asset']
                or not item['asset'].endswith('-api' + key + '.zip')):
            raise ValueError('Release image source identity does not match the plan')
        if entry['cached'] is not None:
            if item != entry['cached']:
                raise ValueError('Reused image record does not match the published cache')
            continue
        archive, _ = check_bundle(release / item['asset'], image, item['sha256'])
        assets.extend((archive, archive.with_suffix('.sha256')))
    if set(release.glob('*')) != set(assets):
        raise ValueError('Release contains unexpected files')
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    notes = directory / 'release-notes.md'
    notes.write_text(f'For saving images without KSU\n\nBuild at:\n{timestamp}\n')
    repository = os.environ['GITHUB_REPOSITORY']
    existing = subprocess.run(['gh', 'release', 'view', TAG, '--repo', repository], capture_output=True, text=True)
    if existing.returncode:
        if 'release not found' not in existing.stderr.lower() or any(entry['cached'] is not None for entry in entries):
            raise RuntimeError(existing.stderr)
        subprocess.run(['gh', 'release', 'create', TAG, '--repo', repository, '--target', os.environ['GITHUB_SHA'],
                        '--title', 'Images', '--latest=false', '--notes-file', str(notes)], check=True)
    if assets:
        subprocess.run(['gh', 'release', 'upload', TAG, *map(str, assets), '--repo', repository, '--clobber'], check=True)
    subprocess.run(['gh', 'release', 'edit', TAG, '--repo', repository, '--notes-file', str(notes)], check=True)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(state, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('plan', 'prepare', 'record', 'publish'):
        command = commands.add_parser(name)
        if name != 'plan':
            command.add_argument('--work', type=Path, required=True)
        if name in ('plan', 'publish'):
            command.add_argument('--cache', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'plan':
        images = available_images(fetch(INDEX))
        state = json.loads(args.cache.read_text()) if args.cache.exists() else {}
        result = matrix(images, state)
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('matrix=' + json.dumps(result, separators=(',', ':')) + '\n')
        print(json.dumps(result, indent=2))
    elif args.command == 'prepare':
        reused = prepare(args.work, json.loads(os.environ['IMAGE_JSON']), json.loads(os.environ['CACHED_JSON']))
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write(f'reused={str(reused).lower()}\n')
    elif args.command == 'record':
        record(args.work, json.loads(os.environ['CACHED_JSON']))
    else:
        publish(args.work, args.cache, json.loads(os.environ['IMAGE_MATRIX'])['include'])


if __name__ == '__main__':
    main()
