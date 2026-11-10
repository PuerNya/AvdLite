"""Resolve official sources, derive cache identities, and verify downloaded content."""
import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

INDEX = 'https://dl.google.com/android/repository/sys-img/google_apis/sys-img2-4.xml'
KSU = 'https://api.github.com/repos/tiann/KernelSU/releases/latest'


def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'AvdLite/1.0'})
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def available_images(xml, api='latest', page_size='auto'):
    if api != 'latest' and (not re.fullmatch(r'\d+(?:\.\d+)?', api) or int(api.split('.')[0]) < 35):
        raise ValueError('Version must be latest or API 35 and above, such as 35, 36.1, or 37.2')
    tags = {'auto': {'google_apis', 'google_apis_ps16k'}, '4k': {'google_apis'}, '16k': {'google_apis_ps16k'}}
    if page_size not in tags:
        raise ValueError('Page size must be auto, 4k, or 16k')
    candidates = []
    for package in ET.fromstring(xml).findall('remotePackage'):
        path = package.attrib['path']
        match = re.fullmatch(r'system-images;android-(\d+(?:\.\d+)?);(google_apis(?:_ps16k)?);x86_64', path)
        if not match or package.find('obsolete') is not None:
            continue
        version, tag = match.groups()
        if int(version.split('.')[0]) < 35 or tag not in tags[page_size] or api not in ('latest', version):
            continue
        channel = package.find('channelRef')
        if package.find('revision/preview') is not None or (channel is not None and channel.get('ref') != 'channel-0'):
            continue
        archive = package.find('archives/archive/complete')
        if archive is None:
            continue
        checksum = archive.find('checksum')
        if checksum is None or checksum.attrib.get('type') not in ('sha1', 'sha256'):
            raise ValueError('Official image is missing a supported checksum')
        revision = '.'.join(package.findtext(f'revision/{name}', '0') for name in ('major', 'minor', 'micro'))
        item = {'package': path, 'api': version, 'tag': tag, 'abi': 'x86_64', 'revision': revision,
                'page_size': '16k' if tag.endswith('ps16k') else '4k', 'size': int(archive.findtext('size')),
                'checksum_type': checksum.attrib['type'], 'checksum': checksum.text,
                'url': urllib.parse.urljoin(INDEX, archive.findtext('url'))}
        candidates.append(item)
    ordered = sorted(candidates, key=lambda p: (tuple(map(int, p['api'].split('.'))), p['page_size'] == '16k', tuple(map(int, p['revision'].split('.')))))
    return list({image['package']: image for image in ordered}.values())


def select_image(xml, api='latest', page_size='auto'):
    candidates = available_images(xml, api, page_size)
    if not candidates:
        raise ValueError('Official repository has no stable image for the selected version and page size')
    return candidates[-1]


def resolve(api, page_size, enable_ksu):
    image = select_image(fetch(INDEX), api, page_size)
    release = None
    if enable_ksu:
        data = json.loads(fetch(KSU))
        if data['draft'] or data['prerelease']:
            raise ValueError('KernelSU latest must be a stable release')
        release = {'tag': data['tag_name'], 'url': data['html_url'], 'assets': []}
        for asset in data['assets']:
            name = asset['name']
            if (name == 'ksud-x86_64-unknown-linux-musl' or name.startswith('KernelSU_') and name.endswith('-release.apk')
                    or name.startswith('lkm-x86_64-') and name.endswith('_kernelsu.ko')):
                digest = asset.get('digest', '')
                if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
                    raise ValueError(f'KernelSU asset is missing SHA-256: {name}')
                release['assets'].append({'name': name, 'url': asset['browser_download_url'], 'sha256': digest[7:]})
        if not any(a['name'] == 'ksud-x86_64-unknown-linux-musl' for a in release['assets']):
            raise ValueError('Latest KernelSU release is missing the Linux x86_64 tool')
    return {'image': image, 'ksu': release}


def download(url, path, algorithm, expected):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    def digest(target):
        with target.open('rb') as stream:
            return hashlib.file_digest(stream, algorithm).hexdigest()
    if path.exists() and digest(path) == expected:
        return path
    temporary = path.with_suffix(path.suffix + '.partial')
    subprocess.run(['curl', '--fail', '--location', '--retry', '5', '--retry-all-errors',
                    '--connect-timeout', '30', '--speed-limit', '1024', '--speed-time', '90',
                    '--continue-at', '-', '--output', str(temporary), url], check=True)
    if digest(temporary) != expected:
        raise ValueError(f'Download checksum verification failed: {path.name}')
    temporary.replace(path)
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('resolve', 'download'))
    parser.add_argument('--api', default='latest')
    parser.add_argument('--page-size', default='auto')
    parser.add_argument('--ksu', action='store_true')
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--cache', type=Path)
    args = parser.parse_args()
    if args.command == 'resolve':
        manifest = resolve(args.api, args.page_size, args.ksu)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, indent=2) + '\n')
        image = manifest['image']
        outputs = {'image_key': f"google-{image['checksum']}", 'api': image['api'], 'page_size': image['page_size']}
        assets = manifest['ksu']['assets'] if manifest['ksu'] else []
        outputs['ksu_key'] = 'ksu-' + hashlib.sha256(json.dumps(assets, sort_keys=True).encode()).hexdigest()
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write(''.join(f'{key}={value}\n' for key, value in outputs.items()))
        print(json.dumps({'image': image, 'ksu': manifest['ksu']['tag'] if manifest['ksu'] else None}, indent=2))
    else:
        manifest = json.loads(args.manifest.read_text())
        image = manifest['image']
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            jobs = [pool.submit(download, image['url'], args.cache / 'image/source.zip', image['checksum_type'], image['checksum'])]
            if manifest['ksu']:
                jobs.extend(pool.submit(download, asset['url'], args.cache / 'ksu' / asset['name'], 'sha256', asset['sha256']) for asset in manifest['ksu']['assets'])
            errors = []
            for job in jobs:
                try:
                    job.result()
                except Exception as error:
                    errors.append(str(error))
            if errors:
                raise RuntimeError('; '.join(errors))


if __name__ == '__main__':
    main()
