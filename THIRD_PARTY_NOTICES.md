# License scope and third-party notices

## First-party code

The maintainer selected the MIT license for the first-party Legion service,
orchestration and native adapters, retained auto-tools source, standalone
launcher, packaging tools, configuration declarations, tests and documentation.
See [LICENSE](LICENSE). This decision does not relicense third-party content.

This repository is a selected source distribution. It does not include the
original private monorepository or its Git history, instance data, credentials,
model weights, or the full source trees of the external harnesses.

## Bundled Codex instruction resource — Apache-2.0

`harness_baselines/meyo-orchestrator/resources/codex-0.153.4-fallback.json`
contains the Codex base instruction resource from `openai/codex`,
`rust-v0.153.4`, `codex-rs/models-manager/prompt.md`. It is an instruction
resource, not a bundled Codex executable or the full Codex source tree.

The resource file retains:

- the original instruction text, source URL, version and SHA-256;
- the full Apache License 2.0 text in `license.text`;
- the upstream NOTICE text in `notice.text`;
- the corresponding license and notice source URLs and hashes.

The text is stored inside a JSON envelope with provenance and binding metadata.
The embedded instruction, license and NOTICE bytes are preserved. Those
third-party contents remain under their original terms; the root MIT license
does not replace them.

## Penguin native-source compatibility patch — Apache-2.0

`release/legion-service/external_adapters/overlays/penguin-v0.2.4/`
contains a narrow source patch for `Prism-Shadow/penguin-harness`,
upstream revision `966af59018562657df0a9853671ff41e7b1ea027` (v0.2.4).
It preserves explicit permanent-error facts through normal requests and native
context compaction. The patch includes portions of the upstream source; its
original Apache License 2.0 is retained in that directory's `LICENSE`.
The two modified upstream files are identified by the patch and its adjacent
before/after hash manifest; the root MIT license does not relicense those contents.

The included first-party hook only checks, applies or reverses that exact source
patch in an exclusive staging checkout. It is not an installer and does not
replace anyone's installed Penguin, create a model account or certify all versions.
The source archive does not bundle the separately built Penguin executable,
its Node runtime, dependency store or private build records.

## DSH connector source

`connectors/dsh-plugin-legion/` is the first-party connector's canonical source,
with its own MIT `LICENSE` retained. Its package remains separately installable.
The DSH packages listed in its lockfile and peer dependencies retain their
upstream terms and are not bundled into this Git source tree.

## Separately installed dependencies and optional capabilities

Python requirements, npm lockfiles, wheel indexes, harness profiles and the
capability catalog declare dependencies; they do not mean those implementations,
binaries, models or accounts are included or installed.

In particular, the OpenMontage entry is an integration and a source-only
installation declaration. Its upstream implementation is not bundled here;
the declared upstream license is AGPL-3.0, not MIT. Installing or redistributing
that implementation is a separate action with its own terms.

The original license and attribution of every separately obtained dependency,
harness, skill, model or device SDK must be retained. An entry in a catalog,
a successful install, or a package hash is not a license grant for that
external project.

## Package metadata

Public source archives declare `distribution: "public-source"` and reference
this notice and the root MIT license from the same fixed source commit.
`rights_verified: false` means the packaging tool does not certify legal title.
It does not revoke the maintainer's license grant.
`member_execution_verified: false` means the source archive does not certify
execution of every optional member.
