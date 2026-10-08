# PsyCachy kernel builder

This fork currently pins and validates Linux **7.2.9**. The original project was
archived; its notice and historical documentation are retained below.

Build on an **x86_64 Debian/Ubuntu system**:

```sh
./build.sh 7.2.9
```

The builder selects a source release from [src/releases.tsv](src/releases.tsv),
currently `cachyos-7.2.9-2`, verifies its
SHA-256, adds BORE 6.8.0 and the Debian headers configuration fix, and builds
with GCC. The configuration is migrated from PsyCachy's Debian/Ubuntu config
and targets generic x86-64 CPUs. Core CachyOS changes, BBR3, and ADIOS are already in
the source archive. See [source provenance](src/patches/SOURCES.md).
BBR3 is built as the `tcp_bbr3` module, with congestion-control name `bbr3`.
The migrated configuration uses full preemption with runtime selection enabled;
Linux 7.2 no longer offers the previous voluntary default in x86 Kconfig.

Missing build and packaging dependencies are installed through `sudo apt-get`.
Set `INSTALL_DEPS=0` to use a toolchain you have supplied yourself.
If `pahole` 1.26 or newer is unavailable, the builder disables BTF and sched_ext;
BORE still works, but BTF-dependent BPF programs need a newer `pahole` build.
To enable BTF, install `pahole` >= 1.26 before running the builder.

To check source preparation without compiling the complete kernel:

```sh
./build.sh 7.2.9 --prepare-only
```

The prepared tree is `src/build-7.2.9`; rerunning the builder reuses it after
checking its patch fingerprint. The fingerprint depends on patch contents and
order, so reorganizing patch paths does not invalidate a prepared build.
Existing builds from the previous layout are upgraded when their source and
patch contents match. Edit `src/config` to customize the configuration.
Set `JOBS=4 ./build.sh 7.2.9` to limit compilation parallelism. Packages are
written to `src/`; the builder does not install the resulting kernel.

Supported releases are listed in `src/releases.tsv`; currently that is 7.2.9.
Patch failures stop the build immediately without prompting for files.
If you previously ran the archived builder, its partially patched
`src/linux-7.2.9` directory is preserved and is not used by the new builder.

The documentation below describes the archived project, including its separate
`proto` branch builder; it does not describe this fork's `build.sh`.

Builder regression tests: `python3 -m unittest discover -s tests -v`.

## Reusing patches for new releases

Patches are organized by the kernel series they target:

```text
src/releases.tsv                         pinned source releases and checksums
src/patches/7.2/0001-bore-cachy.patch      BORE for the 7.2 kernel series
src/patches/common/0002-debian-headers-config.patch
                                        shared Debian packaging fix
```

To add a stable release in the same series, add its kernel version, exact CachyOS
release name, and verified archive SHA-256 to `src/releases.tsv`. No new patch
directory is needed for each stable version: the builder derives `7.2` from
`7.2.<stable-version>` and reuses the series and common patches.

For a different kernel series, supply the matching BORE patch under
`src/patches/<major.minor>/0001-bore-cachy.patch`, update its provenance, and
register the source release. The Debian packaging patch remains shared.
BORE depends on scheduler internals, so it cannot be assumed to apply to every
kernel series. Every patch is dry-run checked with zero fuzz before application.

Run `./build.sh <version> --prepare-only`, then compile and test the kernel before
considering a newly registered release validated. Adding a manifest entry alone
does not establish compatibility. `src/config` is migrated through `olddefconfig`
for the selected source; review configuration changes for new kernel series.

## GitHub Actions packages

The [Build Debian kernel packages](.github/workflows/build-debs.yml) workflow
validates the builder, then compiles and packages a pinned release on Ubuntu 24.04.
The default remains Linux 7.2.9. Manual runs accept a `kernel_version` input,
which must match an entry in `src/releases.tsv`.
It runs when build-related files change on `master` or `main`, on pull requests,
and manually through **Actions → Build Debian kernel packages → Run workflow**.
The workflow becomes available after these commits are pushed to GitHub.

After a successful run, open its **Artifacts** section and download
`psycachy-7.2.9-amd64-<run-id>-<attempt>`. It contains the image, headers, and
libc development `.deb` packages, `SHA256SUMS`, the resolved `kernel.config`,
and build metadata. Extract the artifact and run `sha256sum --check SHA256SUMS`
to verify the packages. Artifacts are retained for 14 days. A separate build-log
artifact is uploaded on success or failure.
For another selected version, the artifact name includes that version instead.

CI builds omit debug information, BTF, and sched_ext to keep disk usage manageable
on standard runners. They use `genksyms` for module versioning and retain generic
x86-64 hardware support, BORE, BBR3, and ADIOS. Local builds continue to use
`src/config` as configured above. The workflow uploads artifacts; it does not
publish a GitHub Release or install the kernel.

# Original archive notice

As of June 9th, 2026, this project has been archived as I no longer have the time to commit to it as my focus has been on LinuxToys. Feel free to fork it if you wish to continue it, and I'll be happy to help in any way I can. Below this message is the old readme.

# PsyCachy Kernel
This repository contains the releases of the `linux-psycachy` kernel, and the script for building that and custom variants. The script may automate the process of configuring and optimizing the kernel build according to your hardware and preferences.

