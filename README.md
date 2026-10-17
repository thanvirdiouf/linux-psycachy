# PsyCachy Linux

Build CachyOS-based Linux kernels for **Debian and Ubuntu**, with generic
**x86-64** support, Clang/LLVM compilation with **ThinLTO**, and Debian packages
ready to install.
PsyCachy combines BORE scheduling with the CachyOS source tree's BBR3 congestion
control and ADIOS I/O scheduler. GitHub Actions handles package builds and
proposes updates for supported stable kernels, CachyOS source revisions, and
relevant upstream recipe, configuration, and patch changes.

The earlier **GCC build of Linux 7.2.9** has been compiled, installed, and
boot-tested. The Clang/ThinLTO build and NVIDIA 595.99.02 open modules have passed
compilation checks, including a DKMS build. The new full-profile build, pinned
upstream configuration, and revised installation/VM checks need a successful CI
run before this change can be described as validated. GPU validation remains pending.
The default
version is recorded in [src/default-version](src/default-version), and all
registered versions are listed in [src/releases.tsv](src/releases.tsv).

## Features

- Clang/LLVM with ThinLTO for generic x86-64 hardware, plus an explicit GCC fallback.
- BORE scheduling, BBR3 as the `tcp_bbr3` module, and ADIOS from the CachyOS source.
- Kernel image, headers, and tracking metapackages; distro userspace headers stay unchanged.
- Optional NVIDIA DKMS support package with matching LLVM dependencies.
- Verified source archives and immutable patch snapshots for registered builds.
- GitHub Actions builds, downloadable artifacts, and optional release publication.
- Automated update PRs with refreshed patches and configurable optional modules.
- Explicit full/minimal feature profiles and optional AutoFDO/Propeller inputs.
- Signed static APT repository preparation and local Secure Boot signing tools.

Future automated updates enable handheld support, AUFS, and ACPI-call by default.
These choices are configured in [src/patches/profile.json](src/patches/profile.json).
Older locked versions keep their existing patch selection; the validated 7.2.9
build does not include these extras.

## Download and install

