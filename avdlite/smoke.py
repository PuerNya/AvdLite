"""Cold boot, reboot, and verify an isolated AVD, then stop the test emulator."""
import argparse
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path


class Device:
    def __init__(self, sdk, evidence, env):
        self.adb = str(sdk / 'platform-tools/adb')
        self.evidence = evidence
        self.env = env
        self.process = None

    def call(self, *args, check=True, timeout=30):
        result = subprocess.run([self.adb, '-s', 'emulator-5554', *args], capture_output=True, text=True, timeout=timeout, env=self.env)
        if check and result.returncode:
            raise RuntimeError(result.stderr + result.stdout)
        return result.stdout.strip()

    def shell(self, *args, **kwargs):
        return self.call('shell', shlex.join(args), **kwargs)

    def wait(self, probe, description, timeout=420):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f'Emulator exited early with code {self.process.returncode}')
            try:
                value = probe()
            except subprocess.TimeoutExpired:
                value = None
            if value:
                return value
            time.sleep(3)
        raise TimeoutError(description)

    def ready(self):
        self.wait(lambda: self.shell('getprop', 'sys.boot_completed', check=False) == '1', 'Timed out waiting for Android boot')
        self.wait(lambda: 'package:' in self.shell('pm', 'path', 'com.android.systemui', check=False), 'Timed out waiting for the package manager')
        self.wait(lambda: self.shell('am', 'get-started-user-state', '0', check=False) == 'RUNNING_UNLOCKED', 'Timed out waiting for user 0 to unlock')

    def reboot(self):
        before = self.shell('cat', '/proc/sys/kernel/random/boot_id')
        boot_id = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
        if not re.fullmatch(boot_id, before):
            raise ValueError('Invalid boot_id before reboot: ' + before)
        try:
            self.shell('svc', 'power', 'reboot', check=False, timeout=60)
        except subprocess.TimeoutExpired:
            pass

        def changed_boot_id():
            value = self.shell('cat', '/proc/sys/kernel/random/boot_id', check=False)
            return value if re.fullmatch(boot_id, value) and value != before else None

        after = self.wait(changed_boot_id, 'Timed out waiting for a new boot_id')
        self.ready()
        return {'before': before, 'after': after}

    def capture(self, name, *args):
        text = self.shell(*args, check=False, timeout=60)
        (self.evidence / name).write_text(text)
        return text


def verify_reboot(device):
    expected = {'ui_night_mode': '3', 'dark_theme_custom_start_time': '79200000000', 'dark_theme_custom_end_time': '21600000000'}

    def schedule():
        return {key: device.shell('settings', '--user', '0', 'get', 'secure', key) for key in expected}

    device.shell('cmd', 'uimode', 'night', 'custom_schedule')
    for key in ('dark_theme_custom_start_time', 'dark_theme_custom_end_time'):
        device.shell('settings', '--user', '0', 'put', 'secure', key, expected[key])
    device.wait(lambda: schedule() == expected, 'Night schedule was not applied before reboot')
    boot_ids = device.reboot()
    actual = schedule()
    if actual != expected:
        raise AssertionError('Night schedule persistence verification failed: ' + repr(actual))
    return {'reboot': boot_ids, 'night_schedule_after_reboot': actual}


