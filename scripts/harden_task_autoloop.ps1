param(
  [string]$TaskName = "ARGS_AutoLoop_5m_with_rotate"
)

$service = New-Object -ComObject "Schedule.Service"
$service.Connect()
$root = $service.GetFolder("\")

$task = $root.GetTask($TaskName)
$def = $task.Definition
$set = $def.Settings

# 1) Run ASAP after missed start
$set.StartWhenAvailable = $true

# 2) Anti-overlap: stop existing instance
# 0=IgnoreNew, 1=Parallel, 2=Queue, 3=StopExisting
$set.MultipleInstances = 3

# 3) Hard timeout (5m schedule -> 4m max runtime)
$set.ExecutionTimeLimit = "PT4M"
$set.AllowHardTerminate = $true

# 4) Battery-safe (optional)
$set.DisallowStartIfOnBatteries = $false
$set.StopIfGoingOnBatteries = $false

# 5) Restart on failure
$set.RestartInterval = "PT1M"
$set.RestartCount = 3

# Re-register definition (Interactive token: no password needed)
# TASK_CREATE_OR_UPDATE = 6, TASK_LOGON_INTERACTIVE_TOKEN = 3
$root.RegisterTaskDefinition($TaskName, $def, 6, $null, $null, 3, $null) | Out-Null

Write-Output ("{0} hardened OK" -f $TaskName)
