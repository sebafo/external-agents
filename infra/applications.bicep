targetScope = 'resourceGroup'

param prefix string
param environment string = 'dev'
param location string = resourceGroup().location
param environmentName string
param registryServer string
param openAiEndpoint string
param openAiDeployment string
@allowed(['maf', 'langchain', 'all'])
param agentSelection string = 'all'
@description('Map of selected agent names to fully qualified image digests.')
param images object

var selectedAgents = agentSelection == 'all' ? ['maf', 'langchain'] : [agentSelection]
module agents 'modules/agent.bicep' = [for agent in selectedAgents: {
  name: '${prefix}-${environment}-${agent}-app'
  params: {
    name: '${prefix}-${environment}-${agent}'
    agent: agent
    identityName: '${prefix}-${environment}-${agent}-identity'
    location: location
    environmentName: environmentName
    registryServer: registryServer
    image: images[agent]
    openAiEndpoint: openAiEndpoint
    openAiDeployment: openAiDeployment
  }
}]

output agents array = [for (agent, i) in selectedAgents: agents[i].outputs.agent]
