import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from avdlite import tools


def package(name, major=1, channel=0, host='linux', checksum='a' * 40):
    return f'''<remotePackage path="{name}"><revision><major>{major}</major></revision>
      <channelRef ref="channel-{channel}"/><archives><archive><host-os>{host}</host-os>
      <host-arch>x64</host-arch><complete><checksum type="sha1">{checksum}</checksum>
      </complete></archive></archives></remotePackage>'''


def repository(*extra):
    return '<repository>' + ''.join(package(name) for name in tools.SDK_PACKAGES) + ''.join(extra) + '</repository>'


class SDKCacheTests(unittest.TestCase):
    def test_latest_stable_revisions_and_archive_checksums_define_identity(self):
        baseline = tools.sdk_packages(repository())
        self.assertEqual(baseline, tools.sdk_packages(repository(
            package('emulator', major=99, channel=2), package('unrelated', major=9))))
        newer = tools.sdk_packages(repository(package('emulator', major=2)))
        self.assertEqual(newer['emulator']['revision'], (2, 0, 0))
        self.assertNotEqual(baseline, newer)
        self.assertNotEqual(baseline, tools.sdk_packages(repository().replace('a' * 40, 'b' * 40)))
        self.assertEqual(newer, tools.sdk_packages('<repository>' + package('emulator', major=2)
                                                 + ''.join(package(n) for n in tools.SDK_PACKAGES) + '</repository>'))

    def test_rejects_missing_unstable_wrong_host_or_invalid_packages(self):
        for xml in ('<repository/>', repository().replace('channel-0', 'channel-1'),
                    repository().replace('linux', 'windows'), repository().replace('x64', 'aarch64'),
                    repository().replace('a' * 40, 'invalid'),
                    repository().replace('<revision>', '<obsolete/><revision>'),
                    repository().replace('</revision>', '<preview>1</preview></revision>')):
            with self.subTest(xml=xml), self.assertRaises(ValueError):
                tools.sdk_packages(xml)

    def test_installed_sdk_must_match_resolved_revisions(self):
        packages = tools.sdk_packages(repository())
        with tempfile.TemporaryDirectory() as folder:
            sdk = Path(folder)
            for name in tools.SDK_PACKAGES:
                target = sdk.joinpath(*name.split(';')) / 'package.xml'
                target.parent.mkdir(parents=True)
                target.write_text(f'<repository><localPackage path="{name}"><revision><major>1</major>'
                                  '</revision></localPackage></repository>')
            tools.verify_sdk(sdk, packages)
            packages['emulator']['revision'] = (2, 0, 0)
            with self.assertRaisesRegex(ValueError, 'Installed SDK revision'):
                tools.verify_sdk(sdk, packages)


class AVBCacheTests(unittest.TestCase):
    def test_cold_warm_and_corrupt_cache_always_check_checksums(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / 'tools.json'
            tool, key = b'avb tool fixture', b'public key fixture'
            config.write_text(json.dumps({'avb_revision': 'fixture',
                'avbtool_sha256': hashlib.sha256(tool).hexdigest(),
                'avb_testkey_sha256': hashlib.sha256(key).hexdigest()}))
            with patch.object(tools, 'CONFIG', config), patch.object(tools, 'fetch') as fetch:
                fetch.side_effect = [base64.b64encode(tool), base64.b64encode(key)]
                cached = tools.prepare(root / 'work')
                self.assertEqual(fetch.call_count, 2)
                fetch.reset_mock(side_effect=True)
                self.assertEqual(tools.prepare(root / 'work'), cached)
                fetch.assert_not_called()
                (cached / 'avbtool.py').write_bytes(b'corrupt')
                fetch.return_value = base64.b64encode(b'wrong download')
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    tools.prepare(root / 'work')
                fetch.return_value = base64.b64encode(tool)
                tools.prepare(root / 'work')
                self.assertEqual((cached / 'avbtool.py').read_bytes(), tool)


if __name__ == '__main__':
    unittest.main()
