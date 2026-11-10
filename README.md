# AvdLite

Build lightweight Android emulator images from official Google APIs x86_64 images, starting with Android 15 (API 35).

## Image features

- Remove optional applications while retaining GMS, WebView, the keyboard, launcher, and system dependencies.
- Unlock theme and rotation settings while preserving upstream language and time zone defaults.
- Set left and right status bar padding to 12 dp.
- Gate UWB and NFC on emulator ports: start services when ports exist and hide feature declarations when ports are absent.
- Build release images without KernelSU.
- Check filesystems, rebuild partition metadata, and sign AVB for an independent system image.

## Daily image archive

**Actions → Daily Images Without KSU** runs daily at 00:00 UTC (08:00 China time), or manually on the default branch. It queries Google's official index on every run for stable Google APIs x86_64 images from API 35 onward and includes every available 4 KB and 16 KB variant.

The `avdlite.ci` script compares each source ZIP's SHA-1 with the last successfully published records in Actions cache. New or changed sources are built without KernelSU and must pass cold boot and reboot verification. Unchanged images are downloaded from the fixed `images` release and checked against their cached SHA-256, source identity, and boot verification report. Missing or invalid reused assets fail the run. A missing cache starts a fresh build.

The release contains every selected ZIP and its `.sha256` file. Daily asset names include the page size, for example `AvdLiteWithoutKsu-Android17-api37.0-4k.zip` and `AvdLiteWithoutKsu-Android17-api37.0-16k.zip`. After all images succeed, publication checks every record against the planned sources and uploads only changed assets, then updates the release description without moving its tag. Reused bundles stay in the release and only their records are passed to publication. The SHA-1 cache is saved only after publication succeeds.

The Android version comes from the official image's `build.prop`; the API version comes from the official repository index.

## Download and import

Download the images and matching `.sha256` files from the [Images release](https://github.com/PuerNya/AvdLite/releases/tag/images). Each ZIP contains a file checksum list, and GitHub provides a digest for each release asset.

Extract the ZIP and import it with Python 3.11 or later:

```text
python import-avd.py --sdk <Android-SDK-directory> --avd-home <AVD-data-directory>
```

The importer verifies checksums, installs the system image, and registers an independent AVD. The default AVD name and SDK image directory are `AvdLiteWithoutKsu`. Defaults are 2 CPU cores, 4096 MB RAM, 32 GB storage, a Resizable display, and Quick Boot. Use `--name` for a separate instance. Install the current stable Android Emulator in the selected SDK.

## Remote sources and caching

Image sources are resolved from the official Google index. Cloud runners download changed source images directly from Google and verify their official SHA-1 checksums before modification. Build tools come from official sources.

`.cache/` stores downloads; `.work/` stores local research and temporary files. Git tracks reviewed source files, resources, and documentation through an allowlist. Run the privacy check before pushing.

## Debloat rules

The [debloat manifest](resources/debloat.json) lists package identities, removal reasons, and retained dependencies. Build reports record actual removals and candidates absent from the selected image.

Hardware components remain installed. The UWB service starts once when its port property appears. Before the framework scans permissions, init mounts feature declarations that match the current port configuration, including vendor files and NFC APEX declarations. A targeted SELinux rule permits these mounts while retaining Enforcing mode.

## Verification

Build checks cover official checksums, GPT and dynamic partition metadata, the AVB public key and signature, ext4 filesystems, the original kernel, and upstream `build.prop` files.

Boot checks use an isolated AVD to verify core applications, SELinux Enforcing, hardware declarations, overlays, and the absence of the KernelSU manager. A reboot verifies dark theme schedule persistence. Each build saves logs, a screenshot, and JSON reports.
