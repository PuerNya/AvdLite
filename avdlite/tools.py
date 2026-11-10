"""Resolve shared SDK cache identities and verify SDK and AVB tools."""
import argparse
import base64
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from .sources import fetch

CONFIG = Path(__file__).resolve().parent.parent / 'resources/tools.json'
SDK_INDEX = 'https://dl.google.com/android/repository/repository2-3.xml'
SDK_PACKAGES = ('build-tools;35.0.0', 'platforms;android-35', 'platform-tools', 'emulator')


def revision(package):
    return tuple(int(package.findtext(f'revision/{name}', '0')) for name in ('major', 'minor', 'micro'))


def sdk_packages(xml):
    packages = {}
    for package in ET.fromstring(xml).findall('remotePackage'):
        name = package.get('path')
        if (name not in SDK_PACKAGES or package.find('obsolete') is not None
                or package.find('revision/preview') is not None):
            continue
        channel = package.find('channelRef')
        if channel is not None and channel.get('ref') != 'channel-0':
            continue
        archives = [a for a in package.findall('archives/archive')
                    if a.findtext('host-os') in (None, 'linux')
                    and a.findtext('host-arch') in (None, 'x64', 'x86_64')
                    and a.findtext('host-bits') in (None, '64')]
        if len(archives) != 1:
            raise ValueError(f'Expected one Linux x86_64 SDK archive: {name}')
        checksum = archives[0].find('complete/checksum')
        if checksum is None or checksum.get('type') != 'sha1' or not re.fullmatch('[0-9a-f]{40}', checksum.text or ''):
            raise ValueError(f'SDK archive is missing an official SHA-1: {name}')
        item = {'revision': revision(package), 'sha1': checksum.text}
        if name not in packages or item['revision'] > packages[name]['revision']:
            packages[name] = item
    if set(packages) != set(SDK_PACKAGES):
        raise ValueError('Official repository is missing required stable SDK packages')
    return packages


def verify_sdk(sdk, packages):
    for name in SDK_PACKAGES:
        package = ET.parse(sdk.joinpath(*name.split(';')) / 'package.xml').getroot().find('localPackage')
        if package is None or package.get('path') != name or revision(package) != tuple(packages[name]['revision']):
            raise ValueError(f'Installed SDK revision does not match the cache identity: {name}')


def prepare(work):
    config = json.loads(CONFIG.read_text())
    tools = Path(work) / 'tools'
    tools.mkdir(parents=True, exist_ok=True)
    base = f"https://android.googlesource.com/platform/external/avb/+/refs/tags/{config['avb_revision']}/"
    for filename, remote, digest in (
        ('avbtool.py', 'avbtool.py', config['avbtool_sha256']),
        ('avb-testkey.pem', 'test/data/testkey_rsa4096.pem', config['avb_testkey_sha256']),
    ):
        path = tools / filename
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            content = base64.b64decode(fetch(base + remote + '?format=TEXT'), validate=False)
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError('AVB build tool checksum verification failed')
            path.write_bytes(content)
    return tools


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('resolve')
    verify = commands.add_parser('verify')
    verify.add_argument('--sdk', type=Path, required=True)
    verify.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'resolve':
        packages = json.dumps(sdk_packages(fetch(SDK_INDEX)), sort_keys=True, separators=(',', ':'))
        key = hashlib.sha256(packages.encode()).hexdigest()
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write(f'key={key}\npackages={packages}\n')
        print(packages)
    else:
        verify_sdk(args.sdk, json.loads(os.environ['SDK_PACKAGES']))
        prepare(args.work)


if __name__ == '__main__':
    main()
