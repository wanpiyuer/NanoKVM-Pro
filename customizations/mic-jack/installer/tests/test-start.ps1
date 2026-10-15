[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$source = Join-Path (Split-Path -Parent $PSScriptRoot) 'src/Start.ps1'
. $source -LibraryOnly
$script:TestRoot = Join-Path ([IO.Path]::GetTempPath()) ('nkm-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
[void][IO.Directory]::CreateDirectory($script:TestRoot)
$script:Results = New-Object 'System.Collections.Generic.List[object]'

function Assert-True([bool]$Value, [string]$Message) {
    if (!$Value) { throw $Message }
}
function Assert-Throws([scriptblock]$Operation, [string]$Pattern = '') {
    $caught = $false
    try { & $Operation | Out-Null } catch {
        $caught = $true
        if ($Pattern -and $_.Exception.Message -notmatch $Pattern) {
            throw "Unexpected failure: $($_.Exception.Message)"
        }
    }
    if (!$caught) { throw 'Expected operation to fail, but it succeeded.' }
}
function Test-Case([string]$Name, [scriptblock]$Body) {
    try {
        New-TestKit $Name
        & $Body
        $script:Results.Add([PSCustomObject]@{ test = $Name; ok = $true })
        Write-Host "PASS $Name"
    } catch {
        $script:Results.Add([PSCustomObject]@{ test = $Name; ok = $false; error = $_.Exception.Message })
        Write-Host "FAIL $Name -- $($_.Exception.Message)" -ForegroundColor Red
    }
}
function New-TestKit([string]$Name) {
    $script:KitRoot = Join-Path $script:TestRoot $Name
    [void][IO.Directory]::CreateDirectory((Join-Path $script:KitRoot 'payload'))
    $script:TransportName = 'nanokvm-new-device-tools-20261015.tar.gz'
    $files = @()
    foreach ($name in @(('payload/' + $script:TransportName), 'a.txt', 'b.txt', 'c.txt', 'd.txt')) {
        $path = Join-Path $script:KitRoot $name
        [IO.File]::WriteAllBytes($path, [Text.Encoding]::UTF8.GetBytes('fixture ' + $name))
        $files += [ordered]@{
            path = $name
            bytes = (Get-Item -LiteralPath $path).Length
            sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    Write-JsonAtomic (Join-Path $script:KitRoot 'manifest.json') ([ordered]@{ schema = 'nanokvm-mic-kit-manifest-v1'; files = $files })
    $script:TransportHash = (Get-FileHash -LiteralPath (Join-Path $script:KitRoot ('payload/' + $script:TransportName)) -Algorithm SHA256).Hash.ToLowerInvariant()
    $script:ArchiveBytes = [Text.Encoding]::UTF8.GetBytes("mock per-device archive`n")
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $script:ArchiveHash = ([BitConverter]::ToString($sha.ComputeHash($script:ArchiveBytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    $script:Fingerprint = 'a' * 64
    $script:Receipt = [ordered]@{
        schema = 'nanokvm-original-backup-receipt-v1'; ok = $true
        baseline_id = $script:BaselineId
        archive_sha256 = $script:ArchiveHash; archive_bytes = $script:ArchiveBytes.Length
        device_fingerprint = $script:Fingerprint
    }
    $script:RemoteMode = 'success'
    $script:ScpMode = 'success'
    $script:RemoteCalls = New-Object 'System.Collections.Generic.List[object]'
    $script:ScpCalls = New-Object 'System.Collections.Generic.List[object]'
    $script:LiveFingerprint = $script:Fingerprint
}

# All transport is replaced: no SSH/SCP process is run by this suite.
function ssh { throw 'External SSH is forbidden during offline tests.' }
function scp { throw 'External SCP is forbidden during offline tests.' }
function Read-Host { throw 'Unexpected interactive prompt during offline tests.' }
function Invoke-RemoteKit([string]$Address, [string]$RemoteAction, [string]$Fingerprint = '', [string]$BackupHash = '') {
    $script:RemoteCalls.Add([PSCustomObject]@{ address = $Address; action = $RemoteAction; fingerprint = $Fingerprint; backup_hash = $BackupHash })
    if ($script:RemoteMode -eq 'failure') { throw 'mock remote failure' }
    if ($RemoteAction -eq 'install') {
        if ($Fingerprint -cne $script:LiveFingerprint) { throw 'mock remote device fingerprint mismatch' }
        if ($BackupHash -cne $script:ArchiveHash) { throw 'mock remote archive mismatch' }
        return 'mock install completed'
    }
    if ($RemoteAction -ne 'prepare') { return 'mock status' }
    $line = 'NANOKVM_RECEIPT_JSON=' + ($script:Receipt | ConvertTo-Json -Compress -Depth 16)
    switch ($script:RemoteMode) {
        'missing-receipt' { return 'unrelated output' }
        'duplicate-receipt' { return @($line, $line) }
        default { return @('mock preparation', $line) }
    }
}
function Invoke-CheckedScp([string[]]$Arguments) {
    $script:ScpCalls.Add([PSCustomObject]@{ arguments = $Arguments })
    Assert-True ($Arguments.Count -eq 2) 'SCP did not receive exactly two arguments.'
    if ($script:ScpMode -eq 'failure') { throw 'mock scp failure' }
    $content = $script:ArchiveBytes
    if ($script:ScpMode -eq 'corrupt') { $content = [Text.Encoding]::UTF8.GetBytes('corrupt archive bytes') }
    [IO.File]::WriteAllBytes($Arguments[1], $content)
}

Test-Case 'prepare-download-receipt-state' {
    Invoke-KitAction '192.168.2.132' 'Prepare'
    $state = Get-PreparedState '192.168.2.132'
    Assert-True ($state.archive_sha256 -ceq $script:ArchiveHash) 'State SHA differs.'
    Assert-True ($state.device_fingerprint -ceq $script:Fingerprint) 'State device differs.'
    Assert-True ($script:ScpCalls.Count -eq 1) 'Expected one backup download.'
    Assert-True (Test-Path -LiteralPath (Join-Path $script:KitRoot 'backups/192.168.2.132/receipt.json')) 'Receipt missing.'
}
Test-Case 'prepare-repeat-revalidates-no-redownload' {
    Invoke-Prepare '192.168.2.132'
    Invoke-Prepare '192.168.2.132'
    Assert-True ($script:ScpCalls.Count -eq 1) 'Valid existing backup should not be downloaded again.'
}
Test-Case 'download-failure-no-state' {
    $script:ScpMode = 'failure'
    Assert-Throws { Invoke-Prepare '192.168.2.132' } 'mock scp failure'
    Assert-True (!(Test-Path -LiteralPath (Join-Path $script:KitRoot 'backups/192.168.2.132/state.json'))) 'Failure wrote state.'
}
Test-Case 'download-hash-mismatch-no-state' {
    $script:ScpMode = 'corrupt'
    Assert-Throws { Invoke-Prepare '192.168.2.132' } 'Downloaded backup differs'
    Assert-True (!(Test-Path -LiteralPath (Join-Path $script:KitRoot 'backups/192.168.2.132/state.json'))) 'Corrupt download wrote state.'
}
Test-Case 'remote-failure-no-state' {
    $script:RemoteMode = 'failure'
    Assert-Throws { Invoke-Prepare '192.168.2.132' } 'mock remote failure'
    Assert-True (!(Test-Path -LiteralPath (Join-Path $script:KitRoot 'backups/192.168.2.132/state.json'))) 'Remote failure wrote state.'
}
Test-Case 'missing-receipt-no-state' {
    $script:RemoteMode = 'missing-receipt'
    Assert-Throws { Invoke-Prepare '192.168.2.132' } 'unique successful'
    Assert-True ($script:ScpCalls.Count -eq 0) 'Missing receipt downloaded backup.'
}
Test-Case 'duplicate-receipt-no-state' {
    $script:RemoteMode = 'duplicate-receipt'
    Assert-Throws { Invoke-Prepare '192.168.2.132' } 'unique successful'
    Assert-True ($script:ScpCalls.Count -eq 0) 'Duplicate receipt downloaded backup.'
}
Test-Case 'unsuccessful-receipt-no-state' {
    $script:Receipt.ok = $false
    Assert-Throws { Invoke-Prepare '192.168.2.132' } 'not a successful receipt'
    Assert-True ($script:ScpCalls.Count -eq 0) 'Unsuccessful receipt downloaded backup.'
}
Test-Case 'wrong-baseline-receipt-no-state' {
    $script:Receipt.baseline_id = 'different baseline'
    Assert-Throws { Invoke-Prepare '192.168.2.132' } 'not a successful receipt'
    Assert-True ($script:ScpCalls.Count -eq 0) 'Wrong baseline downloaded backup.'
}
Test-Case 'different-device-same-ip-blocked' {
    Invoke-Prepare '192.168.2.132'
    $script:Receipt.device_fingerprint = 'b' * 64
    Assert-Throws { Invoke-Prepare '192.168.2.132' } 'different device/backup'
    $state = Get-PreparedState '192.168.2.132'
    Assert-True ($state.device_fingerprint -ceq $script:Fingerprint) 'Old state was overwritten.'
}
Test-Case 'different-archive-same-ip-blocked' {
    Invoke-Prepare '192.168.2.132'
    $script:Receipt.archive_sha256 = 'b' * 64
    Assert-Throws { Invoke-Prepare '192.168.2.132' } 'different device/backup'
    $state = Get-PreparedState '192.168.2.132'
    Assert-True ($state.archive_sha256 -ceq $script:ArchiveHash) 'Old archive state was overwritten.'
}
Test-Case 'local-backup-tamper-blocks-install' {
    Invoke-Prepare '192.168.2.132'
    $state = Get-PreparedState '192.168.2.132'
    [IO.File]::AppendAllText((Join-Path $script:KitRoot ('backups/192.168.2.132/' + $state.archive_name)), 'tamper')
    $prior = $script:RemoteCalls.Count
    Assert-Throws { Invoke-KitAction '192.168.2.132' 'InstallAfterFlash' } 'differs from its receipt'
    Assert-True ($script:RemoteCalls.Count -eq $prior) 'Corrupt local backup reached remote install.'
}
Test-Case 'state-receipt-device-mismatch-blocked' {
    Invoke-Prepare '192.168.2.132'
    $receiptPath = Join-Path $script:KitRoot 'backups/192.168.2.132/receipt.json'
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    $receipt.device_fingerprint = 'b' * 64
    Write-JsonAtomic $receiptPath $receipt
    Assert-Throws { Get-PreparedState '192.168.2.132' } 'different devices/backups'
}
Test-Case 'ip-change-single-backup-auto-selects' {
    Invoke-Prepare '192.168.2.132'
    $state = Get-PreparedState '192.168.2.140'
    Assert-True ($state.prepared_address -eq '192.168.2.132') 'Changed IP selected wrong backup.'
    Invoke-KitAction '192.168.2.140' 'InstallAfterFlash'
    $last = $script:RemoteCalls[$script:RemoteCalls.Count - 1]
    Assert-True ($last.address -eq '192.168.2.140' -and $last.fingerprint -ceq $script:Fingerprint) 'Changed IP lost identity binding.'
}
Test-Case 'remote-device-swap-blocked' {
    Invoke-Prepare '192.168.2.132'
    $script:LiveFingerprint = 'b' * 64
    Assert-Throws { Invoke-KitAction '192.168.2.132' 'InstallAfterFlash' } 'fingerprint mismatch'
}
Test-Case 'no-prepare-state-blocks-install-before-remote' {
    Assert-Throws { Invoke-KitAction '192.168.2.132' 'InstallAfterFlash' } 'No completed Prepare'
    Assert-True ($script:RemoteCalls.Count -eq 0) 'Unprepared install made a connection.'
}
Test-Case 'manifest-file-tamper-before-connection' {
    [IO.File]::AppendAllText((Join-Path $script:KitRoot 'a.txt'), 'tamper')
    Assert-Throws { Invoke-KitAction '192.168.2.132' 'Prepare' } 'differs from the release'
    Assert-True ($script:RemoteCalls.Count -eq 0) 'Manifest failure made connection.'
}
Test-Case 'transport-tamper-even-with-updated-manifest' {
    $transport = Join-Path $script:KitRoot ('payload/' + $script:TransportName)
    [IO.File]::AppendAllText($transport, 'tamper')
    $manifestPath = Join-Path $script:KitRoot 'manifest.json'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $entry = @($manifest.files | Where-Object { $_.path -eq ('payload/' + $script:TransportName) })[0]
    $entry.bytes = (Get-Item -LiteralPath $transport).Length
    $entry.sha256 = (Get-FileHash -LiteralPath $transport -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-JsonAtomic $manifestPath $manifest
    Assert-Throws { Invoke-KitAction '192.168.2.132' 'Prepare' } 'Transport package SHA256 mismatch'
    Assert-True ($script:RemoteCalls.Count -eq 0) 'Transport failure made connection.'
}
Test-Case 'manifest-path-traversal-blocked' {
    $manifestPath = Join-Path $script:KitRoot 'manifest.json'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $manifest.files[0].path = '../escape.txt'
    Write-JsonAtomic $manifestPath $manifest
    Assert-Throws { Assert-KitFiles } 'Unsafe local path'
}
Test-Case 'shell-plan-valid-fixed-arguments' {
    $plan = New-RemotePlan ('/tmp/nanokvm-mic-kit.' + ('c' * 32) + '.tar.gz') 'install' $script:Fingerprint $script:ArchiveHash
    Assert-True ($plan.Contains('bash "$mic_stage/device-actions.sh" install ' + $script:Fingerprint + ' ' + $script:ArchiveHash)) 'Plan arguments differ.'
}
Test-Case 'shell-plan-hex-command-injection-blocked' {
    Assert-Throws { New-RemotePlan ('/tmp/nanokvm-mic-kit.' + ('c' * 32) + '.tar.gz') 'install' (($script:Fingerprint) + ';id') $script:ArchiveHash } 'Invalid SHA256'
    Assert-Throws { New-RemotePlan '/tmp/evil.tar.gz;id' 'prepare' } 'upload path'
    Assert-Throws { New-RemotePlan ('/tmp/nanokvm-mic-kit.' + ('c' * 32) + '.tar.gz') 'prepare;id' } 'remote action'
}
Test-Case 'shell-plan-trailing-newline-rejected' {
    $upload = '/tmp/nanokvm-mic-kit.' + ('c' * 32) + '.tar.gz'
    Assert-Throws { New-RemotePlan $upload 'install' ($script:Fingerprint + "`n") $script:ArchiveHash } 'Invalid SHA256'
    Assert-Throws { New-RemotePlan $upload 'install' $script:Fingerprint ($script:ArchiveHash + "`n") } 'Invalid SHA256'
    Assert-Throws { New-RemotePlan ($upload + "`n") 'prepare' } 'upload path'
    Assert-Throws { New-RemotePlan $upload "prepare`n" } 'remote action'
    $script:TransportHash += "`n"
    Assert-Throws { New-RemotePlan $upload 'prepare' } 'Invalid SHA256'
}
Test-Case 'ipv4-shell-injection-rejected' {
    Assert-Throws { Assert-IPv4 '192.168.2.132;id' }
    Assert-Throws { Assert-IPv4 '999.1.2.3' }
    Assert-Throws { Assert-IPv4 "192.168.2.132`n" }
}

$report = Join-Path $script:TestRoot 'results.json'
Write-JsonAtomic $report @($script:Results.ToArray())
$failures = @($script:Results | Where-Object { !$_.ok })
Write-Host ("START TESTS: {0}/{1} passed; report {2}" -f ($script:Results.Count - $failures.Count), $script:Results.Count, $report)
if ($failures.Count) { exit 1 }
