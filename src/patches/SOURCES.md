# Shared patches and pinned sources

Source releases and SHA-256 checksums live in `src/releases.tsv`. The builder
derives the kernel series from the requested version and applies its BORE patch,
then the shared Debian headers patch. Stable releases in the same kernel series
reuse these files; there is no per-stable-version patch directory.

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

When updating the kernel, pin the new release and checksum in `src/releases.tsv`.
Reuse the series patches when they still apply; rebase them when necessary.
A new kernel series needs a matching BORE patch in its own series directory.
Review the shared headers patch and migrate the configuration through
`olddefconfig`, then validate compilation and boot behavior. Strict patch checks
establish that hunks apply, rather than proving runtime compatibility.