Published packages are available under [Releases](https://github.com/thanvirdiouf/linux-psycachy/releases).
Successful workflow runs also provide packages under
[Actions](https://github.com/thanvirdiouf/linux-psycachy/actions).

Download that build's packages, `install-packages.sh`, and `SHA256SUMS` into
one directory. If downloading an Actions artifact, extract it first. Run:

```sh
sha256sum --check SHA256SUMS
bash install-packages.sh
```

For NVIDIA users who already have their distribution's NVIDIA DKMS driver installed:

```sh
bash install-packages.sh --nvidia
```

The installer first installs the optional `psycachy-nvidia-support` package and
its LLVM dependencies, then installs the kernel and headers. This order ensures
NVIDIA's modules build with the correct tools during installation. Without
`--nvidia`, the optional support package is excluded. The installer skips debug
packages and installs tracking packages when present. `linux-libc-dev` is excluded;
`--with-libc-dev` explicitly opts into replacing the distribution's userspace UAPI
headers, which kernel boot and DKMS do not require.

For automatic APT updates, keep `linux-image-psycachy-llvm` and
`linux-headers-psycachy-llvm` installed and configure a signed repository. The
[APT operator guide](packaging/apt/README.md) explains preparing and hosting one.
No public APT endpoint or signing key is supplied by this repository.

Reboot and select the installed kernel from your boot menu. Keep a working kernel
available while testing a new build. Compilation alone does not establish boot,
hardware, or DKMS compatibility.

## Build locally

Use an **x86_64 Debian/Ubuntu build host** with Python 3 and enough disk space
for the kernel source, compilation, and packages.

```sh
git clone https://github.com/thanvirdiouf/linux-psycachy.git
cd linux-psycachy
./build.sh 7.2.9
```

Replace `7.2.9` with a version registered in `src/releases.tsv`. The builder
verifies the pinned source archive, checks the selected patches, migrates
the pinned upstream configuration through `olddefconfig`, and applies the explicit
[Debian configuration delta](src/debian.config). Legacy snapshots without an
upstream configuration use [src/config](src/config).
It writes an isolated installable bundle under `src/packages/`, including
`SHA256SUMS` and `install-packages.sh`, and records its path in
`src/last-package-output`. It does not install the resulting kernel.

```sh
bash "$(cat src/last-package-output)/install-packages.sh"
```

Missing build and packaging dependencies are installed through `sudo apt-get`.
The default toolchain family is **LLVM 18**, recorded in
[src/llvm-version](src/llvm-version). The builder installs `clang-18`, `lld-18`,
and `llvm-18` alongside the packaging dependencies, and uses `LLVM=-18` with the
LLVM integrated assembler throughout configuration and compilation. Exact tool
versions are recorded in `TOOLCHAIN.txt`; distro package updates can change them.
Set `INSTALL_DEPS=0` to use tools you have supplied yourself. A local override
such as `LLVM_VERSION=19` requires a complete matching toolchain and separate
validation. Changing compiler versions requires a fresh build tree.

The default `BUILD_PROFILE=full` retains **BTF and sched_ext** and requires
`pahole` 1.26 or newer. On a host with an older encoder, build the pinned local
tool and pass its path explicitly (the host packages below are build dependencies):

```sh
sudo apt-get install cmake git libdw-dev libelf-dev libbpf-dev zlib1g-dev
bash scripts/build_pahole.sh "$PWD/.cache/tools"
PAHOLE="$PWD/.cache/tools/bin/pahole" ./build.sh 7.2.9
```

`BUILD_PROFILE=minimal ./build.sh 7.2.9` deliberately omits debug information,
BTF, and sched_ext. It uses separate build directories, kernel names, and tracking
packages with `-minimal`. The builder never silently removes these features.
Full builds retain DWARF for BTF generation but skip the large debug `.deb` unless
`BUILD_DEBUG_PACKAGE=1` is set.

To prepare sources without compiling the complete kernel:

```sh
./build.sh 7.2.9 --prepare-only
```

To limit compilation parallelism:

```sh
JOBS=4 ./build.sh 7.2.9
```

Clang builds limit each module link to one worker to avoid multiplying linker
threads across parallel make jobs. The main kernel link uses `JOBS` workers.
Override these independently with `MODULE_LINK_JOBS` and `KERNEL_LINK_JOBS`;
set `THINLTO_TUNING=0` to compare against the linker's default parallelism.
These controls preserve Kbuild's optimization and module linker flags.

Enable persistent compiler and ThinLTO caches for repeated builds:

```sh
BUILD_CACHE=1 JOBS=4 ./build.sh 7.2.9
```

This installs `ccache` when needed and stores caches in `.cache/ccache` and
`.cache/thinlto`, with a 2 GiB size target for each. ThinLTO pruning runs at most
once a minute, so its cache can temporarily exceed that target. Cached builds
use the Git commit timestamp unless `KBUILD_BUILD_TIMESTAMP` is supplied.
Compiler contents, inputs and flags determine cache validity. The first build
starts with an empty cache; changes to source or configuration can reduce reuse.
`BUILD-TUNING.txt` records the selected resource controls.

To build with GCC without LTO instead:

```sh
TOOLCHAIN=gcc ./build.sh 7.2.9
```

Clang builds use `src/build-<version>-clang-thinlto` and the kernel suffix
`-psycachy-llvm`; GCC builds retain `src/build-<version>` and `-psycachy`.
Their kernel images and headers can coexist. The effective revision `R` is
`src/package-revision + the release lock's debian_revision - 1`. Clang packages
use `<version>-2R` and GCC uses `<version>-(2R-1)`, preserving the old initial
revisions. For `R > 1`, kernel names and build directories also include `-rR`:
same-version patch/config updates therefore get fresh DKMS module directories.
Increment [src/package-revision](src/package-revision) for builder or local config
changes; the updater increments the release lock for upstream input changes.
`PACKAGE_REVISION` can override the builder revision for private rebuilds.
Existing bundle directories are never overwritten; move one aside to repeat an
identical build.

Subsequent runs reuse the selected tree
when its source and patch fingerprints match. Patch order, patch contents, and
optional configuration changes are included in this check; moving patch files
alone does not invalidate a prepared build. Compiler and linker versions are
also checked before reuse.

Edit `src/debian.config` to customize the delta applied to a pinned upstream
configuration. `OPTIMIZATIONS.json` records the feature profile, base-config hash,
and whether profiling inputs were supplied. Generic x86-64-v1 remains the portable
CPU target. Default builds do not use AutoFDO or Propeller profiles and do not
claim equivalence to CachyOS's profiled binaries.

To use profiles generated for your selected source/configuration/workload, set
`CLANG_AUTOFDO_PROFILE=/absolute/path/profile.afdo` and optionally
`CLANG_PROPELLER_PROFILE_PREFIX=/absolute/path/propeller`. The latter requires
`propeller_cc_profile.txt` and `propeller_ld_profile.txt`. The builder enables
and verifies the relevant Kconfig options, passes profiles to Kbuild, and records
their hashes. Unsupported compiler/profile combinations fail explicitly.
Profile quality and performance still require workload measurements.

BBR3 and ADIOS are available as modules. The configuration keeps `cubic` as the
default congestion control; installing this kernel does not select BBR3 or ADIOS
for your system. Configure runtime policy separately for your workload.

### External modules and DKMS

A Clang/ThinLTO kernel needs compatible tools when building external modules.
Use the selected toolchain consistently, for example `make LLVM=-18 LLVM_IAS=1`
with that kernel's headers. DKMS integrations may need package-specific build
settings; do not assume their default GCC commands will work. Validate the
external modules you use, especially proprietary drivers, before replacing your
working kernel. Building packages does not change system DKMS configuration.

### NVIDIA support

Clang builds also produce `psycachy-nvidia-support_<version>_amd64.deb`. This
package configures NVIDIA DKMS builds only for PsyCachy LLVM kernels, including
minimal and revision suffixes. It reads
the LLVM family from the installed headers and uses a wrapper to prevent DKMS
from replacing the versioned compiler/linker with unsuffixed defaults. Other
kernels keep their existing build settings. More-specific administrator DKMS
overrides still take precedence.

The support package supplies configuration and tool dependencies. Install your
distribution's driver package appropriate for your GPU first; it does not bundle
NVIDIA modules or choose a driver branch. Driver upgrades use the same scoped
configuration, although newer kernels can still require newer drivers. If an
existing `/etc/dkms/nvidia.conf` contains custom settings, installation stops
instead of overwriting them; back it up and merge the PsyCachy override.

Keep older LLVM families in [packaging/nvidia/llvm-versions](packaging/nvidia/llvm-versions)
when changing the default compiler. Increment [packaging/nvidia/version](packaging/nvidia/version)
when changing the helper so APT can upgrade it. The package retains its DKMS
configuration on removal as a Debian conffile; purge it to remove that file.
Without the helper's code, the retained file has no effect.

To build only the support package without recompiling the kernel:

```sh
python3 scripts/package_nvidia.py --output dist
```

CI compiles the pinned NVIDIA **open** modules against the generated headers
and checks every module's driver version and kernel vermagic. The pin and source
hash are in [src/nvidia-validation.json](src/nvidia-validation.json); update them
deliberately and rerun validation when changing the tested driver. This is a
compilation check, not a GPU boot, rendering, suspend, or Secure Boot test.
Closed/legacy driver branches are not covered by that check.

## GitHub Actions

The [Build Debian kernel packages](.github/workflows/build-debs.yml) workflow
runs regression tests, builds the kernel on Ubuntu 24.04, verifies the Debian
packages, and uploads artifacts. It runs for build-related pushes to `master`
or `main`, pull requests, and manual runs.

For a manual build, open **Actions → Build Debian kernel packages → Run workflow**.
Leave the kernel version blank to use `src/default-version`, or enter a registered
version.

The package artifact is named
`psycachy-<version>-amd64-<run-id>-<attempt>` and contains:

- The image, headers, and libc development `.deb` packages and `SHA256SUMS`.
- The optional NVIDIA support `.deb`, `install-packages.sh`, and `NVIDIA-VALIDATION.txt`.
- The resolved `kernel.config`, compiler versions in `TOOLCHAIN.txt`, and build metadata.
- `PATCHES.json` for locked builds, with patch provenance and configuration.

Artifacts are retained for **14 days**. A separate diagnostic artifact contains
the kernel/NVIDIA build logs and configuration, including when compilation fails.

CI defaults to the full feature profile, builds a pinned pahole on Ubuntu LTS,
and passes that encoder explicitly to every Kbuild invocation. The manual
`build_profile` input can select minimal. Both profiles use Clang/LLVM with ThinLTO
and retain generic x86-64 support. `OPTIMIZATIONS.json` and `PACKAGE-BUILD.json`
describe the exact output.
CI restores compiler and ThinLTO caches compatible with the installed LLVM
toolchain and saves them after successful builds and NVIDIA validation. Pull
requests can restore caches but do not save them. The run summary includes
compiler cache statistics and sampled CPU, available RAM, swap and I/O wait.
The diagnostic artifact contains `resources.csv`, `summary.json` and GNU time
output. Memory samples are host-wide; GNU time's maximum RSS is per process,
not total memory used by all compiler and linker jobs. Use these measurements
to compare runs before increasing job counts or changing the configuration.
After compilation, fresh **Debian 13 (trixie)** and **Ubuntu 24.04 (noble)** images
exercise package installation, kernel hooks, a small DKMS module, generated
initramfs, and direct QEMU boot. Serial logs and reports are separate diagnostic
artifacts. Release publication waits for both jobs. These checks do not establish
GPU, suspend/resume, firmware Secure Boot, or compatibility with other releases.
The new validation jobs have not been run until a workflow with this change passes.

### Publish a release

On a manual run from `master`, check **Publish a GitHub Release after a successful build**.
The release job downloads that run's artifact, verifies its checksums, and
publishes the packages and metadata as release assets.

Build-and-publish tags use `psycachy-<version>-build-<run-number>-<attempt>` and
point to the built commit. Release assets remain available after Actions artifacts expire.
Publication uses the repository secret `PSYCACHY_RELEASE_TOKEN`, which must
allow release creation and asset uploads. The release job runs only on `master`.

**Push, pull-request, and automated update builds upload artifacts without
publishing releases.** To publish an already completed build without recompiling,
run **Publish an existing kernel build** from the default branch and enter the
successful build or kernel update workflow's run ID. The publisher discovers the
kernel version and built commit from that run's package metadata; no version is
hardcoded. Pull-request and fork builds are excluded from promotion.

Before publication, it verifies the source provenance, package checksums, expected
Debian packages, patch snapshot, and NVIDIA validation report. It uploads a draft,
checks the release assets, and publishes a tag pointing to the exact built commit.
Promotion tags use `psycachy-<version>-run-<run-id>-<attempt>` so run counters from
different workflows cannot collide.
Repeating a promotion verifies the existing release rather than creating a duplicate.
The workflow uses `PSYCACHY_RELEASE_TOKEN` when configured, or its repository token
with Actions read and contents write permissions.

The same publisher can be used locally. To inspect a completed run without
creating a release, authenticate `gh` and run:

```bash
python3 scripts/publish_release.py --repo thanvirdiouf/linux-psycachy --run-id RUN_ID --dry-run
```

Remove `--dry-run` to publish the verified packages. The publisher makes one check
and stops if the build is not ready; it does not wait or poll.

## Automated updates

The [Check kernel updates](.github/workflows/update-kernel.yml) workflow runs daily
at **03:23 UTC / 08:53 IST**, and can also be started manually. It follows the
kernel series in `src/default-version`: a default of `7.2.9` selects newer
`7.2.x` versions with matching CachyOS source releases.

For eligible new or revised inputs, the updater:

1. Downloads and verifies the exact CachyOS source archive.
2. Pins a `linux-cachyos` recipe and its exact config for that source release,
   plus the selected `CachyOS/kernel-patches` files and their verified Git blobs.
3. Checks patch application with zero fuzz, saves an immutable snapshot, and
   records the source, checksum, patch lock, and new default version.
4. Increments the release's Debian revision, keeps previous locks in history,
   opens a uniquely identified `codex/linux-...` PR, and builds its exact commit.

BORE and the Debian headers configuration fix are always included. Optional
features are selected through `src/patches/profile.json`:

```json
{"features": ["handheld", "aufs", "acpi-call"]}
```

Use any subset of these features for future updates. Handheld support enables
selected Steam Deck, ASUS Ally, MSI, Zotac, and audio modules. AUFS and ACPI-call
are also built as modules. Other upstream patches are listed in the update PR
for review and are not applied automatically.

Same-version CachyOS revisions and relevant selected patch/config/recipe changes
trigger update proposals. Unselected patch changes and unrelated upstream commits
do not. Discovery and application must agree on the complete input identity.
Release candidates and new kernel series require deliberate adoption.
Existing open or closed PRs for that exact update identity are skipped.
Patch conflicts stop the update for review; selected patches are never silently
omitted.

Enable **Settings → Actions → General → Workflow permissions → Allow GitHub
Actions to create and approve pull requests**. The updater grants its job the
required write permissions, but does not approve or merge PRs.

Review the PR, build logs, and resolved configuration. Download and boot-test the
packages before merging. To retry a build after editing an update branch, run
**Build Debian kernel packages** manually on that branch with its candidate version.
Release publication remains a separate manual step.

## Secure Boot

The signing tool operates on an explicitly selected local kernel and local key;
it does not fetch code, guess release names, or modify bootloader settings.

```sh
sudo bash secureboot/create-key.sh --kernel-release 7.2.10-psycachy-llvm-r3 \
  --key-dir /root/.local/share/psycachy/secureboot --create-key --enroll
```

Use the actual release from `PACKAGE-BUILD.json` or `uname -r`. Install `openssl`,
`sbsigntool`, and, for enrollment, `mokutil` first. `--image /absolute/path/vmlinuz`
selects another image explicitly. Signing verifies the result, keeps an unsigned
backup, and atomically replaces the selected image. `--verify-only` checks it;
`--restore` restores the backup. Enrollment still requires completing the firmware
MOK screen. External/DKMS modules must also be signed with an enrolled trusted key;
signing the kernel image alone does not establish module trust.

## Maintenance and contribution

| File or directory | Purpose |
| --- | --- |
| `src/releases.tsv` | Pinned source releases and archive checksums |
| `src/default-version` | Default CI version and updater's selected kernel series |
| `src/config` | Base kernel configuration |
| `src/debian.config` | Explicit delta applied to pinned upstream configurations |
| `src/package-revision` | Builder/config revision, also separates kernel ABIs |
| `src/llvm-version` | Default LLVM major version for local and CI builds |
| `src/patches/profile.json` | Optional features for future automated updates |
| `src/patches/releases.json` | Source-bound kernel-to-snapshot locks |
| `src/patches/snapshots/` | Immutable, reusable patches and configuration |
| `src/patches/<series>/` and `src/patches/common/` | Fallback patches for manually registered versions without locks |

To discover an update locally without modifying files or downloading its archive:

```sh
python3 scripts/update_kernel.py
```

To register the discovered candidate with refreshed patches:

```sh
python3 scripts/update_kernel.py --apply
```

Inspect the resulting diff, prepare the candidate source, run the regression
tests, and compile and boot-test the packages before treating the version as
validated:

```sh
./build.sh <version> --prepare-only
python3 -m unittest discover -s tests -v
./build.sh <version>
```

Do not edit saved snapshots in place: their metadata and patch checksums are
verified. Profile changes affect future updates, while older releases keep their
locked snapshots. See [source and patch provenance](src/patches/SOURCES.md) for
upstream details and the known BORE context adjustment.

Issues and pull requests are welcome. Include the kernel version, build log,
resolved configuration, and relevant hardware details when reporting a problem.

## License

Project contributions to the builder, updater, tests, workflows, and documentation
are licensed under the **GNU General Public License version 2 only**
(`GPL-2.0-only`). See [LICENSE](LICENSE) for the full text.

Inherited MIT-licensed material retains its MIT terms and attribution. Linux
kernel sources, kernel-derived patches, and other third-party material retain
their applicable upstream licenses; this project's license does not replace
them. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the licensing scope
and upstream acknowledgements.

When distributing compiled kernel packages, provide the corresponding source,
patches, configuration, and build scripts as required by the applicable licenses.

## Acknowledgements

This project began as a fork of [psygreg/linux-psycachy](https://github.com/psygreg/linux-psycachy),
created by [psygreg](https://github.com/psygreg). Thank you to psygreg for the
original PsyCachy builder and Debian/Ubuntu kernel configuration.

Thanks also to [CachyOS](https://github.com/CachyOS) and the Linux kernel and patch
authors whose work makes these builds possible.
