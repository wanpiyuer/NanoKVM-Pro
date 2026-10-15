[CmdletBinding()]
param([switch]$PreviewOnly)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Start.ps1') -LibraryOnly
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$script:RunningTask = $null
$script:RunningKind = ''
$script:ActionButtons = New-Object System.Collections.Generic.List[System.Windows.Forms.Button]

$form = New-Object System.Windows.Forms.Form
$form.Text = 'NanoKVM Pro 麦克风安装助手 · 20261015'
$form.ClientSize = New-Object System.Drawing.Size(790, 540)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedSingle'
$form.MaximizeBox = $false
$form.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10)
$form.BackColor = [System.Drawing.Color]::FromArgb(247, 249, 251)

function Add-Label([string]$Text, [int]$X, [int]$Y, [int]$Width, [int]$Height) {
    $label = New-Object System.Windows.Forms.Label
    $label.Text = $Text
    $label.Location = New-Object System.Drawing.Point($X, $Y)
    $label.Size = New-Object System.Drawing.Size($Width, $Height)
    $form.Controls.Add($label)
    return $label
}
function Show-Error([string]$Message) {
    [void][System.Windows.Forms.MessageBox]::Show($form, $Message, '操作已停止', 'OK', 'Error')
}
function Set-Running($Process, [string]$Kind) {
    $script:RunningTask = $Process
    $script:RunningKind = $Kind
    foreach ($button in $script:ActionButtons) { $button.Enabled = $false }
    $addressBox.Enabled = $false
    $statusLabel.Text = '任务窗口已自动打开。按提示输入 KVM root 密码即可，无需输入命令。'
    if ($Kind -eq 'Driver') { $statusLabel.Text = '正在安装 AXERA 下载驱动。Windows 可能要求管理员授权；不会自动重启电脑。' }
}
function Start-KvmTask([ValidateSet('Prepare', 'InstallAfterFlash', 'Status', 'RollbackWeb')][string]$TaskAction) {
    try {
        if ($null -ne $script:RunningTask -and !$script:RunningTask.HasExited) { throw '已有任务正在运行，请先等待任务窗口结束。' }
        $address = $addressBox.Text.Trim()
        Assert-IPv4 $address
        Assert-KitFiles
        $program = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
        $worker = Get-SafeLocalPath 'Start.ps1'
        $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $worker + '" -KvmAddress ' + $address + ' -Action ' + $TaskAction + ' -PauseOnExit'
        $taskProcess = Start-Process -FilePath $program -ArgumentList $arguments -WorkingDirectory $script:KitRoot -WindowStyle Normal -PassThru
        Set-Running $taskProcess $TaskAction
    } catch { Show-Error $_.Exception.Message }
}
function Add-Button([string]$Text, [int]$X, [int]$Y, [int]$Width, [int]$Height, [scriptblock]$OnClick, [bool]$ActionButton = $true) {
    $button = New-Object System.Windows.Forms.Button
    $button.Text = $Text
    $button.Location = New-Object System.Drawing.Point($X, $Y)
    $button.Size = New-Object System.Drawing.Size($Width, $Height)
    $button.FlatStyle = 'System'
    $button.Add_Click($OnClick)
    $form.Controls.Add($button)
    if ($ActionButton) { $script:ActionButtons.Add($button) }
    return $button
}

$title = Add-Label 'NanoKVM Pro 麦克风安装助手' 28 22 730 35
$title.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 18, [System.Drawing.FontStyle]::Bold)
[void](Add-Label '新机流程：检查备份 → 刷一次内核 → 自动安装网页联动' 30 68 730 28)
[void](Add-Label 'KVM 的 IPv4 地址' 30 112 165 30)
$addressBox = New-Object System.Windows.Forms.TextBox
$addressBox.Location = New-Object System.Drawing.Point(195, 109)
$addressBox.Size = New-Object System.Drawing.Size(245, 30)
$addressBox.Font = New-Object System.Drawing.Font('Consolas', 12)
$form.Controls.Add($addressBox)
[void](Add-Label '例如 192.168.2.132' 462 113 270 28)

