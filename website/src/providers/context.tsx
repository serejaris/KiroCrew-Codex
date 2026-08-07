// Modified 2026 by Sereja Ris for VibecodersCrew (community fork of Kiro Crew).
// See NOTICE and CHANGELOG.md for the nature of the modifications.
import { createContext, useContext, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { getAdapter } from './registry'
import type { ProviderAdapter, ProviderId } from './types'

const acpAdapter = getAdapter()

const ProviderContext = createContext<ProviderAdapter>(acpAdapter)

export function ProviderProvider({ children }: { children: ReactNode }) {
  const { data } = useQuery({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const provider = (data as { agent?: { provider?: ProviderId } } | undefined)?.agent?.provider
  const adapter = getAdapter(provider)

  return <ProviderContext.Provider value={adapter}>{children}</ProviderContext.Provider>
}

export function useProvider(): ProviderAdapter {
  return useContext(ProviderContext)
}
