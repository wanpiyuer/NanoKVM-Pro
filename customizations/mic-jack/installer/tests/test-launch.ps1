[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$base = Split-Path -Parent $PSScriptRoot
$launchPath = Join-Path $base 'src/Launch.ps1'
$startPath = Join-Path $base 'src/Start.ps1'
$cmdPath = Join-Path $base 'src/Start.cmd'
$launchText = Get-Content -LiteralPath $launchPath -Raw -Encoding UTF8
$startText = Get-Content -LiteralPath $startPath -Raw -Encoding UTF8
$cmdText = Get-Content -LiteralPath $cmdPath -Raw -Encoding UTF8
$script:Results = New-Object 'System.Collections.Generic.List[object]'

function Assert-True([bool]$Value, [string]$Message) { if (!$Value) { throw $Message } }
function Test-Case([string]$Name, [scriptblock]$Body) {
    try { & $Body; $script:Results.Add([PSCustomObject]@{ test=$Name; ok=$true }); Write-Host "PASS $Name" }
    catch { $script:Results.Add([PSCustomObject]@{ test=$Name; ok=$false; error=$_.Exception.Message }); Write-Host "FAIL $Name -- $($_.Exception.Message)" -ForegroundColor Red }
}
Test-Case 'utf8-bom-ps51-chinese' {
    foreach ($path in @($launchPath, $startPath)) {
        $bytes = [IO.File]::ReadAllBytes($path)
        Assert-True ($bytes.Length -gt 3 -and $bytes[0] -eq 239 -and $bytes[1] -eq 187 -and $bytes[2] -eq 191) ('UTF8 BOM missing: ' + $path)
    }
}
Test-Case 'ps51-ast-parse' {
    foreach ($path in @($launchPath, $startPath)) {
        $tokens = $null; $errors = $null
        [void][Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
        Assert-True (@($errors).Count -eq 0) ('AST parse errors: ' + $path)
    }
}
Test-Case 'cmd-quoted-hidden-sta-launcher' {
    Assert-True ($cmdText.Contains('"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"')) 'Launcher executable path is unquoted.'
    Assert-True ($cmdText.Contains('-File "%~dp0Launch.ps1"')) 'Launcher script path is unquoted.'
    Assert-True ($cmdText.Contains('-STA -WindowStyle Hidden')) 'Launcher does not request hidden STA.'
}
Test-Case 'worker-cli-validates-action' {
    Assert-True ($startText -match "\[ValidateSet\('Menu', 'Prepare', 'InstallAfterFlash', 'Status', 'RollbackWeb'\)\]") 'Worker action enum changed.'
    Assert-True ($startText -match 'finally\s*\{\s*if \(\$PauseOnExit\)') 'Worker does not retain task output until Enter.'
}
Test-Case 'no-automatic-flash-start-or-erasure' {
    Assert-True ($launchText -notmatch '(?i)SendKeys|SendMessage|BM_CLICK|UIAutomation|Win32Api|/autostart|--flash|--erase|EraseAll') 'Launcher contains an automatic flash/erase mechanism.'
    Assert-True ($launchText.Contains("Get-SafeLocalPath 'candidate/REVIEW-candidate-kernel-A.axp'")) 'Candidate location is not fixed.'
    Assert-True ($launchText.Contains("Get-Process -Name AXDL")) 'AXDL running guard is missing.'
}
Test-Case 'no-password-storage-code' {
    $combined = $launchText + "`n" + $startText
    Assert-True ($combined -notmatch '(?i)\$(password|passwd|sshpass)\b|ConvertFrom-SecureString|Export-Clixml|CredentialManager|cmdkey|sshpass') 'Credential persistence mechanism found.'
    Assert-True ($startText.Contains('Only stdout is saved; no passwords are stored.')) 'SSH output capture boundary changed.'
}
Test-Case 'official-driver-provenance' {
    $evidence = Join-Path $base 'tools-downloads.json'
    $downloads = Get-Content -LiteralPath $evidence -Raw -Encoding UTF8 | ConvertFrom-Json
    $driver = @($downloads | Where-Object { $_.file -eq 'Driver_V1.20.46.1.7z' })
    Assert-True ($driver.Count -eq 1 -and $driver[0].url -eq 'https://dl.sipeed.com/fileList/MaixCAM/MaixCAM2/Software/Tools/Driver_V1.20.46.1.7z') 'Official Sipeed download provenance differs.'
    Assert-True ($driver[0].sha256 -eq '5160ff28302ae7fd78bc37f8644cf8d2afc2c6f8f715f170075221826d4612a5') 'Driver archive SHA differs.'
    if ($env:NANOKVM_VENDOR_ARCHIVE_DIR) {
        $archive = Join-Path $env:NANOKVM_VENDOR_ARCHIVE_DIR 'Driver_V1.20.46.1.7z'
        Assert-True ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ceq $driver[0].sha256) 'Explicit vendor archive differs.'
    }
    Assert-True ($launchText.Contains("Get-SafeLocalPath 'tools/Driver_V1.20.46.1/DriversForWin10/Drivers/axeravcom.inf'")) 'Driver INF path differs.'
    Assert-True ($launchText.Contains("Join-Path `$env:SystemRoot 'System32/pnputil.exe'")) 'pnputil does not use the system executable.'
    Assert-True ($launchText.Contains("'/add-driver `"' + `$inf + '`" /install'")) 'Driver install arguments changed.'
}

# Construct the actual controls and event closures without Show/ShowDialog,
# starting the timer, PreviewOnly rendering, or any external process.
. $startPath -LibraryOnly
$cut = $launchText.IndexOf('if ($PreviewOnly) {', [StringComparison]::Ordinal)
if ($cut -lt 0) { throw 'GUI initialization boundary not found.' }
$prefix = $launchText.Substring(0, $cut).Replace(". (Join-Path `$PSScriptRoot 'Start.ps1') -LibraryOnly", '')
. ([scriptblock]::Create($prefix))
$script:KitRoot = Join-Path ([IO.Path]::GetTempPath()) 'NanoKVM GUI fixture with spaces'
$script:ProcessCalls = New-Object 'System.Collections.Generic.List[object]'
$script:Errors = New-Object 'System.Collections.Generic.List[string]'
$script:KitValid = $true
$script:CheckCount = 0
function Assert-KitFiles { $script:CheckCount++; if (!$script:KitValid) { throw 'mock kit hash failure' } }
function Show-Error([string]$Message) { $script:Errors.Add($Message) }
function Start-Process([string]$FilePath, [string]$ArgumentList, [string]$WorkingDirectory, [string]$WindowStyle, [switch]$PassThru, [string]$Verb) {
    $script:ProcessCalls.Add([PSCustomObject]@{ path=$FilePath; arguments=$ArgumentList; directory=$WorkingDirectory; style=$WindowStyle; verb=$Verb })
    $process = [PSCustomObject]@{ HasExited=$false; ExitCode=0; WasDisposed=$false; WasWaited=$false }
    $process | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { $this.WasWaited=$true }
    $process | Add-Member -MemberType ScriptMethod -Name Dispose -Value { $this.WasDisposed=$true }
    return $process
}
function Reset-GuiFixture {
    $script:RunningTask = $null
    $script:RunningKind = ''
    $script:ProcessCalls.Clear()
    $script:Errors.Clear()
    $script:KitValid = $true
    foreach ($button in $script:ActionButtons) { $button.Enabled = $true }
    $addressBox.Enabled = $true
    $addressBox.Text = '192.168.2.132'
}
function Invoke-ButtonHandler([string]$Text) {
    $button = @($form.Controls | Where-Object { $_ -is [Windows.Forms.Button] -and $_.Text -eq $Text })
    Assert-True ($button.Count -eq 1) ('Button not found: ' + $Text)
    $method = $button[0].GetType().GetMethod('OnClick', [Reflection.BindingFlags]'Instance,NonPublic')
    [void]$method.Invoke($button[0], @([EventArgs]::Empty))
}
try {
    Test-Case 'worker-arguments-quoted-and-validated' {
        Reset-GuiFixture
        Start-KvmTask 'Prepare'
        Assert-True ($script:ProcessCalls.Count -eq 1) 'Expected exactly one worker.'
        $call = $script:ProcessCalls[0]
        Assert-True ($call.path -eq (Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe')) 'Worker executable differs.'
        Assert-True ($call.arguments.Contains('-File "' + (Join-Path $script:KitRoot 'Start.ps1') + '"')) 'Worker file path with spaces is unquoted.'
        Assert-True ($call.arguments.EndsWith('-KvmAddress 192.168.2.132 -Action Prepare -PauseOnExit')) 'Worker address/action arguments differ.'
        Assert-True ($call.directory -eq $script:KitRoot -and $call.style -eq 'Normal') 'Interactive SSH password window is not normal.'
    }
    Test-Case 'one-task-disables-action-buttons' {
        Reset-GuiFixture
        Start-KvmTask 'Status'
        Assert-True (@($script:ActionButtons | Where-Object { $_.Enabled }).Count -eq 0) 'An action button stayed enabled.'
        Assert-True (!$addressBox.Enabled) 'Address remained editable during the worker.'
    }
    Test-Case 'active-worker-cannot-start-second-worker' {
        Reset-GuiFixture
        Start-KvmTask 'Prepare'
        try { Start-KvmTask 'Status' } catch { }
        Assert-True ($script:ProcessCalls.Count -eq 1) 'Second worker started while the first is active.'
    }
    Test-Case 'invalid-action-never-starts-worker' {
        Reset-GuiFixture
        try { Start-KvmTask 'Prepare -Command evil' } catch { }
        Assert-True ($script:ProcessCalls.Count -eq 0) 'Non-whitelisted action reached Start-Process.'
    }
    Test-Case 'invalid-address-and-kit-never-start-worker' {
        Reset-GuiFixture
        $addressBox.Text = '192.168.2.132;evil'
        Start-KvmTask 'Prepare'
        Assert-True ($script:ProcessCalls.Count -eq 0 -and $script:Errors.Count -eq 1) 'Bad address started a worker.'
        Reset-GuiFixture
        $script:KitValid = $false
        Start-KvmTask 'Prepare'
        Assert-True ($script:ProcessCalls.Count -eq 0 -and $script:Errors.Count -eq 1) 'Invalid kit started a worker.'
    }
    Test-Case 'timer-releases-completed-worker' {
        Reset-GuiFixture
        Start-KvmTask 'Status'
        $finished = $script:RunningTask
        $finished.HasExited = $true
        $method = $timer.GetType().GetMethod('OnTick', [Reflection.BindingFlags]'Instance,NonPublic')
        [void]$method.Invoke($timer, @([EventArgs]::Empty))
        Assert-True ($null -eq $script:RunningTask -and $finished.WasWaited -and $finished.WasDisposed) 'Completed worker was not reaped.'
        Assert-True (@($script:ActionButtons | Where-Object { !$_.Enabled }).Count -eq 0 -and $addressBox.Enabled) 'Controls stayed disabled after completion.'
    }
    Test-Case 'driver-event-is-system-pnputil-official-inf-only' {
        Reset-GuiFixture
        Invoke-ButtonHandler '安装 AXERA 驱动'
        Assert-True ($script:ProcessCalls.Count -eq 1) 'Expected one mocked driver process.'
        $call = $script:ProcessCalls[0]
        Assert-True ($call.path -eq (Join-Path $env:SystemRoot 'System32/pnputil.exe') -and $call.verb -eq 'RunAs') 'Driver event executable/elevation differs.'
        $inf = Join-Path $script:KitRoot 'tools/Driver_V1.20.46.1/DriversForWin10/Drivers/axeravcom.inf'
        Assert-True ($call.arguments -eq ('/add-driver "' + $inf + '" /install')) 'Driver arguments differ.'
        Assert-True ($script:RunningKind -eq 'Driver') 'Driver did not hold the one-task lock.'
    }
} finally {
    $timer.Dispose()
    $form.Dispose()
}
$failed = @($script:Results | Where-Object { !$_.ok })
Write-Host ("LAUNCH TESTS: {0}/{1} passed" -f ($script:Results.Count - $failed.Count), $script:Results.Count)
if ($failed.Count) { exit 1 }
