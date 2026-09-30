# Databricks Disaster Recovery — Terraform Exporter + Azure DevOps

Two Azure DevOps pipelines that back up and restore **Databricks workspace and Unity Catalog
configuration** as Terraform, using the **Databricks Terraform-provider exporter**. Backups are
versioned in Git and mirrored to Azure Data Lake Storage (ADLS); restores run `terraform plan`/`apply`
behind an approval gate. Pipelines run on `ubuntu-latest` Microsoft-hosted agents.

## Prerequisites

An Azure subscription and a service principal; two Databricks workspaces (a source and a restore
target); an ADLS Gen2 storage account (**hierarchical namespace enabled**) with a backup container; a
Key Vault for the SP secret; and an Azure DevOps project with a repo.

## Pinned versions

| Tool | Version | Where |
|------|---------|-------|
| Terraform | **1.5.5** | install-tooling template param |
| Databricks TF-provider exporter | **v1.134.0** | install-tooling template param |
| Databricks CLI | **0.281.0** | install-tooling template param |
| Agent | `ubuntu-latest` | Microsoft-hosted |

The install template downloads pinned release archives and verifies their SHA256 checksums before
installation.

## Authentication model

Everything runs as a single service principal.

**Databricks** (exporter, CLI, TF provider) via env vars:

```
DATABRICKS_HOST    = https://<host>/
ARM_TENANT_ID      = <tenant-id>
ARM_CLIENT_ID      = <sp-client-id>
ARM_CLIENT_SECRET  = <sp-secret>
```

**Azure / ADLS / tfstate** via an `AzureCLI@2` task bound to an ARM service connection; the azurerm
backend also needs `ARM_SUBSCRIPTION_ID` = the **subscription GUID**.

### Service connection

Create an **Azure Resource Manager** connection (`sc-mjs-drdemo`):

1. Project Settings → Service connections → New → **Azure Resource Manager**.
2. **Service principal (manual)**: subscription ID/name, tenant ID, SP client ID + secret.
3. Verify, name, save, grant pipeline access.

Grant the SP **Storage Blob Data Contributor** on the storage account for blob data operations and
tfstate:

```bash
az role assignment create --assignee <sp-client-id> \
  --role "Storage Blob Data Contributor" \
  --scope $(az storage account show -n <acct> -g <rg> --query id -o tsv)
```

The ADLS mirror uses `az storage blob sync`, which obtains a storage account key at runtime because
that command does not support `--auth-mode`. The SP therefore also needs
`Microsoft.Storage/storageAccounts/listKeys/action`, commonly through **Contributor** at the storage
account or resource-group scope, or through a narrower custom/key-operator role. The key is used
transiently and is not logged.

### Service principal in the workspaces

- Add the SP as **workspace admin** on both source and target.
- For UC restores, add the SP to the **Databricks account** and grant the metastore/catalog
  privileges it will touch.

## Configuration surfaces

**Config variable group** (`vg-mjs-drdemo`): `storage-account-name`, `storage-container-name`,
`azure-resource-group`, `azure-subscription-id` (subscription GUID), `tenant-id`.

**KV-linked variable group** (`vg-kv-mjs-drdemo`): `client-id`, `client-secret`.

**Workspace registry** (`azdo_pipelines/templates/vars-workspaces.yml`):

```yaml
variables:
  - name: databricks-host-<env>
    value: 'https://<host>/'
  - name: restore-class-<env>
    value: allowed   # allowed | approval | blocked
```

- Host is looked up at **runtime** from the workspace name (naming convention):
  `host: $(databricks-host-${{ parameters.<env> }})`.
- Restore class is read at **compile time** to select the approval environment — hence it lives in a
  repo variable template, not a variable group (variable groups are runtime-only).
- Adding a workspace = a host line, a restore-class line, and the `<env>` in the `values:` dropdowns.

## Backup pipeline (`azdo_pipelines/backup_export.yml`)

### Parameters

| Param | Default | Values |
|-------|---------|--------|
| `source_env` | `adb-mjsdrdemo` | registered envs |
| `objects` | `workspace` | `workspace` · `unity_catalog` · `unity_catalog_full` · `workspace_and_uc` · `all` |
| `isIncremental` | `false` | exporter `-incremental` |
| `services_override` | `preset` | `preset`, or exact exporter names/aliases/exclusions |
| `catalogs` | `all` | `all`, or comma list → `-matchRegex` |
| `runValidation` | `false` | placeholder stage; currently fails deliberately if enabled |

### Flow

`checkout` → install tooling → validate connectivity → optionally download the prior ADLS export →
run exporter → write `backend.tf` → commit to `terraform_export/<env>/<objects>/` → mirror that
scoped path to ADLS. A missing prior export is acceptable on backup. The mirror deletes stale files
from that destination path. The disabled `ValidateExport` stage is an extension point.

### Object-scope resolution

