# How to delete a deployment

This is a manual decommissioning guide. The deployment script does not delete
resources. Removing a Bicep deployment-history entry does **not** remove its
resources, and deploying fewer resources does not delete the old ones.

**Deletion is destructive.** This guide does not execute any deletion for you.
Use an approved change window and confirm ownership before running commands.

## 1. Decide what to remove

For a complete teardown, remove both Teams/Copilot apps, their Azure resources, and
their permissions on the existing model account.

| Component | Full teardown |
|-----------|---------------|
| Teams/Copilot catalog entries, installations, and app/agent pins | Remove separately in Microsoft 365/Teams |
| Two Container Apps and Azure Bots, including Teams channels | Delete |
| Two user-assigned managed identities | Delete after capturing principal IDs and removing permissions |
| Shared Container Apps environment, registry, and Log Analytics workspace | Delete only if dedicated to this deployment |
| Existing Foundry/Azure OpenAI account and model deployments | **Keep** |
| Unused storage from an older version | Review ownership/data, then delete if approved |
| Local configuration, ZIPs, and deployment outputs | Remove separately if no longer needed |

Deleting the registry destroys its stored images. Export any logs, release
metadata, or storage data required by retention policy first. In-memory
conversation context is lost when the containers stop.

## 2. Record and verify the targets

Use Azure CLI in Bash. Replace all placeholders; do not paste real values into
repository documentation.

```bash
export AZURE_SUBSCRIPTION_ID="<application-subscription-id>"
export AZURE_TENANT_ID="<tenant-id>"
export AZURE_RESOURCE_GROUP="<application-resource-group>"
export PREFIX="<deployment-prefix>"
export ENVIRONMENT="<environment>"
export MODEL_SUBSCRIPTION_ID="<model-subscription-id>"
export AZURE_FOUNDRY_PROJECT_ENDPOINT="https://<foundry-account>.services.ai.azure.com/api/projects/<project-name>"

az login --tenant "$AZURE_TENANT_ID"
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
az account show --query '{subscription:id,tenant:tenantId,name:name}' -o table

MODEL_ACCOUNT="$(printf '%s' "$AZURE_FOUNDRY_PROJECT_ENDPOINT" | sed -E 's#^https://([^./?]+)\.services\.ai\.azure\.(com|us|cn)/api/projects/[^/?#]+/?$#\1#')"
MODEL_RESOURCE_JSON="$(az resource list \
  --subscription "$MODEL_SUBSCRIPTION_ID" \
  --resource-type Microsoft.CognitiveServices/accounts \
  --name "$MODEL_ACCOUNT" -o json)"
test "$(jq 'length' <<<"$MODEL_RESOURCE_JSON")" = 1
AZURE_FOUNDRY_RESOURCE_ID="$(jq -er '.[0].id' <<<"$MODEL_RESOURCE_JSON")"

az resource list \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --query '[].{name:name,type:type,id:id}' -o table

az identity list \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --query '[].{name:name,clientId:clientId,principalId:principalId,id:id}' -o table
```

Verify the subscription and tenant against your approved deployment record.
Check **every resource in the resource group**, not just resources with the
expected prefix.

Record the two identities' **principal IDs** before deleting them. Principal IDs
are used for RBAC; they are not the client IDs used in Teams manifests.
Also record each Teams manifest app ID and its corresponding catalog entry.
Local deployment outputs under `dist/<prefix>-<environment>/` can help identify
resources, but the live inventory is authoritative.

**Do not delete the resource group if it contains shared resources**, including
the model account. Use the selective procedure in section 6 instead.

## 3. Remove the Teams and Copilot apps

For a full teardown, do this for both the MAF and LangChain apps. For a
publication-only reset, apply these steps only to the selected app and stop
after this section. Keep the other app and all Azure resources.

1. Notify users that the assistants are being retired.
2. In Teams admin center or Microsoft 365 admin center, find the exact custom
   app by name and app ID and block/remove its availability.
   For Copilot, also check **Agents > All agents** for the corresponding custom
   engine agent and remove its assigned audience, preinstallation, and pins.
