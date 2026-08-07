# Modifications (Apache-2.0 §4(b))

VibecodersCrew is an independent community fork of
[KiroCrew](https://github.com/kirodotdev/KiroCrew) (Apache-2.0).

Modified 2026 by Sereja Ris. Not affiliated with Amazon or OpenAI.

## Nature of modifications

1. **OpenAI Codex provider** — App Server integration using an existing ChatGPT/Codex login.
2. **Product identity** — rebranded to VibecodersCrew (`tech.serejaris.vibecoderscrew`); repository `serejaris/vibecoderscrew`.
3. **Telemetry** — outbound product telemetry hard-disabled (beacon, install receipts, local product metrics, OTLP, Electron profiling).
4. **Embeddings** — no default CDN first-install download; explicit URL or local path required.
5. **Distribution** — source-only public releases; public CI does not upload unsigned desktop/wheel artifacts.
6. **Upstream automation** — Amazon signing/CDN/update lanes are not used for community distribution.

## Notice form

Modified source files carry a short header comment pointing here and to `NOTICE` / `CHANGELOG.md`. Binary and pure-JSON assets are covered by this file and `NOTICE`.

## Technical identifiers retained

- Python import path `kiro_crew` (mergeability with upstream).
- CLI alias `kirocrew` (compatibility); primary entry point is `vibecoderscrew`.
- Default data home `~/.kiro/crew` (existing installs); override with `KIROCREW_HOME`.

These are technical paths, not the public product name.