def verify(sdk, work, manifest):
    sdk, work = sdk.resolve(), work.resolve()
    image = work / 'output/x86_64'
    evidence = work / 'evidence'
    evidence.mkdir()
    avds = work / 'avds'
    avd = avds / 'AvdLiteCI.avd'
    avd.mkdir(parents=True)
    (avds / 'AvdLiteCI.ini').write_text(f'avd.ini.encoding=UTF-8\npath={avd}\ntarget=android-{manifest["image"]["api"]}\n')
    values = json.loads((Path(__file__).resolve().parent.parent / 'resources/avd-config.json').read_text())
    values.update({'AvdId': 'AvdLiteCI', 'avd.ini.displayname': 'AvdLiteCI', 'image.sysdir.1': str(image) + '/',
                   'hw.gpu.mode': 'swiftshader_indirect', 'fastboot.forceColdBoot': 'yes', 'fastboot.forceFastBoot': 'no'})
    (avd / 'config.ini').write_text(''.join(f'{k}={v}\n' for k, v in values.items()))
    emulator_home = work / 'emulator-home'
    emulator_home.mkdir()
    env = dict(os.environ, ANDROID_AVD_HOME=str(avds), ANDROID_EMULATOR_HOME=str(emulator_home),
               ANDROID_USER_HOME=str(emulator_home), QT_QPA_PLATFORM='offscreen')
    device = Device(sdk, evidence, env)
    subprocess.run([device.adb, 'start-server'], check=True, env=env)
    report = {'ksu_enabled': bool(manifest['ksu']), 'checks': {}}
    try:
        with (evidence / 'emulator.log').open('w') as log:
            command = [str(sdk / 'emulator/emulator'), '-avd', 'AvdLiteCI', '-port', '5554', '-no-window', '-no-audio',
                       '-no-boot-anim', '-no-snapshot', '-no-metrics', '-gpu', 'swiftshader_indirect', '-feature', '-Vulkan', '-accel', 'on', '-verbose']
            device.process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)
            device.ready()
            for package in ('com.google.android.gms', 'com.google.android.gsf', 'com.google.android.apps.nexuslauncher', 'com.google.android.inputmethod.latin'):
                if 'package:' not in device.shell('pm', 'path', package):
                    raise AssertionError('Core application verification failed: ' + package)
            report['checks']['core_packages'] = True
            if device.shell('getenforce') != 'Enforcing':
                raise AssertionError('SELinux must be Enforcing')
            report['checks']['selinux_enforcing'] = True
            features = device.capture('features.txt', 'pm', 'list', 'features')
            for feature in ('uwb', 'nfc'):
                port = device.shell('getprop', 'vendor.qemu.vport.' + feature)
                report['checks'][feature + '_port'] = port
                if not port and re.search(r'feature:android\.hardware\.' + feature + r'(?:[.=\s]|$)', features):
                    raise AssertionError('Hardware feature declared with its emulator port absent: ' + feature)
            for name in ('config_lockDayNightMode', 'config_lockUiMode'):
                value = device.shell('cmd', 'overlay', 'lookup', 'android', 'android:bool/' + name)
                if value != 'false':
                    raise AssertionError(name + ' lock state verification failed: ' + value)
            for name in ('status_bar_padding_start', 'status_bar_padding_end'):
                value = device.shell('cmd', 'overlay', 'lookup', 'com.android.systemui', 'com.android.systemui:dimen/' + name)
                if not re.fullmatch(r'12(?:\.0+)?(?:dip|dp)', value):
                    raise AssertionError('Status bar padding verification failed: ' + value)
            report['checks']['overlays'] = True
            if manifest['ksu']:
                device.wait(lambda: device.shell('getprop', 'sys.avdlite.ksu_ready', check=False) == '1', 'Timed out waiting for KernelSU initialization', timeout=120)
                uid = device.shell('su', '-c', 'id')
                if 'uid=0' not in uid:
                    raise AssertionError('KernelSU root verification failed')
                report['checks']['ksu_version'] = device.shell('su', '-c', '/data/adb/ksud debug version')
                if not device.shell('pm', 'path', 'me.weishu.kernelsu').startswith('package:/data/app/'):
                    raise AssertionError('KernelSU manager must be installed in the data partition')
            else:
                if 'package:' in device.shell('pm', 'path', 'me.weishu.kernelsu', check=False):
                    raise AssertionError('Standard image unexpectedly contains the KernelSU manager')
            report['checks'].update(verify_reboot(device))
            if manifest['ksu']:
                device.wait(lambda: device.shell('getprop', 'sys.avdlite.ksu_ready', check=False) == '1', 'Timed out waiting for KernelSU initialization after reboot', timeout=120)
                if 'uid=0' not in device.shell('su', '-c', 'id'):
                    raise AssertionError('Root verification failed after reboot')
            displays = device.shell('dumpsys', 'SurfaceFlinger', '--display-id')
            match = re.search(r'Display (\d+)', displays)
            if not match:
                raise AssertionError('Display ID query returned no display for the screenshot')
            with (evidence / 'screen.png').open('wb') as screenshot:
                subprocess.run([device.adb, '-s', 'emulator-5554', 'exec-out', 'screencap', '-d', match[1], '-p'], stdout=screenshot, check=True, timeout=30, env=env)
            device.capture('uimode.txt', 'dumpsys', 'uimode')
            device.capture('overlays.txt', 'cmd', 'overlay', 'list')
            report['status'] = 'passed'
    except Exception as error:
        report.update(status='failed', error=str(error))
        raise
    finally:
        if device.process is not None:
            device.capture('logcat.txt', 'logcat', '-d', '-b', 'all')
            device.capture('properties.txt', 'getprop')
            device.call('emu', 'kill', check=False)
            try:
                device.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                device.process.terminate()
                device.process.wait(timeout=10)
        (work / 'verification.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('sdk', 'work', 'manifest'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    verify(args.sdk, args.work, json.loads(args.manifest.read_text()))
