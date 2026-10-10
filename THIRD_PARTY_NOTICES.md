# Licensing scope and third-party notices

## Project contributions

Contributions made for this maintained project to `build.sh`, `scripts/`,
`tests/`, `packaging/`, the build and update workflows, and project documentation are licensed
under GNU GPL version 2 only (`GPL-2.0-only`). The full license text is in
[LICENSE](LICENSE). The GPL identifiers on maintained files describe the
project's distribution of those files; inherited material retains its original
terms and notices.

This grant applies only to rights held by the contributors. It does not change
third-party licenses or claim ownership of upstream work. Earlier versions
remain available under the terms under which they were published.

## Inherited builder and supporting material

The project inherits material from:

- [psygreg/linux-psycachy](https://github.com/psygreg/linux-psycachy), by psygreg,
  including the earlier builder, Debian/Ubuntu configuration, and supporting
  files.
- [CachyOS/linux-cachyos-deb](https://github.com/CachyOS/linux-cachyos-deb), whose
  builder names Laio O. Seman as maintainer.

Both upstream READMEs declare the projects MIT-licensed. Neither inspected
upstream tree includes the standalone `LICENSE` file referenced by its README.
This notice records those declarations and preserves attribution; it does not
invent missing copyright years or notices. Preserve any original notices that
accompany upstream material, including any recovered later.

Inherited material, including `src/config`, `src/cachyconfs.sh`, and `secureboot/`,
is not relabeled by the GPL license added for project contributions. Inherited
portions of maintained files likewise retain their upstream MIT terms.

The MIT permission terms are reproduced below:

> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
> of the Software, and to permit persons to whom the Software is furnished to do
> so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

## Kernel sources and patches

Linux is distributed as a whole under GPL version 2 only, with file-specific
licenses and exceptions described in the
[Linux kernel licensing rules](https://docs.kernel.org/process/license-rules.html).
Downloaded CachyOS kernel sources retain their upstream `COPYING`, `LICENSES/`,
and file notices.

Files in `src/patches/`, including immutable snapshots, retain the licensing
applicable to their upstream or kernel-derived content. Do not replace their
notices or change saved snapshots to add project license identifiers. Source
and patch provenance is recorded in [src/patches/SOURCES.md](src/patches/SOURCES.md)
and each snapshot's `patchset.json`.

A repository-level license does not relicense the kernel, its patches, external
modules, or settings downloaded by supporting scripts. Distribute those under
their applicable licenses, and provide corresponding source and build materials
when required. Adding these notices does not itself supply corresponding source
for binary kernel releases.

## NVIDIA integration

The optional `psycachy-nvidia-support` package contains this project's DKMS
configuration and build wrapper under GPL-2.0-only. It does not redistribute
NVIDIA drivers. CI downloads checksum-pinned NVIDIA open module sources to test
compilation; the resulting NVIDIA modules are not included in release packages.
Installed NVIDIA drivers retain their own upstream licenses and notices.
