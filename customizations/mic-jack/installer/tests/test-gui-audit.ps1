[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$sourceDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'src'
$launchPath = Join-Path $sourceDir 'Launch.ps1'
$startPath = Join-Path $sourceDir 'Start.ps1'
$launchSource = [IO.File]::ReadAllText($launchPath)
$startSource = [IO.File]::ReadAllText($startPath)
$testDirectory = Join-Path ([IO.Path]::GetTempPath()) ('nanokvm gui audit with spaces ' + [Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($testDirectory)
$script:AuditResults = New-Object 'System.Collections.Generic.List[object]'
function Assert-True([bool]$Value, [string]$Message) { if (!$Value) { throw $Message } }
function Add-Result([string]$Name) { $script:AuditResults.Add([PSCustomObject]@{test=$Name;ok=$true}); Write-Host "PASS $Name" }

# Load the GUI construction/functions only; do not show a dialog, start timer,
# run PreviewOnly, or dispatch any real process. Substitute only library path.
$prefix = $launchSource.Substring(0, $launchSource.IndexOf('if ($PreviewOnly)'))
$prefix = $prefix.Replace(". (Join-Path `$PSScriptRoot 'Start.ps1') -LibraryOnly", ". '$($startPath.Replace("'", "''"))' -LibraryOnly")
. ([scriptblock]::Create($prefix))
$script:KitRoot = $testDirectory
$script:ProcessCalls = New-Object 'System.Collections.Generic.List[object]'
function Assert-KitFiles { }
function Get-SafeLocalPath([string]$Relative) { return Join-Path $script:KitRoot $Relative }
function Start-Process {
    param($FilePath,$ArgumentList,$WorkingDirectory,$WindowStyle,[switch]$PassThru,$Verb)
    $script:ProcessCalls.Add([PSCustomObject]@{FilePath=$FilePath;ArgumentList=$ArgumentList;WorkingDirectory=$WorkingDirectory;WindowStyle=$WindowStyle;Verb=$Verb})
    $process = [PSCustomObject]@{HasExited=$false;ExitCode=0;Waited=$false;Disposed=$false}
    $process | Add-Member ScriptMethod WaitForExit {$this.Waited=$true}
    $process | Add-Member ScriptMethod Dispose {$this.Disposed=$true}
    return $process
}
function Get-Process { throw 'Real process discovery is forbidden in this mock test.' }
function Show-Error([string]$Message) { throw $Message }

$addressBox.Text = '192.168.2.132'
Start-KvmTask 'Prepare'
Assert-True ($script:ProcessCalls.Count -eq 1) 'Expected one mocked worker.'
$call = $script:ProcessCalls[0]
Assert-True ($call.ArgumentList.Contains(' -File "' + (Join-Path $testDirectory 'Start.ps1') + '" ')) 'Worker path with spaces is not quoted.'
Assert-True ($call.ArgumentList.EndsWith('-Action Prepare -PauseOnExit')) 'Action/pause arguments differ.'
Assert-True ($call.WorkingDirectory -ceq $testDirectory) 'Worker working directory differs.'
Assert-True (!$addressBox.Enabled) 'Address should be disabled during worker.'
Assert-True (@($script:ActionButtons | Where-Object {$_.Enabled}).Count -eq 0) 'Action buttons should be disabled.'
Add-Result 'worker-arguments-and-event-scope'

$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseInput($launchSource,[ref]$tokens,[ref]$errors)
Assert-True ($errors.Count -eq 0) 'GUI parse failed.'
$tick = $ast.Find({param($node) $node -is [Management.Automation.Language.InvokeMemberExpressionAst] -and $node.Member.Value -eq 'Add_Tick'},$true)
$tickText = $tick.Arguments[0].ScriptBlock.Extent.Text
$tickBody = [scriptblock]::Create($tickText.Substring(1,$tickText.Length-2))
$process=$script:RunningTask;$process.HasExited=$true;$process.ExitCode=17
& $tickBody
Assert-True ($process.Waited -and $process.Disposed) 'Ended worker must be waited/disposed.'
Assert-True ($null -eq $script:RunningTask) 'Ended worker must be cleared.'
Assert-True ($addressBox.Enabled) 'Address should be re-enabled.'
Assert-True ($statusLabel.Text.Contains('17')) 'GUI must retain failure exit code.'
Assert-True (@($script:ActionButtons | Where-Object {!$_.Enabled}).Count -eq 0) 'Actions should be re-enabled.'
Add-Result 'worker-nonzero-exit-restores-buttons'

$driver=[PSCustomObject]@{HasExited=$true;ExitCode=3010;Waited=$false;Disposed=$false}
$driver | Add-Member ScriptMethod WaitForExit {$this.Waited=$true}
$driver | Add-Member ScriptMethod Dispose {$this.Disposed=$true}
Set-Running $driver 'Driver'
& $tickBody
Assert-True ($statusLabel.Text.Contains('重启')) 'Driver exit 3010 must request manual restart.'
Add-Result 'driver-reboot-code-is-manual'

# A copied worker overrides its action before main, retaining the real try /
# catch / finally exit handling. No SSH/SCP/driver/AXDL is invoked.
$injection = @'
function Invoke-KitAction([string]$Address,[string]$SelectedAction) {
    if ($SelectedAction -eq 'Status') { Write-Host 'mock worker success'; return }
    throw 'mock worker failure'
}
'@
$workerSource=$startSource.Replace('if ($LibraryOnly) { return }', $injection + "`r`nif (`$LibraryOnly) { return }")
$workerPath=Join-Path $testDirectory 'Start worker copy.ps1'
[IO.File]::WriteAllText($workerPath,$workerSource,(New-Object Text.UTF8Encoding($true)))
$powershell=Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
& $powershell -NoProfile -ExecutionPolicy Bypass -File $workerPath -KvmAddress 192.168.2.132 -Action Status
Assert-True ($LASTEXITCODE -eq 0) 'Successful worker did not return 0.'
Add-Result 'worker-success-exit-code'
& $powershell -NoProfile -ExecutionPolicy Bypass -File $workerPath -KvmAddress 192.168.2.132 -Action Prepare
Assert-True ($LASTEXITCODE -eq 1) 'Failed worker did not return 1.'
Add-Result 'worker-failure-exit-code'
"`n" | & $powershell -NoProfile -ExecutionPolicy Bypass -File $workerPath -KvmAddress 192.168.2.132 -Action Prepare -PauseOnExit
Assert-True ($LASTEXITCODE -eq 1) 'Pause/finally masked worker failure.'
Add-Result 'pause-finally-preserves-failure'

$commandSource=[IO.File]::ReadAllText((Join-Path $sourceDir 'Start.cmd'))
Assert-True ($commandSource.Contains('-File "%~dp0Launch.ps1"')) 'Start.cmd must quote its script path.'
Assert-True ($commandSource.Contains('start "" "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"')) 'Start.cmd start title/executable quoting differs.'
Add-Result 'cmd-entrypoint-static-quoting'
Assert-True ([BitConverter]::ToString([IO.File]::ReadAllBytes($launchPath)[0..2]) -ceq 'EF-BB-BF') 'Chinese GUI source requires BOM for Windows PowerShell 5.1.'
Add-Result 'windows-powershell-utf8-bom'
$timer.Dispose();$form.Dispose()
$report=Join-Path $testDirectory 'gui-independent-verification.json'
[IO.File]::WriteAllText($report,($script:AuditResults | ConvertTo-Json -Depth 4),(New-Object Text.UTF8Encoding($false)))
Write-Host "All $($script:AuditResults.Count) GUI / worker mock checks passed."
