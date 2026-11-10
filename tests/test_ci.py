import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from avdlite import ci
from avdlite.package import package
from test_sources import entry


class DailyImagesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repository', 'GITHUB_SHA': 'b' * 40})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def image(self, page='4k'):
        tag = 'google_apis_ps16k' if page == '16k' else 'google_apis'
        return ci.available_images('<repository>' + entry('35', tag) + '</repository>')[0]

    def plan(self, *pages):
        return [{'image': self.image(page), 'cached': None} for page in pages or ('4k',)]

    def bundle(self, name='build', page='4k'):
        work = self.root / name
        ci.prepare(work, self.image(page), None)
        output = work / 'output/x86_64'
        output.mkdir(parents=True)
        (output / 'system.img').write_bytes(b'filesystem-fixture')
        (output / 'build.prop').write_text('ro.build.version.release=15\n')
        (work / 'verification.json').write_text('{"status":"passed"}')
        (work / 'build-report.json').write_text('{}')
        package(work)
        ci.record(work, None)
        return work / 'artifact'

    def test_matrix_compares_each_api_and_page_sha1_after_parsing_index(self):
        cached = {'sha1': 'a' * 40, 'asset': 'existing.zip', 'sha256': 'c' * 64}
        changed = dict(cached, sha1='b' * 40)
        xml = '<repository>' + entry('34') + entry('35') + entry('35', 'google_apis_ps16k') + entry('36.1') + '</repository>'
        result = ci.matrix(ci.available_images(xml), {'35-4k': cached, '35-16k': changed, '34-4k': cached})['include']
        self.assertEqual([(p['image']['api'], p['image']['page_size']) for p in result], [('35', '4k'), ('35', '16k'), ('36.1', '4k')])
        self.assertEqual([p['cached'] for p in result], [cached, None, None])
        for invalid in ('<repository/>', xml.replace('type="sha1"', 'type="sha256"'), xml.replace('a' * 40, 'bad')):
            with self.assertRaises(ValueError):
                ci.matrix(ci.available_images(invalid), {})

    def test_first_build_disables_ksu_and_page_sizes_do_not_collide(self):
        names = []
        for page in ('4k', '16k'):
            work = self.bundle(page, page)
            self.assertIsNone(json.loads((work.parent / 'manifest.json').read_text())['ksu'])
            archive, digest = ci.check_bundle(next((work / 'release').glob('*.zip')), self.image(page))
            self.assertTrue(archive.name.endswith('-' + page + '.zip'))
            self.assertEqual(len(digest), 64)
            names.append(archive.name)
        self.assertNotEqual(*names)

    def test_unchanged_image_downloads_and_verifies_release_bundle(self):
        built = self.bundle()
        cached = json.loads((built / 'state/35-4k.json').read_text())
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            ci.check_bundle(built / 'release' / cached['asset'], self.image(), '0' * 64)

        def download(arguments, **kwargs):
            destination = Path(arguments[arguments.index('--dir') + 1])
            for path in (built / 'release').iterdir():
                shutil.copyfile(path, destination / path.name)
            return subprocess.CompletedProcess(arguments, 0)

        with patch.object(ci.subprocess, 'run', side_effect=download) as run:
            self.assertTrue(ci.prepare(self.root / 'reuse', self.image(), cached))
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[:4], ['gh', 'release', 'download', 'images'])
        self.assertIn(cached['asset'], arguments)
        self.assertIn(cached['asset'][:-4] + '.sha256', arguments)
        ci.record(self.root / 'reuse', cached)
        artifact = self.root / 'reuse/artifact'
        self.assertFalse((artifact / 'release').exists())
        with patch.object(ci.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            ci.publish(artifact, self.root / 'cache.json', [{'image': self.image(), 'cached': cached}])
        self.assertEqual([call.args[0][2] for call in run.call_args_list], ['view', 'edit'])

    def test_reuse_rejects_corruption_wrong_source_ksu_and_failed_boot(self):
        work = self.bundle()
        release = work / 'release'
        archive = next(release.glob('*.zip'))
        with archive.open('ab') as stream:
            stream.write(b'corrupted')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            ci.check_bundle(archive, self.image())
        for field, value in (('checksum', 'b' * 40), ('ksu', {'tag': 'test'}), ('status', 'failed')):
            with self.subTest(field=field):
                manifest = {'image': self.image(), 'ksu': None}
                verification = {'status': 'passed'}
                if field == 'checksum':
                    manifest['image'][field] = value
                elif field == 'ksu':
                    manifest[field] = value
                else:
                    verification[field] = value
                with zipfile.ZipFile(archive, 'w') as content:
                    content.writestr('manifest.json', json.dumps(manifest))
                    content.writestr('verification.json', json.dumps(verification))
                digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                archive.with_suffix('.sha256').write_text(f'{digest}  {archive.name}\n')
                with self.assertRaisesRegex(ValueError, 'source or boot'):
                    ci.check_bundle(archive, self.image())

    def test_missing_release_or_mismatched_cache_never_silently_reuses(self):
        work = self.bundle()
        cached = json.loads((work / 'state/35-4k.json').read_text())
        with patch.object(ci.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['gh'])):
            with self.assertRaises(subprocess.CalledProcessError):
                ci.prepare(self.root / 'missing', self.image(), cached)
        with self.assertRaisesRegex(ValueError, 'identity'):
            ci.prepare(self.root / 'wrong', self.image(), dict(cached, asset='../escape.zip'))

    def test_publish_updates_only_assets_and_notes_then_records_sha1(self):
        work = self.bundle()
        cache = self.root / 'cache/state.json'
        with patch.object(ci.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            ci.publish(work, cache, self.plan())
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([command[2] for command in commands], ['view', 'upload', 'edit'])
        self.assertIn('--clobber', commands[1])
        self.assertNotIn('--target', commands[2])
        self.assertNotIn('--title', commands[2])
        self.assertEqual(json.loads(cache.read_text())['35-4k']['sha1'], 'a' * 40)
        self.assertTrue((work / 'release-notes.md').read_text().startswith('For saving images without KSU\n\nBuild at:\n'))

    def test_both_page_artifacts_merge_and_next_plan_reuses_them(self):
        merged = self.root / 'publish'
        for page in ('4k', '16k'):
            work = self.bundle(page, page)
            for name in ('release', 'state'):
                shutil.copytree(work / name, merged / name, dirs_exist_ok=True)
        cache = self.root / 'cache.json'
        with patch.object(ci.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            ci.publish(merged, cache, self.plan('4k', '16k'))
        upload = run.call_args_list[1].args[0]
        for path in (merged / 'release').iterdir():
            self.assertIn(str(path), upload)
        xml = '<repository>' + entry('35') + entry('35', 'google_apis_ps16k') + '</repository>'
        next_run = ci.matrix(ci.available_images(xml), json.loads(cache.read_text()))['include']
        self.assertEqual(len(next_run), 2)
        self.assertTrue(all(row['cached'] for row in next_run))

    def test_mixed_run_uploads_only_changed_assets_and_rejects_changed_reuse_records(self):
        old = self.bundle('old')
        new = self.bundle('new', '16k')
        cached = json.loads((old / 'state/35-4k.json').read_text())
        shutil.copyfile(old / 'state/35-4k.json', new / 'state/35-4k.json')
        entries = self.plan('4k', '16k')
        entries[0]['cached'] = cached
        cache = self.root / 'cache.json'
        with patch.object(ci.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            ci.publish(new, cache, entries)
        upload = run.call_args_list[1].args[0]
        self.assertFalse(any(arg.endswith(cached['asset']) for arg in upload))
        self.assertEqual(len([arg for arg in upload if arg.endswith(('.zip', '.sha256'))]), 2)
        previous = cache.read_text()
        state = new / 'state/35-4k.json'
        state.write_text(json.dumps(dict(cached, sha256='0' * 64)))
        with patch.object(ci.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'published cache'):
                ci.publish(new, cache, entries)
        run.assert_not_called()
        state.write_text(json.dumps(cached))
        with patch.object(ci.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'release not found')) as run:
            with self.assertRaises(RuntimeError):
                ci.publish(new, cache, entries)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(cache.read_text(), previous)

    def test_plan_cli_parses_index_before_cache_and_writes_matrix_output(self):
        cache, output = self.root / 'cache.json', self.root / 'github-output'
        xml = '<repository>' + entry('35') + '</repository>'
        cache.write_text('{')
        with patch.object(sys, 'argv', ['ci', 'plan', '--cache', str(cache)]), patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
            with patch.object(ci, 'fetch', return_value=xml) as fetch, patch.object(ci, 'available_images', wraps=ci.available_images) as parse:
                with self.assertRaises(json.JSONDecodeError):
                    ci.main()
                fetch.assert_called_once_with(ci.INDEX)
                parse.assert_called_once_with(xml)
            cache.write_text('{}')
            with patch.object(ci, 'fetch', return_value=xml), contextlib.redirect_stdout(io.StringIO()):
                ci.main()
        key, value = output.read_text().strip().split('=', 1)
        self.assertEqual(key, 'matrix')
        self.assertEqual(json.loads(value)['include'], self.plan())

    def test_first_release_created_once_and_publication_failure_preserves_cache(self):
        work = self.bundle()
        cache = self.root / 'cache.json'
        missing = subprocess.CompletedProcess([], 1, '', 'release not found')
        success = subprocess.CompletedProcess([], 0)
        with patch.object(ci.subprocess, 'run', side_effect=[missing, success, success, success]) as run:
            ci.publish(work, cache, self.plan())
        self.assertEqual(run.call_args_list[1].args[0][2], 'create')
        self.assertIn('--latest=false', run.call_args_list[1].args[0])
        previous = cache.read_text()
        for effects in ([subprocess.CompletedProcess([], 1, '', 'HTTP 403')],
                        [missing, subprocess.CalledProcessError(1, ['gh'])],
                        [success, subprocess.CalledProcessError(1, ['gh'])],
                        [success, success, subprocess.CalledProcessError(1, ['gh'])]):
            with patch.object(ci.subprocess, 'run', side_effect=effects):
                with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
                    ci.publish(work, cache, self.plan())
            self.assertEqual(cache.read_text(), previous)

    def test_incomplete_or_corrupt_artifact_set_cannot_publish(self):
        work = self.bundle()
        cache = self.root / 'cache.json'
        with patch.object(ci.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'planned API'):
                ci.publish(work, cache, self.plan('4k', '16k'))
            with self.assertRaisesRegex(ValueError, 'planned API'):
                ci.publish(work, cache, self.plan('16k'))
            state = work / 'state/35-4k.json'
            original = state.read_text()
            item = json.loads(original)
            item['sha1'] = 'b' * 40
            state.write_text(json.dumps(item))
            with self.assertRaisesRegex(ValueError, 'source identity'):
                ci.publish(work, cache, self.plan())
            state.write_text(original)
            checksum = next((work / 'release').glob('*.sha256'))
            checksum.write_text('0' * 64 + '  ' + checksum.with_suffix('.zip').name)
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                ci.publish(work, cache, self.plan())
        run.assert_not_called()
        self.assertFalse(cache.exists())

    def test_publication_rechecks_bundle_source_even_when_all_digests_match(self):
        work = self.bundle()
        archive = next((work / 'release').glob('*.zip'))
        with zipfile.ZipFile(archive) as content:
            manifest = json.loads(content.read('manifest.json'))
        manifest['image']['page_size'] = '16k'
        with zipfile.ZipFile(archive, 'w') as content:
            content.writestr('manifest.json', json.dumps(manifest))
            content.writestr('verification.json', '{"status":"passed"}')
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.with_suffix('.sha256').write_text(f'{digest}  {archive.name}\n')
        state = work / 'state/35-4k.json'
        item = json.loads(state.read_text())
        item['sha256'] = digest
        state.write_text(json.dumps(item))
        with patch.object(ci.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'source or boot'):
                ci.publish(work, self.root / 'cache.json', self.plan())
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
