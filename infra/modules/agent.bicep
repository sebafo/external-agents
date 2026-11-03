param name string
param agent string
param identityName string
param location string
param environmentName string
param registryServer string
param image string
param openAiEndpoint string
param openAiDeployment string

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}
resource environment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 3978
        transport: 'http'
        allowInsecure: false
      }
      registries: [{ server: registryServer, identity: identity.id }]
    }
    template: {
      containers: [{
        name: agent
        image: image
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: [
          { name: 'APP_ENV', value: 'azure' }
          { name: 'PORT', value: '3978' }
          { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
          { name: 'AZURE_TENANT_ID', value: tenant().tenantId }
          { name: 'AZURE_OPENAI_ENDPOINT', value: openAiEndpoint }
          { name: 'AZURE_OPENAI_DEPLOYMENT_NAME', value: openAiDeployment }
        ]
        probes: [
          {
            type: 'Liveness'
            httpGet: { path: '/health/live', port: 3978 }
            initialDelaySeconds: 10
            periodSeconds: 10
          }
          {
            type: 'Readiness'
            httpGet: { path: '/health/ready', port: 3978 }
            initialDelaySeconds: 10
            periodSeconds: 10
            timeoutSeconds: 8
            failureThreshold: 12
          }
          {
            type: 'Startup'
            httpGet: { path: '/health/live', port: 3978 }
            periodSeconds: 5
            failureThreshold: 30
          }
        ]
      }]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

resource bot 'Microsoft.BotService/botServices@2022-09-15' = {
  name: '${name}-bot'
  location: 'global'
  kind: 'azurebot'
  sku: { name: 'F0' }
  properties: {
    displayName: '${agent} Teams Assistant'
    endpoint: 'https://${app.properties.configuration.ingress.fqdn}/api/messages'
    msaAppType: 'UserAssignedMSI'
    msaAppId: identity.properties.clientId
    msaAppTenantId: tenant().tenantId
    msaAppMSIResourceId: identity.id
    isStreamingSupported: false
  }
}
resource teams 'Microsoft.BotService/botServices/channels@2022-09-15' = {
  parent: bot
  name: 'MsTeamsChannel'
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
    properties: { isEnabled: true }
  }
}

output agent object = {
  name: agent
  appName: app.name
  appId: app.id
  botName: bot.name
  botClientId: identity.properties.clientId
  fqdn: app.properties.configuration.ingress.fqdn
  messagingEndpoint: bot.properties.endpoint
  image: image
}