[void](Add-Button '① 检查并备份' 30 160 350 48 { Start-KvmTask 'Prepare' })
[void](Add-Label '自动核对原厂版本，备份本机并下载到 Windows。' 405 170 355 38)
[void](Add-Button '安装 AXERA 驱动' 30 221 167 44 {
    try {
        Assert-KitFiles
        $inf = Get-SafeLocalPath 'tools/Driver_V1.20.46.1/DriversForWin10/Drivers/axeravcom.inf'
        $pnputil = Join-Path $env:SystemRoot 'System32/pnputil.exe'
        $driverProcess = Start-Process -FilePath $pnputil -ArgumentList ('/add-driver "' + $inf + '" /install') -Verb RunAs -PassThru -WorkingDirectory (Split-Path -Parent $inf)
        Set-Running $driverProcess 'Driver'
    } catch { Show-Error $_.Exception.Message }
})
[void](Add-Button '② 打开 AXDL' 211 221 169 44 {
    try {
        Assert-KitFiles
        $address = $addressBox.Text.Trim()
        Assert-IPv4 $address
        $statePath = Get-SafeLocalPath ('backups/' + $address + '/state.json')
        if (!(Test-Path -LiteralPath $statePath -PathType Leaf)) { throw '请先对这台新机完成“① 检查并备份”，再打开烧写工具。' }
        [void](Read-PreparedState $statePath)
        $axdl = Get-SafeLocalPath 'tools/AXDL_V1.24.22.1/Bin/AXDL.exe'
        if (@(Get-Process -Name AXDL -ErrorAction SilentlyContinue).Count -gt 0) { throw 'AXDL 已经打开。请使用现有窗口；若要重开，先停止并关闭现有 AXDL。' }
        [void](Start-Process -FilePath 'notepad.exe' -ArgumentList ('"' + (Get-SafeLocalPath 'AXDL-操作.txt') + '"') -PassThru)
        [void](Start-Process -FilePath 'explorer.exe' -ArgumentList ('/select,"' + (Get-SafeLocalPath 'candidate/REVIEW-candidate-kernel-A.axp') + '"') -PassThru)
        [void](Start-Process -FilePath $axdl -WorkingDirectory (Split-Path -Parent $axdl) -WindowStyle Normal -PassThru)
        $statusLabel.Text = '已打开 AXDL、候选文件位置和逐步说明。按说明载包、核对设置，然后现场确认刷写。'
    } catch { Show-Error $_.Exception.Message }
})
[void](Add-Label '先装下载驱动，再按说明载包并确认单 KERNEL 刷写。' 405 230 355 45)
[void](Add-Button '③ 刷写后自动安装' 30 282 350 48 {
    if (@(Get-Process -Name AXDL -ErrorAction SilentlyContinue).Count -gt 0) {
        Show-Error '请先确认 AXDL 显示 Passed，点击 Stop 并关闭 AXDL；将 KVM 完全断电，再正常上电联网。'
        return
    }
    Start-KvmTask 'InstallAfterFlash'
})
[void](Add-Label 'AXDL Passed 后冷启动 KVM，关闭网页，再点击此处。' 405 291 355 45)
[void](Add-Button '查看状态' 30 352 167 42 { Start-KvmTask 'Status' })
[void](Add-Button '回退网页' 211 352 169 42 {
    $answer = [System.Windows.Forms.MessageBox]::Show($form, '将恢复原网页入口，保留候选内核、运行时和备份。请先关闭 KVM 网页。继续回退网页联动？', '回退网页', 'YesNo', 'Question')
    if ($answer -eq [System.Windows.Forms.DialogResult]::Yes) { Start-KvmTask 'RollbackWeb' }
})
[void](Add-Button '打开说明' 405 352 167 42 {
    [void](Start-Process -FilePath 'notepad.exe' -ArgumentList ('"' + (Get-SafeLocalPath '00-开始.txt') + '"') -PassThru)
} $false)
[void](Add-Button '打开备份文件夹' 587 352 173 42 {
    $backupFolder = Get-SafeLocalPath 'backups'
    [void][System.IO.Directory]::CreateDirectory($backupFolder)
    [void](Start-Process -FilePath 'explorer.exe' -ArgumentList ('"' + $backupFolder + '"') -PassThru)
} $false)

$statusLabel = Add-Label '先在 KVM 网页打开 SSH。安装时使用网线或 Wi-Fi，不依赖 USB 虚拟网卡。' 30 415 730 54
$statusLabel.ForeColor = [System.Drawing.Color]::FromArgb(28, 73, 110)
$footer = Add-Label '仅支持检查通过的 Pro 基线（应用 1.2.15）。保留 USB 名称；升级后需重新适配。' 30 489 730 30
$footer.ForeColor = [System.Drawing.Color]::FromArgb(95, 103, 113)
$footer.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 500
$timer.Add_Tick({
    if ($null -ne $script:RunningTask -and $script:RunningTask.HasExited) {
        $script:RunningTask.WaitForExit()
        $code = $script:RunningTask.ExitCode
        if ($code -eq 0) {
            $statusLabel.Text = '任务已成功完成：' + $script:RunningKind + '。请按说明进行下一步；详细输出保存在 logs 文件夹。'
            if ($script:RunningKind -eq 'Driver') { $statusLabel.Text = 'AXERA 驱动已加入 Windows 驱动库。连接处于下载模式的 KVM 后可由系统匹配。' }
        } elseif ($script:RunningKind -eq 'Driver' -and $code -eq 3010) {
            $statusLabel.Text = '驱动安装完成，Windows 要求重启。请自行安排重启后再刷写。'
        } else { $statusLabel.Text = '任务已停止，返回码 ' + $code + '。保留任务输出和 logs 日志，先检查原因。' }
        $script:RunningTask.Dispose()
        $script:RunningTask = $null
        foreach ($button in $script:ActionButtons) { $button.Enabled = $true }
        $addressBox.Enabled = $true
    }
})
$form.Add_FormClosing({
    if ($null -ne $script:RunningTask -and !$script:RunningTask.HasExited) {
        $_.Cancel = $true
        Show-Error '任务还在运行。请先在任务窗口等待完成，按 Enter 关闭任务窗口，再关闭安装助手。'
    }
})

if ($PreviewOnly) {
    $form.CreateControl()
    $form.Show()
    [System.Windows.Forms.Application]::DoEvents()
    $bitmap = New-Object System.Drawing.Bitmap($form.Width, $form.Height)
    $form.DrawToBitmap($bitmap, (New-Object System.Drawing.Rectangle(0, 0, $form.Width, $form.Height)))
    $bitmap.Save((Join-Path $PSScriptRoot 'gui-preview.png'), [System.Drawing.Imaging.ImageFormat]::Png)
    $bitmap.Dispose()
    $form.Dispose()
    return
}
$timer.Start()
[void]$form.ShowDialog()
$timer.Dispose()
$form.Dispose()
