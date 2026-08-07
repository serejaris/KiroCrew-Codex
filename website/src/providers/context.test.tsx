// Modified 2026 by Sereja Ris for VibecodersCrew (community fork of Kiro Crew).
// See NOTICE and CHANGELOG.md for the nature of the modifications.
import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'

import { ProviderProvider, useProvider } from './context'

vi.mock('../api/client', () => ({
  api: { kirocrewConfig: vi.fn(() => new Promise(() => undefined)) },
}))

function ProviderName() {
  return <div data-testid="provider">{useProvider().id}</div>
}

describe('ProviderProvider', () => {
  it('reacts to provider changes in the shared config cache', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    queryClient.setQueryData(['kirocrewConfig'], { agent: { provider: 'acp' } })
    render(
      <QueryClientProvider client={queryClient}>
        <ProviderProvider><ProviderName /></ProviderProvider>
      </QueryClientProvider>,
    )

    expect(screen.getByTestId('provider')).toHaveTextContent('acp')
    act(() => {
      queryClient.setQueryData(['kirocrewConfig'], { agent: { provider: 'codex' } })
    })
    await waitFor(() => {
      expect(screen.getByTestId('provider')).toHaveTextContent('codex')
    })
  })
})