3. Remove admin-managed installation and pinning assignments, if configured.
4. Remove the app through the relevant management controls:
   - **Microsoft 365 admin center > Settings > Integrated apps**: select the
     exact app in the **Deployed Apps** list, choose **Remove app**, and confirm
     the removal. See Microsoft's
     [custom-app removal procedure](https://learn.microsoft.com/en-us/microsoft-365/admin/manage/teams-apps-work-on-outlook-and-m365?view=o365-worldwide#removal-of-a-custom-app).
     Available actions under **Agents > All agents > Registry** depend on agent
     type and publisher state; **Block** is not deletion.
   - **Teams admin center > Teams apps > Manage apps**: remove the organization
     catalog entry if it is still present.
   With unified app management, availability changes are synchronized between
   the two admin centers. Verify removal in both rather than assuming that
   blocking an app removes its catalog entries.
5. Uninstall personal installations as appropriate. For individually uploaded
   pilot apps, have users remove them from Teams and their Copilot agent list.
6. Verify the agents are no longer available to the former pilot audience in
   both clients. With unified management, use the shared entry rather than
   treating its Teams and Copilot surfaces as different backend deployments.

Blocking availability is not the same as uninstalling existing installations.
Neither action deletes Azure resources or necessarily removes historical
Teams/Copilot chat messages. Follow tenant retention policy for existing messages.

## 4. Remove model-account role assignments

The deployment creates inference role assignments on an **existing** model
account, potentially outside the application resource group. Resource-group
deletion does not clean up assignments at that external scope.

List assignments for each recorded agent principal ID:

```bash
export AGENT_PRINCIPAL_ID="<recorded-agent-principal-id>"

az role assignment list \
  --subscription "$MODEL_SUBSCRIPTION_ID" \
  --scope "$AZURE_FOUNDRY_RESOURCE_ID" \
  --all \
  --query "[?principalId=='$AGENT_PRINCIPAL_ID'].{id:id,scope:scope,role:roleDefinitionName,principalId:principalId}" \
  -o json
```

Repeat for the other identity. Confirm each assignment's principal, scope, and
role. This repository grants **Cognitive Services OpenAI User** at the account
scope. Delete only the exact assignment IDs belonging to this deployment:

```bash
az role assignment delete \
  --subscription "$MODEL_SUBSCRIPTION_ID" \
  --ids "<reviewed-role-assignment-resource-id>"
```

Repeat the listing to verify the assignments are gone. Do not remove other
applications' permissions, inherited permissions, the account, its project, or
its deployed models.

If identities were granted additional permissions manually, review those scopes
as well. Permissions to delete role assignments may require a separate
administrator even if you can delete the application resources.

## 5. Delete a dedicated application resource group

**Only use this procedure if the inventory in section 2 contains exclusively
resources approved for deletion.** This deletes all resources in that group,
not only the resources created by this repository.

```bash
az group delete \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --name "$AZURE_RESOURCE_GROUP"
```

The CLI prompts for confirmation. The command waits for completion; do not add
`--yes` unless your approved automation has an equivalent safety gate.

This removes the Container Apps, Bots and channels, identities, registry,
environment, workspace, and any older storage account in that group. Deletion
locks or policy restrictions can prevent completion; investigate the reported
failure rather than bypassing customer controls.

Verify:

```bash
az group exists \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --name "$AZURE_RESOURCE_GROUP"
```

Expected result: `false`. Separately verify the shared model account still
exists and that the agent-specific model role assignments are gone.

## 6. Alternative: remove only one agent or use a shared resource group

Do **not** run the resource-group deletion command.

For one agent:

1. Remove only its Teams app.
2. Capture that identity's principal ID.
3. Remove its model-account role assignment as described above.
4. From the live inventory, copy the exact Container App and Bot resource IDs.
   Delete those resources individually:

   ```bash
   az resource delete --ids "<reviewed-container-app-resource-id>"
   az resource delete --ids "<reviewed-bot-resource-id>"
   ```

   Deleting the Bot also removes its Teams channel configuration.
5. Review and remove that identity's `AcrPull` assignment on the shared registry
   using the same list/review/delete-by-ID approach as section 4, but with the
   registry ARM ID and application subscription.
6. Confirm nothing else uses the identity, then delete it:

   ```bash
   az resource delete --ids "<reviewed-managed-identity-resource-id>"
   ```

Keep the shared environment, registry, workspace, and the other agent's identity
and resources. Shared-service charges can continue. Review stored images
separately; do not delete images referenced by the remaining agent or retained
releases.

For a full teardown within a shared resource group, repeat the per-agent
procedure, then delete only approved, exclusively owned shared components by
their exact IDs. Delete the Container Apps before their environment.

**Redeployment caveat:** the foundation template defines both identities and
their role assignments. Running the deployment script again can recreate them,
even when using `--agent` to select a single application. Teardown is not a
persistent exclusion setting.

## 7. Older Blob Storage and local artifacts

Current templates do not create Blob Storage. An account from an earlier
deployment remains until explicitly deleted:

- If it is in a dedicated group deleted in section 5, it is deleted with that
  group.
- Otherwise, confirm its exact ID, ownership, stored data, retention requirements,
  and any remaining permissions before approving separate deletion.
- Do not enable public networking or weaken storage policy just to clean it up.

After retaining required records, remove only the relevant local generated
output directory and local configuration through your normal file-management
process. These ignored artifacts can contain deployment identifiers. Do not
delete configuration or outputs belonging to other environments.

## Completion checklist

- [ ] Both retired apps are unavailable and removed from intended installations/pins.
- [ ] Approved Azure resources are gone; no unapproved shared resource was deleted.
- [ ] Retired identities' external role assignments have been removed.
- [ ] The existing model account and model deployments remain available.
- [ ] Old storage and retained data have been reviewed.
- [ ] Required logs and release records are preserved according to policy.
- [ ] Local artifacts and any deployment automation targeting this environment are retired.

See the [README architecture](README.md#architecture-and-data-flow) and
[deployment instructions](README.md#deploy) for background.
