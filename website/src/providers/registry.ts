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
