# Signed APT delivery

Build bundles contain versioned kernel images and headers plus matching tracking
packages. Install `linux-image-psycachy-llvm` and `linux-headers-psycachy-llvm` to
follow the default LLVM build, or `linux-image-psycachy` and
`linux-headers-psycachy` for GCC. Minimal-profile tracking names end in `-minimal`
and remain separate from the full profile. Tracking packages depend on one exact
kernel package version, so APT upgrades install its matching image and headers.
`linux-libc-dev` and optional kernel debug symbols are excluded from the normal
APT channel and from the default local installer.

## Prepare a repository locally

Install `python3`, `dpkg-dev`, and `gnupg`. Obtain the complete verified build
bundles, including `SHA256SUMS`, from the published releases you want to retain.
Keep at least the previous working kernel available. Use an existing dedicated
archive signing key; the tool does not create a key or establish user trust.

```bash
python3 scripts/apt_repository.py \
  --packages /path/to/previous-build \
  --packages /path/to/current-build \
  --output /path/to/new-apt-repository \
  --signing-key YOUR_FULL_SIGNING_KEY_FINGERPRINT \
  --suite stable
```

The output directory must not already exist. Add `--gnupghome /path/to/keyring`
when the signing key is outside the default keyring. For an encrypted key, supply
its passphrase through the `PSYCACHY_APT_KEY_PASSPHRASE` environment variable.
The builder verifies bundle checksums, matching kernel/tracking metadata, and both
new signatures before completing. It rejects conflicting package bytes with the
same package name, version, and architecture. Its output contains `pool/`,
`dists/`, a binary public key, and the signing fingerprint; no secret key is
copied into the output.

Upload the reviewed output to your chosen HTTPS static host as a whole. This
repository does not assume a hosting account or publish to one. Preserve every
package referenced by the new indexes while switching metadata; deploy metadata
last or use an atomic hosting release. The indexes include `by-hash` copies for
APT clients. Rebuild and replace signed metadata before its `Valid-Until` date
(default 14 days, configurable with `--valid-days` up to 90), even when package
contents have not changed.

## Prepare a repository in GitHub Actions

The manual **Prepare signed APT repository artifact** workflow downloads the
selected published release tags, signs a static repository, and uploads a
seven-day review artifact. It does not deploy the artifact. All supplied releases
must contain tracking packages; older bundles can be prepared locally with
`scripts/package_meta.py` first, then given fresh checksums after verifying their
original checksums.

Configure a protected GitHub environment named `apt-signing` with:

- Secret `APT_SIGNING_KEY`: the ASCII armored dedicated archive secret key.
- Secret `APT_SIGNING_KEY_PASSPHRASE`: its passphrase, when encrypted.
- Variable `APT_SIGNING_FINGERPRINT`: the full key fingerprint.

Restrict the environment to the default branch and require the repository's normal
release review policy. The workflow itself also rejects other branch refs. Use
all release tags you intend to retain when rebuilding; an omitted tag is omitted
from the new repository. Signing configuration and a hosting destination must be
supplied by the operator before users can receive updates from a live channel.

## Configure clients after hosting

Replace `https://packages.example.invalid/psycachy` with the actual HTTPS host.
Distribute the public key fingerprint through an authenticated channel. Download
the public key and compare `gpg --show-keys --with-fingerprint` output against that
fingerprint before installing it. A fingerprint file downloaded beside the key
alone cannot establish its authenticity.

```bash
curl -fsSLo psycachy-archive-keyring.gpg \
  https://packages.example.invalid/psycachy/psycachy-archive-keyring.gpg
gpg --show-keys --with-fingerprint psycachy-archive-keyring.gpg
sudo install -m 0644 psycachy-archive-keyring.gpg /usr/share/keyrings/psycachy-archive-keyring.gpg
printf '%s\n' 'deb [arch=amd64 signed-by=/usr/share/keyrings/psycachy-archive-keyring.gpg] https://packages.example.invalid/psycachy stable main' \
  | sudo tee /etc/apt/sources.list.d/psycachy.list
sudo apt update
sudo apt install linux-image-psycachy-llvm linux-headers-psycachy-llvm
```

The archive key authenticates APT packages. Secure Boot kernel signing and MOK
enrollment remain a separate setup, documented by the main repository.
