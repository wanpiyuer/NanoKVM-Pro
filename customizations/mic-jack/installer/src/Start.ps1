[CmdletBinding()]
param(
    [string]$KvmAddress = '',
    [ValidateSet('Menu', 'Prepare', 'InstallAfterFlash', 'Status', 'RollbackWeb')]
    [string]$Action = 'Menu',
    [switch]$LibraryOnly,
    [switch]$PauseOnExit
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# Pin the built-in Windows PowerShell module. A parent development tool may
# otherwise pass a PowerShell 7 PSModulePath to this PowerShell 5.1 process.
if ($PSVersionTable.PSEdition -eq 'Desktop') {
    Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Utility/Microsoft.PowerShell.Utility.psd1') -Force -ErrorAction Stop
}
$script:KitRoot = $PSScriptRoot
$script:TransportHash = '__TRANSPORT_SHA256__'
$script:TransportName = 'nanokvm-new-device-tools-20261015.tar.gz'
$script:BaselineId = 'nanokvm-pro-stock-4.19.125-app-1.2.15-20261015'

function Assert-HexDigest([string]$Value) {
    if ($Value -cnotmatch '\A[0-9a-f]{64}\z') { throw 'Invalid SHA256 value.' }
}

function Assert-IPv4([string]$Address) {
    if ($Address -notmatch '\A(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}\z') {
        throw 'Enter the KVM IPv4 address, for example 192.168.2.132.'
    }
    $parsed = [System.Net.IPAddress]::Parse($Address)
    if ($parsed.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { throw 'IPv4 is required.' }
}

function Get-SafeLocalPath([string]$Relative) {
    if ([System.IO.Path]::IsPathRooted($Relative) -or $Relative.Contains(':')) { throw 'Unsafe local path.' }
    $parts = @($Relative -split '[/\\]')
    if ($parts.Count -eq 0 -or @($parts | Where-Object { $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' }).Count) {
        throw 'Unsafe local path component.'
    }
    $cursor = $script:KitRoot
    foreach ($part in $parts) {
        $cursor = Join-Path $cursor $part
        if (Test-Path -LiteralPath $cursor) {
            if (((Get-Item -LiteralPath $cursor -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Links/junctions are not supported inside the kit: $Relative"
            }
        }
    }
    return $cursor
}

function Assert-KitFiles {
    $manifestPath = Get-SafeLocalPath 'manifest.json'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema -ne 'nanokvm-mic-kit-manifest-v1' -or @($manifest.files).Count -lt 5) {
        throw 'Invalid kit manifest. Extract the complete ZIP again.'
    }
    foreach ($entry in $manifest.files) {
        $file = Get-SafeLocalPath ([string]$entry.path)
        Assert-HexDigest ([string]$entry.sha256)
        if (!(Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing kit file: $($entry.path)" }
        if ((Get-Item -LiteralPath $file).Length -ne [long]$entry.bytes -or
            (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -cne $entry.sha256) {
            throw "Kit file differs from the release: $($entry.path). Extract the complete ZIP again."
        }
    }
    $transport = Get-SafeLocalPath ('payload/' + $script:TransportName)
    Assert-HexDigest $script:TransportHash
    if ((Get-FileHash -LiteralPath $transport -Algorithm SHA256).Hash.ToLowerInvariant() -cne $script:TransportHash) {
        throw 'Transport package SHA256 mismatch. No connection was made.'
    }
}

function Invoke-CheckedScp([string[]]$Arguments) {
    & scp @Arguments
    if ($LASTEXITCODE -ne 0) { throw "SCP stopped (exit $LASTEXITCODE). Nothing is marked as prepared/installed." }
}

function New-RemotePlan([string]$RemoteArchive, [string]$RemoteAction, [string]$Fingerprint = '', [string]$BackupHash = '') {
    if ($RemoteArchive -cnotmatch '\A/tmp/nanokvm-mic-kit\.[0-9a-f]{32}\.tar\.gz\z') { throw 'Invalid upload path.' }
    if ($RemoteAction -cnotmatch '\A(prepare|install|status|rollback-web)\z') { throw 'Invalid remote action.' }
    if ($RemoteAction -eq 'install' -or $RemoteAction -eq 'rollback-web') {
        Assert-HexDigest $Fingerprint
        Assert-HexDigest $BackupHash
        $arguments = "$RemoteAction $Fingerprint $BackupHash"
    } else { $arguments = $RemoteAction }
    Assert-HexDigest $script:TransportHash
    $plan = @'
set -Eeuo pipefail
umask 077
test "$(id -u)" = 0
mic_archive=__REMOTE_ARCHIVE__
test -f "$mic_archive" && test ! -L "$mic_archive"
test "$(sha256sum "$mic_archive" | awk '{print $1}')" = '__TRANSPORT_SHA__'
mic_stage=$(mktemp -d /root/nanokvm-new-device-stage.XXXXXX)
tar -xzf "$mic_archive" -C "$mic_stage"
test -f "$mic_stage/device-actions.sh" && test ! -L "$mic_stage/device-actions.sh"
printf 'Persistent kit setup: %s\n' "$mic_stage"
bash "$mic_stage/device-actions.sh" __ARGUMENTS__
'@
    return $plan.Replace('__REMOTE_ARCHIVE__', $RemoteArchive).Replace('__TRANSPORT_SHA__', $script:TransportHash).Replace('__ARGUMENTS__', $arguments)
}

function Invoke-RemoteKit([string]$Address, [string]$RemoteAction, [string]$Fingerprint = '', [string]$BackupHash = '') {
    Assert-IPv4 $Address
    $remoteArchive = '/tmp/nanokvm-mic-kit.' + [Guid]::NewGuid().ToString('N') + '.tar.gz'
    $transport = Get-SafeLocalPath ('payload/' + $script:TransportName)
    Write-Host "Uploading checked installer to root@$Address (enter the KVM root password when asked)."
    Invoke-CheckedScp @($transport, "root@${Address}:$remoteArchive")
    $plan = New-RemotePlan $remoteArchive $RemoteAction $Fingerprint $BackupHash
    $logDirectory = Get-SafeLocalPath 'logs'
    [void][System.IO.Directory]::CreateDirectory($logDirectory)
    $logPath = Join-Path $logDirectory ((Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + $Address + '-' + $RemoteAction + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 6) + '.log')
    $lines = @()
    try {
        # SSH prompts stay interactive. Only stdout is saved; no passwords are stored.
        # tr handles Windows PowerShell 5.1 CRLF text pipelines.
        $lines = @($plan | & ssh -T -o ServerAliveInterval=15 -o ServerAliveCountMax=3 "root@$Address" "bash -o pipefail -c 'tr -d \\r | bash -s'" | ForEach-Object { Write-Host $_; [string]$_ })
        $remoteExit = $LASTEXITCODE
        if ($remoteExit -ne 0) { throw "Remote $RemoteAction stopped (exit $remoteExit). Keep the log; do not retry activation blindly." }
    } finally {
        [System.IO.File]::WriteAllLines($logPath, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
        Write-Host "Log: $logPath"
    }
    return $lines
}

function Assert-Receipt($Receipt) {
    if ($Receipt.schema -ne 'nanokvm-original-backup-receipt-v1' -or $Receipt.ok -ne $true -or $Receipt.baseline_id -ne $script:BaselineId) {
        throw 'The backup receipt is not a successful receipt for this kit.'
    }
    Assert-HexDigest ([string]$Receipt.archive_sha256)
    Assert-HexDigest ([string]$Receipt.device_fingerprint)
    if ([long]$Receipt.archive_bytes -le 0 -or [long]$Receipt.archive_bytes -gt 167772160) { throw 'Invalid backup size.' }
}

function Read-PreparedState([string]$StatePath) {
    $rootPrefix = [System.IO.Path]::GetFullPath($script:KitRoot).TrimEnd([char[]]'\/') + [System.IO.Path]::DirectorySeparatorChar
    $fullStatePath = [System.IO.Path]::GetFullPath($StatePath)
    if (!$fullStatePath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'State must be inside this extracted kit.' }
    $StatePath = Get-SafeLocalPath $fullStatePath.Substring($rootPrefix.Length)
    $state = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($state.schema -ne 'nanokvm-mic-kit-local-state-v1' -or $state.baseline_id -ne $script:BaselineId) { throw 'Invalid local Prepare state.' }
    Assert-IPv4 ([string]$state.prepared_address)
    Assert-HexDigest ([string]$state.device_fingerprint)
    Assert-HexDigest ([string]$state.archive_sha256)
    $expectedName = 'originals-' + $state.archive_sha256.Substring(0, 12) + '.tar.gz'
    if ($state.archive_name -cne $expectedName) { throw 'Unexpected backup file name in state.' }
    $directory = Split-Path -Parent $StatePath
    $relativeDirectory = $directory.Substring($rootPrefix.Length)
    $receiptPath = Get-SafeLocalPath ($relativeDirectory + '/receipt.json')
    $receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-Receipt $receipt
    if ($receipt.device_fingerprint -cne $state.device_fingerprint -or $receipt.archive_sha256 -cne $state.archive_sha256) { throw 'State and receipt refer to different devices/backups.' }
    $backup = Get-SafeLocalPath ($relativeDirectory + '/' + $expectedName)
    if (!(Test-Path -LiteralPath $backup -PathType Leaf) -or
        (Get-Item -LiteralPath $backup).Length -ne [long]$receipt.archive_bytes -or
        (Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash.ToLowerInvariant() -cne $receipt.archive_sha256) {
        throw 'The local per-device backup is missing or differs from its receipt. Installation is blocked.'
    }
    return $state
}

function Get-PreparedState([string]$Address) {
    $current = Get-SafeLocalPath ('backups/' + $Address + '/state.json')
    if (Test-Path -LiteralPath $current -PathType Leaf) { return Read-PreparedState $current }
    $backups = Get-SafeLocalPath 'backups'
    $states = @()
    if (Test-Path -LiteralPath $backups -PathType Container) {
        $states = @(Get-ChildItem -LiteralPath $backups -Directory | Where-Object { $_.Name -match '^\d+\.\d+\.\d+\.\d+$' } | ForEach-Object {
            $relative = 'backups/' + $_.Name + '/state.json'
            $candidate = Get-SafeLocalPath $relative
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { [PSCustomObject]@{ Path = $candidate; Address = $_.Name } }
        })
    }
    if ($states.Count -eq 0) { throw 'No completed Prepare backup is present. Use menu 1 before flashing a new device.' }
    $selected = 0
    if ($states.Count -gt 1) {
        Write-Host 'The address changed. Select the Prepare backup for this KVM:'
        for ($index = 0; $index -lt $states.Count; $index++) { Write-Host ("{0}. Prepared at {1}" -f ($index + 1), $states[$index].Address) }
        $choice = Read-Host 'Backup number'
        $number = 0
        if (![int]::TryParse($choice, [ref]$number) -or $number -lt 1 -or $number -gt $states.Count) { throw 'Invalid backup selection.' }
        $selected = $number - 1
    }
    Write-Host "Using the backup prepared at $($states[$selected].Address). The KVM fingerprint must match."
    return Read-PreparedState $states[$selected].Path
}

function Write-JsonAtomic([string]$Path, $Value) {
    $temporary = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.partial'
    [System.IO.File]::WriteAllText($temporary, (($Value | ConvertTo-Json -Depth 16) + "`n"), (New-Object System.Text.UTF8Encoding($false)))
    if (Test-Path -LiteralPath $Path) { [System.IO.File]::Replace($temporary, $Path, [System.Management.Automation.Language.NullString]::Value) }
    else { [System.IO.File]::Move($temporary, $Path) }
}

function Invoke-Prepare([string]$Address) {
    $lines = @(Invoke-RemoteKit $Address 'prepare')
    $receipts = @($lines | Where-Object { $_.StartsWith('NANOKVM_RECEIPT_JSON=') })
    if ($receipts.Count -ne 1) { throw 'Missing unique successful backup receipt. Do not flash.' }
    $receipt = $receipts[0].Substring('NANOKVM_RECEIPT_JSON='.Length) | ConvertFrom-Json
    Assert-Receipt $receipt
    $directory = Get-SafeLocalPath ('backups/' + $Address)
    [void][System.IO.Directory]::CreateDirectory($directory)
    $statePath = Get-SafeLocalPath ('backups/' + $Address + '/state.json')
    if (Test-Path -LiteralPath $statePath) {
        $prior = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($prior.device_fingerprint -cne $receipt.device_fingerprint -or $prior.archive_sha256 -cne $receipt.archive_sha256) {
            throw 'This local address folder belongs to a different device/backup. Use a fresh extracted copy of the kit.'
        }
    }
    $name = 'originals-' + $receipt.archive_sha256.Substring(0, 12) + '.tar.gz'
    $target = Get-SafeLocalPath ('backups/' + $Address + '/' + $name)
    if (!(Test-Path -LiteralPath $target)) {
        $partial = $target + '.' + [Guid]::NewGuid().ToString('N') + '.partial'
        Invoke-CheckedScp @("root@${Address}:/root/nanokvm-mic-originals.tar.gz", $partial)
        if ((Get-Item -LiteralPath $partial).Length -ne [long]$receipt.archive_bytes -or
            (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant() -cne $receipt.archive_sha256) {
            throw 'Downloaded backup differs from the KVM receipt. Do not flash; keep the partial file for diagnosis.'
        }
        [System.IO.File]::Move($partial, $target)
    }
    if ((Get-Item -LiteralPath $target).Length -ne [long]$receipt.archive_bytes -or
        (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -cne $receipt.archive_sha256) {
        throw 'Existing local backup differs from the receipt. Do not flash.'
    }
    Write-JsonAtomic (Get-SafeLocalPath ('backups/' + $Address + '/receipt.json')) $receipt
    $state = [ordered]@{
        schema = 'nanokvm-mic-kit-local-state-v1'; baseline_id = $script:BaselineId
        prepared_address = $Address; device_fingerprint = $receipt.device_fingerprint
        archive_sha256 = $receipt.archive_sha256; archive_name = $name
        prepared_utc = [DateTime]::UtcNow.ToString('o')
    }
    Write-JsonAtomic $statePath $state
    Write-Host "PREPARE COMPLETE. Verified per-device backup: $target" -ForegroundColor Green
    Write-Host 'Next: follow AXDL-操作.txt, flash only the candidate KERNEL, stop AXDL and cold boot. Then return to the assistant and click step 3.'
}

function Invoke-KitAction([string]$Address, [string]$SelectedAction) {
    Assert-IPv4 $Address
    Assert-KitFiles
    [void](Get-Command ssh -ErrorAction Stop)
    [void](Get-Command scp -ErrorAction Stop)
    switch ($SelectedAction) {
        'Prepare' { Invoke-Prepare $Address }
        'Status' { [void](Invoke-RemoteKit $Address 'status') }
        'InstallAfterFlash' {
            $state = Get-PreparedState $Address
            Write-Host 'Close ALL KVM browser tabs. Runtime activation briefly reconnects USB; keep LAN/Wi-Fi connected.'
            [void](Invoke-RemoteKit $Address 'install' $state.device_fingerprint $state.archive_sha256)
            Write-Host 'INSTALL COMPLETE. Open the KVM web page, Ctrl+F5, then test microphone on/off/on and actual audio.' -ForegroundColor Green
        }
        'RollbackWeb' {
            $state = Get-PreparedState $Address
            Write-Host 'Close ALL KVM browser tabs. This rolls back only the web bridge, keeping kernel/runtime/backups.'
            [void](Invoke-RemoteKit $Address 'rollback-web' $state.device_fingerprint $state.archive_sha256)
            Write-Host 'WEB ROLLBACK COMPLETE. Kernel and runtime remain installed.' -ForegroundColor Green
        }
        default { throw 'Invalid menu action.' }
    }
}

if ($LibraryOnly) { return }
try {
    if (!$KvmAddress) { $KvmAddress = (Read-Host 'KVM IPv4 address (example 192.168.2.132)').Trim() }
    Assert-IPv4 $KvmAddress
    if ($Action -ne 'Menu') { Invoke-KitAction $KvmAddress $Action; exit 0 }
    while ($true) {
        Write-Host "`nNanoKVM Pro microphone kit 20261015 - $KvmAddress"
        Write-Host '1  Prepare (check stock device and save its backup to this Windows PC)'
        Write-Host '2  InstallAfterFlash (only after AXDL Passed + normal cold boot)'
        Write-Host '3  Status'
        Write-Host '4  RollbackWeb (keeps kernel and runtime)'
        Write-Host '0  Exit'
        $choice = Read-Host 'Choose'
        if ($choice -eq '0') { break }
        $chosen = switch ($choice) { '1' { 'Prepare' } '2' { 'InstallAfterFlash' } '3' { 'Status' } '4' { 'RollbackWeb' } default { '' } }
        if (!$chosen) { Write-Host 'Choose 0, 1, 2, 3, or 4.'; continue }
        try { Invoke-KitAction $KvmAddress $chosen }
        catch { Write-Host ("STOPPED: " + $_.Exception.Message) -ForegroundColor Red }
    }
} catch {
    Write-Host ("STOPPED: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
} finally {
    if ($PauseOnExit) { [void](Read-Host 'Task finished. Press Enter to close this task window') }
}