# What's this about?
PsyCachy is a kernel with improved settings for compatibility and stability across Debian/Ubuntu Linux distributions derived from linux-cachyos. Releases are made targeting Ubuntu rolling and LTS with *dkms* module compatibility, since it's the most widely used version, but you are free to build it for other kernel versions and distributions for yourself like Debian using the `proto` branch.
### Differences to `linux-cachyos`
- Built with `gcc`, as `clang` caused too many inconsistencies due to `llvm` bugs.
- Doesn't include processor architecture-specific optimizations, as they bring too small gains to justify the time compiling them or the confusion caused to newcomers by multiple kernel versions on release - you can include those by building the kernel yourself running `cachyos-deb.sh` with `-b` option if you wish.
- Doesn't include handheld console drivers, as there isn't much of a point on doing it for Debian/Ubuntu.
- ~~OS/-o2 optimization instead of -o3, which caused quite a few problems with Debian/Ubuntu packages.~~ Now it builds -o3 by default, fixed.

# Recommended usage (for most people)
Install the kernel image of your choice from [Releases](https://github.com/psygreg/linux-psycachy/releases) or through [LinuxToys](https://github.com/psygreg/linuxtoys). 

## Manual installation
- Download **all three** .deb packages
- Open terminal in the same directory of the packages
- `sudo dpkg -i linux-image-psycachy_6.14.11-1_amd64.deb linux-headers-psycachy_6.14.11-1_amd64.deb linux-libc-dev_6.14.11-1_amd64.deb`, replacing 6.14.11 with your package version
- To install CachyOS SystemD configuration files as well to maximize effectiveness, download and run `cachyconfs.sh` available from *Releases* or [LinuxToys](https://git.linux.toys/psygreg/linuxtoys).

## Secure Boot
You can make the kernel compatible with Secure Boot by signing it using `create-key.sh` available from *Releases*. Remember to store the password you set when the keypair is created carefully as it will be required to import the MOK into BIOS.

# Building
## Prerequisites
Before running the script, ensure you have the following prerequisites installed:

- `libncurses-dev gawk flex bison openssl libssl-dev dkms libelf-dev libudev-dev libpci-dev libiberty-dev autoconf llvm gcc rustc`: for compiling the kernel.
- `whiptail`: For displaying dialog boxes in the script.
- `curl`: For fetching the latest kernel version.
- `devscripts` and `debhelper`: For packaging.

You can install these dependencies using your distribution's package manager, or have the build scripts install them for you. It is advisable to use *Ubuntu LTS* or *Debian Stable* for building to ensure better compatibility. You can use a `docker`, `podman` or `distrobox` container for that.

## Features
The builder in `proto` offers a variety of configuration options:

- Auto-detection of CPU architecture for optimization.
- Selection of CachyOS specific optimizations.
- Configuration of CPU scheduler, LLVM LTO, tick rate, and more.
- Support for various kernel configurations such as NUMA, NR_CPUS, Hugepages, and LRU.
- Application of O3 optimization and performance governor settings.

## Usage
To use the script to build your own kernel, follow these steps:

1. Clone the repository to your local machine.
2. Make the script executable with `chmod +x cachyos-deb.sh`.
3. Run the script with `./cachyos-deb.sh`.
4. Follow the on-screen prompts to select your desired kernel version and configurations, for:
   - Choose the kernel version.
   - Enable or disable CachyOS optimizations.
   - Configure the CPU scheduler, LLVM LTO, tick rate, NR_CPUS, Hugepages, LRU, and other system optimizations. You may want to check the [Advanced Configurations](#advanced-configurations) section for more details on these options.
   - Select the preempt type and tick type for further system tuning.
5. Compile and install.

### Launch options (for `cachyos-deb.sh` on `proto` branch)
- `-b`: builds a `psycachy`-variant kernel with optimizations specific to your CPU `MARCH`. 
- `-g`: builds a `psycachy` generic image from the latest kernel upstream release.
- `-l`: builds a `psycachy-lts` generic image from the latest LTS kernel upstream release.

## Advanced Configurations
The script includes advanced configuration options for users who want to fine-tune their kernel:

- **CachyOS Configuration**: Enable all optimizations from CachyOS. A kernel with this option enabled is not guaranteed to work.
- **CPU Scheduler**: Choose between different schedulers like Cachy, PDS, or none.
- **Tick Rate**: Configure the kernel tick rate according to your system's needs.
- **NR_CPUS**: Set the maximum number of CPUs/cores the kernel will support.
- **Hugepages**: Enable or disable Hugepages support.
- **LRU**: Configure the Least Recently Used memory management mechanism.
- **O3 Optimization**: Apply O3 optimization for performance improvement.
- **Performance Governor**: Set the CPU frequency scaling governor to performance.
- **Modprobed.db**: will use the database built from `modprobed.db` to only build drivers specific to your machine. **WARNING:** use the default kernel with `modprobed.db` up and running for at least a week before building with this option to make sure all drivers you need are on the database, and **always** keep a default (or `psycachy` generic package) kernel as a backup!

## Contributing
Contributions are welcome! If you have suggestions for improving the script or adding new features, please open an issue or submit a pull request.

## License
This project is licensed under the MIT License as upstream - see the LICENSE file for details.