| `objects` | services | extra |
|-----------|----------|-------|
| `workspace` | `access,compute,directories,dlt,jobs,notebooks,policies,secrets,sql-endpoints,storage,wsconf,wsfiles` | `-exportDeletedUsersAssets` |
| `unity_catalog` | `uc-catalogs,uc-schemas,uc-tables,uc-grants` | curated core UC structure |
| `unity_catalog_full` | `uc` (exporter alias) | the full UC service family supported by exporter v1.134.0, including credentials, external locations, metastores, volumes, tags, sharing, and vector search |
| `workspace_and_uc` | workspace + core UC | `-exportDeletedUsersAssets` |
| `all` | `all` (incl. account/global) | `-exportDeletedUsersAssets` |

`services_override != preset` replaces the service list; `catalogs != all` appends
`-matchRegex="^(cat1|cat2)($|\.)"`; `isIncremental` appends `-incremental`. Always run with
`-skip-interactive -native-import -noformat`.

The `workspace` preset is a curated baseline, not every exporter-supported workspace service.
Use `services_override` for additional types such as dashboards, or deliberately review `all`.
Likewise, use `unity_catalog` for routine catalog structure backups; use `unity_catalog_full` only
when the wider UC configuration is intentional and the resulting Terraform will be reviewed.

## Restore pipeline (`azdo_pipelines/restore_plan_apply.yml`)

### Parameters

| Param | Default | Notes |
|-------|---------|-------|
| `restore_to_env` | `adb-mjsdrdemo-restore` | target workspace |
| `restore_from_env` | `adb-mjsdrdemo` | which export |
| `restore_from_location` | `adls` | `adls` \| `version_control` |
| `objects` | `workspace` | must match the backup |
| `uc_restore_prefix` | `drrestore_` | `none`/blank forces restricted approval |

### Compile-time policy resolution

| Condition | policy | environment |
|-----------|--------|-------------|
| class `blocked` | `blocked` | (guard fails first) |
| class `approval`, `objects=all`, **or** UC objects with no prefix | `approval` | restricted env |
| otherwise | `allowed` | standard env |

### Flow

**Plan:** guard target (blocked → fail) → when selected, require the ADLS export and commit it →
conditionally strip `import.tf` (dropped for a cross-workspace restore; kept when
`restore_to_env == restore_from_env`) → prefix restored catalog names → `terraform init` (azurerm)
and save `tfplan`, readable `tfplan.txt`, and `.terraform.lock.hcl` → publish the artifact. An ADLS
authentication, network, or missing-path failure stops the run instead of falling back to Git.
**Apply:** `deployment` job bound to the selected environment (**approval check pauses here**) →
`terraform apply -input=false tfplan` applies the exact approved plan.

### tfstate

azurerm backend, key `tfstate/<restore_to_env>/<objects>/terraform.tfstate` in the backup container.

## The approval / restore-class model

Every workspace has a class in the registry: `allowed` → standard gate; `approval` → restricted
gate; `blocked` → hard fail. Class your export source `approval`/`blocked`. Cross-cutting rules: a UC
restore without a prefix and every `objects=all` restore use the restricted gate. Every permitted
restore still passes through an approval environment. Because an ADO `environment:` must be static,
the class is resolved at compile time from the repo registry — a runtime variable group can't drive
it.

## Reusable step templates

`steps-install-tooling` · `steps-validate-connectivity` · `steps-adls-download` ·
`steps-adls-upload` · `steps-git-commit-push` · `steps-tf-export` · `steps-uc-prefix` ·
`steps-guard-restore-target` · `steps-tf-plan` · `steps-tf-apply`. Both pipelines compose these, so a
phase change is a one-file edit. There is also a `validate/` set that smoke-tests each moving part
against a dummy path.

## Gotchas

1. **Handle `import.tf` before plan** — native-import blocks reference the source workspace's IDs, so
   a cross-workspace restore drops them and creates resources fresh; a same-workspace restore keeps
   them to adopt existing resources.
2. **UC name collisions** — the helper prefixes `databricks_catalog.name`; child resources retain
   Terraform references to that catalog.
3. **`all` scope is reference-only** — includes account/global objects (users, groups, metastores,
   storage credentials, external locations).
4. **`ARM_SUBSCRIPTION_ID` = subscription GUID**
5. **Enable hierarchical namespace** on the storage account.
6. **Approval env is compile-time** → keep the restore class in the repo, not a variable group.
7. **Sentinel defaults** (`preset`, `all`, `none`) keep the run form from blocking on empty required
   string params.
8. **Declarative Automation Bundles** — the exporter skips bundle-managed resources because they
   are already code. Point the bundle at the recovery workspace and redeploy it.
9. **Protected workspace directories** — Apply treats only errors matching
   `cannot delete directory: Folder ... is protected` as non-fatal. Non-empty-directory errors and
   every other Terraform error still fail. The pipeline does not recursively delete directories;
   review the apply log and resulting state.

## Roadmap

Validate export *and* import with Lakeflow jobs, notebooks, and the Databricks SDK — assert restored
objects and behavior match the source (the opt-in `ValidateExport` stage is the seam).
