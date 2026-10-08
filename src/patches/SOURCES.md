# Shared patches and pinned sources

Source releases and SHA-256 checksums live in `src/releases.tsv`. The builder
uses `releases.json` to select an immutable patch snapshot for each registered
kernel version. Snapshots are stored under `snapshots/<content-hash>/` and can
be shared by multiple versions. Each snapshot pins its upstream commit, ordered
patch checksums, local adaptations, and optional module configuration.

The validated 7.2.9 snapshot preserves the existing BORE and Debian headers fix
byte-for-byte, with no extra modules. Versions without a snapshot retain the
legacy series/common patch selection for manual maintenance.

## Current validated source

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

## Patch refresh on new Linux versions

`profile.json` selects the optional extras for future automated updates; its
default enables `handheld`, `aufs`, and `acpi-call`. BORE and the Debian headers
fix are always included. These settings do not retrofit older locked releases.

After discovering a newer stable kernel with a CachyOS source archive, the
updater pins the current `CachyOS/kernel-patches` commit, selects patches from the
matching kernel series, and verifies each downloaded Git blob. For AUFS it
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

The automatic trigger remains a newer Linux version in the selected series.
Patch-only commits and same-version source revisions do not trigger an update
build. New kernel series still require deliberate adoption and compatibility
review. Compilation and boot validation remain necessary before publication.
