// Modified 2026 by Sereja Ris for VibecodersCrew (community fork of Kiro Crew).
// See NOTICE and CHANGELOG.md for the nature of the modifications.
import { AcpAdapter } from './acp'
import { i18nT } from '../../i18n/t'
import type {
  AgentBinding,
  ProviderCapabilities,
  ProviderLabels,
} from '../types'

/** UI adapter for the official Codex App Server backend.
 *
 * The backend keeps the normalized HTTP contracts implemented by AcpAdapter;
 * this subclass replaces the provider identity, labels, and capabilities.
 */
export class CodexAdapter extends AcpAdapter {
  readonly id = 'codex' as const
  readonly displayName = 'OpenAI Codex'
  readonly capabilities: ProviderCapabilities = {
    hooks: true,
    pluginRegistry: true,
    agentTemplates: false,
    usageBilling: false,
    warmPool: true,
    modelResolution: true,
    toolExecution: true,
    subagents: true,
    sandbox: true,
    contextWindow: true,
    modelSwitching: true,
    permissionModes: true,
    sessionResume: true,
    compaction: true,
    reasoningEffort: true,
  }

  readonly labels: ProviderLabels = {
    get sessionProcess() { return i18nT('pages.overview.kiroCrewCfgTab.provider_codex_session_process') },
    get agentTemplateField() { return i18nT('pages.overview.kiroCrewCfgTab.provider_codex_agent_template_field') },
    processCountLabel: 'codex_app_server',
    get warmPoolDescription() { return i18nT('pages.overview.kiroCrewCfgTab.provider_codex_warm_pool_description') },
    configFile: 'config.toml',
    get pluginRegistryName() { return i18nT('pages.overview.kiroCrewCfgTab.provider_codex_plugin_registry_name') },
    get hooksSection() { return i18nT('pages.overview.kiroCrewCfgTab.provider_codex_hooks_section') },
  }

  resolveAgentTemplate(agent: AgentBinding): string { return agent.name }
}
