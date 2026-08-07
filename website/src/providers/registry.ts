// Modified 2026 by Sereja Ris for VibecodersCrew (community fork of Kiro Crew).
// See NOTICE and CHANGELOG.md for the nature of the modifications.
import type { ProviderAdapter, ProviderId } from './types'
import { AcpAdapter } from './adapters/acp'
import { CodexAdapter } from './adapters/codex'

const ADAPTERS: Record<ProviderId, ProviderAdapter> = {
  acp: new AcpAdapter(),
  codex: new CodexAdapter(),
}

export function getAdapter(id: ProviderId = 'acp'): ProviderAdapter {
  return ADAPTERS[id] ?? ADAPTERS.acp
}
