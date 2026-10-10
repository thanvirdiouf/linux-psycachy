# PsyCachy Linux

Build CachyOS-based Linux kernels for **Debian and Ubuntu**, with generic
**x86-64** support, Clang/LLVM compilation with **ThinLTO**, and Debian packages
ready to install.
PsyCachy combines BORE scheduling with the CachyOS source tree's BBR3 congestion
control and ADIOS I/O scheduler. GitHub Actions handles package builds and
proposes updates when a newer supported stable kernel becomes available.

The earlier **GCC build of Linux 7.2.9** has been compiled, installed, and
boot-tested. The Clang/ThinLTO build and NVIDIA 595.99.02 open modules have passed
compilation checks, including a DKMS build; boot and GPU validation remain pending.
The default
version is recorded in [src/default-version](src/default-version), and all
registered versions are listed in [src/releases.tsv](src/releases.tsv).

## Features

- Clang/LLVM with ThinLTO for generic x86-64 hardware, plus an explicit GCC fallback.
- BORE scheduling, BBR3 as the `tcp_bbr3` module, and ADIOS from the CachyOS source.
- Kernel image, headers, and libc development `.deb` packages.
- Optional NVIDIA DKMS support package with matching LLVM dependencies.
- Verified source archives and immutable patch snapshots for registered builds.
- GitHub Actions builds, downloadable artifacts, and optional release publication.
- Automated update PRs with refreshed patches and configurable optional modules.

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
`--nvidia`, the optional support package is excluded. Older releases without
the installer can be installed with `sudo apt install ./linux-*.deb`.

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
[src/config](src/config) through `olddefconfig`, and writes packages to `src/`.
It does not install the resulting kernel.

Missing build and packaging dependencies are installed through `sudo apt-get`.
The default toolchain family is **LLVM 18**, recorded in
[src/llvm-version](src/llvm-version). The builder installs `clang-18`, `lld-18`,
and `llvm-18` alongside the packaging dependencies, and uses `LLVM=-18` with the
LLVM integrated assembler throughout configuration and compilation. Exact tool
versions are recorded in `TOOLCHAIN.txt`; distro package updates can change them.
Set `INSTALL_DEPS=0` to use tools you have supplied yourself. A local override
such as `LLVM_VERSION=19` requires a complete matching toolchain and separate
validation. Changing compiler versions requires a fresh build tree.

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
Their kernel images and headers can coexist. Clang package revisions use
`<version>-2`, while GCC uses `<version>-1`; `linux-libc-dev` remains a shared
package. Existing GCC object files are preserved.

Subsequent runs reuse the selected tree
when its source and patch fingerprints match. Patch order, patch contents, and
optional configuration changes are included in this check; moving patch files
alone does not invalidate a prepared build. Compiler and linker versions are
also checked before reuse.

Edit `src/config` to customize local builds. With `pahole` older than 1.26, the
builder disables BTF and sched_ext. Install `pahole` 1.26 or newer before building
if you need BTF-dependent BPF programs.

### External modules and DKMS

A Clang/ThinLTO kernel needs compatible tools when building external modules.
Use the selected toolchain consistently, for example `make LLVM=-18 LLVM_IAS=1`
with that kernel's headers. DKMS integrations may need package-specific build
settings; do not assume their default GCC commands will work. Validate the
external modules you use, especially proprietary drivers, before replacing your
working kernel. Building packages does not change system DKMS configuration.

### NVIDIA support

Clang builds also produce `psycachy-nvidia-support_<version>_amd64.deb`. This
package configures NVIDIA DKMS builds only for `*-psycachy-llvm` kernels. It reads
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

CI builds omit debug information, BTF, and sched_ext to fit standard runners.
They use Clang/LLVM with ThinLTO, retain generic x86-64 support, and use
`genksyms` for module versioning.
CI restores compiler and ThinLTO caches compatible with the installed LLVM
toolchain and saves them after successful builds and NVIDIA validation. Pull
requests can restore caches but do not save them. The run summary includes
compiler cache statistics and sampled CPU, available RAM, swap and I/O wait.
The diagnostic artifact contains `resources.csv`, `summary.json` and GNU time
output. Memory samples are host-wide; GNU time's maximum RSS is per process,
not total memory used by all compiler and linker jobs. Use these measurements
to compare runs before increasing job counts or changing the configuration.
Local builds use `src/config` with the builder's compatibility adjustments.

### Publish a release

On a manual run from `master`, check **Publish a GitHub Release after a successful build**.
The release job downloads that run's artifact, verifies its checksums, and
publishes the packages and metadata as release assets.

Release tags use `psycachy-<version>-build-<run-number>-<attempt>` and point to the
built commit. Release assets remain available after Actions artifacts expire.
Publication uses the repository secret `PSYCACHY_RELEASE_TOKEN`, which must
allow release creation and asset uploads. The release job runs only on `master`.

**Push, pull-request, and automated update builds upload artifacts without
publishing releases.** To publish an already completed build without recompiling,
download its artifact and attach the files to a release manually.

## Automated updates

The [Check kernel updates](.github/workflows/update-kernel.yml) workflow runs daily
at **03:23 UTC / 08:53 IST**, and can also be started manually. It follows the
kernel series in `src/default-version`: a default of `7.2.9` selects newer
`7.2.x` versions with matching CachyOS source releases.

For a new eligible version, the updater:

1. Downloads and verifies the exact CachyOS source archive.
2. Pins the current `CachyOS/kernel-patches` commit and downloads the selected
   patches for the same kernel series, verifying their Git blobs.
3. Checks patch application with zero fuzz, saves an immutable snapshot, and
   records the source, checksum, patch lock, and new default version.
4. Opens a `codex/linux-<version>` PR and builds its exact commit.

BORE and the Debian headers configuration fix are always included. Optional
features are selected through `src/patches/profile.json`:

```json
{"features": ["handheld", "aufs", "acpi-call"]}
```

Use any subset of these features for future updates. Handheld support enables
selected Steam Deck, ASUS Ally, MSI, Zotac, and audio modules. AUFS and ACPI-call
are also built as modules. Other upstream patches are listed in the update PR
for review and are not applied automatically.

Patch-only upstream commits, same-version CachyOS source revisions, and release
candidates do not trigger update builds. A new kernel series requires deliberate
adoption. Existing open or closed update PRs are skipped to avoid repeated builds.
Patch conflicts stop the update for review; selected patches are never silently
omitted.

Enable **Settings → Actions → General → Workflow permissions → Allow GitHub
Actions to create and approve pull requests**. The updater grants its job the
required write permissions, but does not approve or merge PRs.

Review the PR, build logs, and resolved configuration. Download and boot-test the
packages before merging. To retry a build after editing an update branch, run
**Build Debian kernel packages** manually on that branch with its candidate version.
Release publication remains a separate manual step.

## Maintenance and contribution

| File or directory | Purpose |
| --- | --- |
| `src/releases.tsv` | Pinned source releases and archive checksums |
| `src/default-version` | Default CI version and updater's selected kernel series |
| `src/config` | Base kernel configuration |
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
