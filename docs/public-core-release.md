# Public core technical preview / 军团核心公开技术预览

## What this distribution contains

This is the independent Legion core behind the DSH Legion connector: the
existing service, native member adapters, collaboration, persistent task
context, research and knowledge interfaces, continuation, formal artifact
delivery, selected shared-tool source, and the standalone instance launcher.
It is not a replacement DSH host or a second agent written above the members.

The DSH connector remains a separate package. External harnesses, native CLIs,
media models, device services and accounts are installed and configured on
demand. Source inclusion does not mean every capability is ready to execute.

## Public source, not private repository history

The public repository is `seanliii/meyo-legion-core`. Its initial commit is a
source snapshot, not a fork containing private repository history.
The first-party portion is MIT; [third-party notices](../THIRD_PARTY_NOTICES.md)
retain the bundled Codex resource's Apache-2.0 license and NOTICE.

The release archive is produced by the existing `scripts/package_core.py`
from a clean, complete public commit:

```sh
python3.12 -I -B scripts/package_core.py build \
  --repo "$PWD" --commit "$FULL_PUBLIC_COMMIT" \
  --output "$OUTPUT/meyo-legion-core-0.1.0.tar" \
  --distribution public-source --license-id MIT \
  --license-file LICENSE --notice-file THIRD_PARTY_NOTICES.md
```

The release includes the archive and its SHA-256. `CORE-PACKAGE.json` inside
the archive records the public commit and every source file's original
bytes, mode, size, SHA-256 and Git blob ID. Source files are at the archive
root; no Git history or instance is inside it. The default private archive
format and old release bytes are unchanged.

## Installation

Use the public release's fixed archive or clone the public repository at a
fixed release tag. Verify the archive's SHA-256 against the release before
extracting it to a new directory. The existing
[packaging guide](core-private-package.md) describes the trusted verifier
and extractor; do not execute an untrusted package to certify itself.

From the extracted or checked-out source, follow the [README](../README.md):

1. Create a Python 3.12 virtual environment and install `requirements-core.txt`.
2. Initialize a private instance outside the source directory.
3. Start the existing service. An unconfigured model remains unconfigured.
4. Use `meyo.py dsh-config` to obtain the service address and private token-file
   path. Enter the references, not the token value, in DSH Legion's connection
   settings.
5. Configure your own model/provider and install the native members needed for
   your task. Neither the source archive nor the connector supplies accounts.

The supported launcher environment is macOS/Linux with Python 3.12. A
technical preview is not a claim of Windows support, unattended success on
arbitrary tasks, or a performance win over every individual harness.

## Verification scope

Release evidence must distinguish public download and hash verification,
fresh dependency installation, service startup, DSH connection, actual tool
execution, and real model tasks. Historical model, device or artifact
journeys in other documents keep their original version and date; they are
not silently rerun by publishing a source archive.

The files under `docs/plans/`, `LOCAL-CANDIDATE.md`, and dated personal-runtime
verification documents are historical development records. They are not
instructions to use the maintainer's private instance or credentials.
