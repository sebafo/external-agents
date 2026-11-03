using '../foundation.bicep'

param prefix = 'teamsdemo'
param environment = 'dev'
param location = 'westeurope'
// Replace with an existing model account ID before using this example directly.
param openAiResourceId = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/model-rg/providers/Microsoft.CognitiveServices/accounts/model-account'
