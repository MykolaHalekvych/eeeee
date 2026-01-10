param(
  [Parameter(Mandatory=$false)][ValidateSet("dryrun","apply")][string]$Mode = "dryrun",
  [Parameter(Mandatory=$false)][string]$ConfirmDelete = "NO",
  [Parameter(Mandatory=$false)][string]$Policy = "manifests/retention/retention_policy_v1.json",
  [Parameter(Mandatory=$false)][string]$Repo = (Get-Location).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

py -3.11 -m args.foundry.retention_v1 --repo $Repo --policy $Policy --mode $Mode --confirm-delete $ConfirmDelete
exit $LASTEXITCODE