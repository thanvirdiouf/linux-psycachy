# Shared patches and pinned sources

Source releases and SHA-256 checksums live in `src/releases.tsv`. The builder
uses `releases.json` to select an immutable patch snapshot for each registered
kernel version. Snapshots are stored under `snapshots/<content-hash>/` and can
be shared by multiple versions. Each snapshot pins its upstream commit, ordered
patch checksums, local adaptations, and optional module configuration.
New snapshots also contain the exact `linux-cachyos` PKGBUILD and upstream config,
with their repository commit and hashes. The builder applies `src/debian.config`
as its explicit distribution delta before `olddefconfig`.

The validated 7.2.9 snapshot preserves the existing BORE and Debian headers fix
byte-for-byte, with no extra modules. Versions without a snapshot retain the
legacy series/common patch selection for manual maintenance.

## Source validated with the earlier GCC build

The builder uses the CachyOS source release `cachyos-7.2.9-2`, which already
contains CachyOS's core changes, BBR3, and the ADIOS I/O scheduler. It does not
apply the archived Linux 6.17 patches in `src/*.patch`.
Unlike the archived patch, which replaced `tcp_bbr`, this release provides
BBR3 separately as `tcp_bbr3` (`CONFIG_TCP_CONG_BBR3=m` in our configuration).

- [Source release](https://github.com/CachyOS/linux/releases/tag/cachyos-7.2.9-2)
- Archive SHA-256: `2da9e6ffe31436657f46909db059059946dac22c2ac6cde05e49a66d61acfcba`
- [Upstream build recipe](https://github.com/CachyOS/linux-cachyos/blob/7ded3a8b97d197123452b7549fb59aa0c1c83e71/linux-cachyos/PKGBUILD)

`7.2/0001-bore-cachy.patch` is BORE 6.8.0 from
[CachyOS/kernel-patches](https://github.com/CachyOS/kernel-patches/blob/17bb0bb818d283d0dc6e0280a2e9d5b95e66808b/7.2/sched/0001-bore-cachy.patch).
Its first hunk is rebased around the `task_ipi_mask` definition added before
`task_struct`. No scheduler logic is changed.

`common/0002-debian-headers-config.patch` rebases PsyCachy's original `config.patch`
so the Debian headers package contains `.config` for external module builds.

## Toolchain transition

The maintained builder now defaults to Clang/LLVM with ThinLTO, using the LLVM
major version in `src/llvm-version`. This leaves the pinned sources and patch
snapshots unchanged. The earlier GCC build was boot-tested; that validation does
not establish compatibility for the new compiler and LTO settings. Full package,
boot, hardware, and external-module testing is required for Clang builds.

## Patch refresh and revised upstream inputs

`profile.json` selects the optional extras for future automated updates; its
default enables `handheld`, `aufs`, and `acpi-call`. BORE and the Debian headers
fix are always included. These settings do not retrofit older locked releases.

The updater checks newer stable kernels, same-version source revisions, and
changes to selected recipe/config/patch inputs. It matches a pinned upstream
recipe to the exact source tag, records the recipe and config bytes, selects
patches from the matching series, and verifies each downloaded Git blob. For AUFS it
selects the newest dated patch matching the series. Only explicitly selected
patches are applied; other files are listed in the PR for review.

The updater dry-run checks each patch with zero fuzz, then applies it to a fresh
source extraction in order: BORE, ACPI-call, AUFS, handheld, and Debian headers.
It carries forward the known `task_ipi_mask` BORE context adjustment only when
the original hunk and source context match exactly. Other conflicts fail the
update and require review; patches are not silently omitted.

Successful preparation produces a content-addressed snapshot and an entry in
`releases.json` tied to the source archive checksum. The builder verifies this
lock, applies its optional configuration, and checks after `olddefconfig` that
all selected module settings survived. Configuration changes also invalidate
prepared-source fingerprints. Do not edit a saved snapshot in place: changing
its patch files or metadata fails checksum validation.

Relevant patch/config/recipe-only changes trigger proposals; unrelated upstream
changes do not. Each replacement increments `debian_revision` and saves the
previous source-bound lock in history. Old snapshots stay immutable. New kernel
series still require deliberate adoption and compatibility review. Compilation
and boot validation remain necessary before publication.

## Candidate source: Linux 7.2.10

- [CachyOS source release](https://github.com/CachyOS/linux/releases/tag/cachyos-7.2.10-1)
- Archive SHA-256: `02af8a6115596d20e9998926953f2040f8d7f66ca1122f399c5920a9601f1cef`
- Patch snapshot: `c3a5ac872ccd8404426b5ad3f1daaef82e145723f3902530a9a454bc0af9b2c8`
- Upstream patch commit: `17bb0bb818d283d0dc6e0280a2e9d5b95e66808b`
- Optional features: acpi-call, aufs, handheld.
- Local patch adaptations: `0001-bore-cachy.patch` (task_ipi_mask context only), `0004-handheld.patch` (amdgpu_dm millipercent declaration context only).

Registered by the updater. Compilation and boot validation must be reviewed
before describing this version as validated.

## Candidate source: Linux 7.2.10

- [CachyOS source release](https://github.com/CachyOS/linux/releases/tag/cachyos-7.2.10-1)
- Archive SHA-256: `02af8a6115596d20e9998926953f2040f8d7f66ca1122f399c5920a9601f1cef`
- Patch snapshot: `5914c2737b2935e6b94d3942cb65f83eac502657b679d3e7370a974737e8769f`
- Upstream patch commit: `17bb0bb818d283d0dc6e0280a2e9d5b95e66808b`
- Debian build revision: `2`
- [Upstream recipe snapshot](https://github.com/CachyOS/linux-cachyos/tree/5a14cc870dbe958da0fe3fff96d0df6561b8b064/linux-cachyos)
- Upstream config SHA-256: `3df33112e3357feb6a899bbfc006f2ded15226d1b2eb741ac986d581b9ca7773`
- Optional features: acpi-call, aufs, handheld.
- Local patch adaptations: `0001-bore-cachy.patch` (task_ipi_mask context only), `0004-handheld.patch` (amdgpu_dm millipercent declaration context only).

Registered by the updater. Compilation and boot validation must be reviewed
before describing this version as validated.
