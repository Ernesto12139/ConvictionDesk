# Windows PowerShell 5.1 / PowerShell 7. Dot-source from the two public entrypoints.
Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT') { throw 'WINDOWS_DPAPI_REQUIRED' }
# A PS7 parent may pass an incompatible module search path to Windows PS5.1.
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
$script:MiaProviderKeys = [ordered]@{
    OpenAI = 'OPENAI_API_KEY'; DeepSeek = 'DEEPSEEK_API_KEY'
    Massive = 'MASSIVE_API_KEY'; Finnhub = 'FINNHUB_API_KEY'
    Jev = 'JEV_API_KEY'; Longbridge = 'LONGBRIDGE_MCP_ACCESS_TOKEN'
    Compatible = 'LLM_API_KEY'
}

function Get-MiaSecretRoot {
    if ($env:OS -ne 'Windows_NT') { throw 'WINDOWS_DPAPI_REQUIRED' }
    Join-Path $env:LOCALAPPDATA 'ConvictionDesk\secrets'
}

function Assert-MiaPrivatePath([string]$Path) {
    # Refuse links/junctions, including ancestors, before touching credential storage.
    $taskItem = [IO.DirectoryInfo]([IO.Path]::GetFullPath($Path))
    while ($null -ne $taskItem) {
        if ($taskItem.Exists -and ($taskItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'SECRET_STORE_LINK_REFUSED'
        }
        $taskItem = $taskItem.Parent
    }
}

function Initialize-MiaSecretRoot([string]$Root) {
    Assert-MiaPrivatePath $Root
    if (-not [IO.Directory]::Exists($Root)) { [void][IO.Directory]::CreateDirectory($Root) }
    $taskAcl = Get-Acl -LiteralPath $Root
    $taskAcl.SetAccessRuleProtection($true, $false)
    $taskSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    foreach ($taskOldRule in @($taskAcl.Access)) { [void]$taskAcl.RemoveAccessRuleSpecific($taskOldRule) }
    foreach ($taskPrincipal in @($taskSid, [Security.Principal.SecurityIdentifier]'S-1-5-18')) {
        $taskRule = New-Object Security.AccessControl.FileSystemAccessRule($taskPrincipal,
            'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $taskAcl.AddAccessRule($taskRule)
    }
    $taskDirectory = New-Object IO.DirectoryInfo($Root)
    if ($PSVersionTable.PSVersion.Major -ge 7) {
        [IO.FileSystemAclExtensions]::SetAccessControl($taskDirectory, $taskAcl)
    } else { $taskDirectory.SetAccessControl($taskAcl) }
}

function Get-MiaSecretPath([string]$Root, [string]$Provider) {
    if (-not $script:MiaProviderKeys.Contains($Provider)) { throw 'UNKNOWN_PROVIDER' }
    Assert-MiaPrivatePath $Root
    $taskPath = Join-Path $Root ($Provider.ToLowerInvariant() + '.dpapi')
    if ((Test-Path -LiteralPath $taskPath) -and
        ((Get-Item -LiteralPath $taskPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'SECRET_STORE_LINK_REFUSED'
    }
    return $taskPath
}

function Read-MiaSecret([string]$Root, [string]$Provider) {
    $taskPath = Get-MiaSecretPath $Root $Provider
    if (-not [IO.File]::Exists($taskPath)) { return $null }
    try {
        $taskSecret = ConvertTo-SecureString ([IO.File]::ReadAllText($taskPath)) -ErrorAction Stop
        if ($taskSecret.Length -eq 0) { throw 'EMPTY' }
        return $taskSecret
    } catch { throw 'SECRET_UNREADABLE_RECONFIGURE_PROVIDER' }
}

function Save-MiaSecret([string]$Root, [string]$Provider, [Security.SecureString]$Secret) {
    if ($null -eq $Secret -or $Secret.Length -eq 0) { return $false }
    Initialize-MiaSecretRoot $Root
    $taskPath = Get-MiaSecretPath $Root $Provider
    $taskTemp = Join-Path $Root ([guid]::NewGuid().ToString('N') + '.tmp')
    $taskBackup = Join-Path $Root ([guid]::NewGuid().ToString('N') + '.bak')
    try {
        $taskCipher = ConvertFrom-SecureString $Secret -ErrorAction Stop
        $taskCheck = ConvertTo-SecureString $taskCipher -ErrorAction Stop
        try { if ($taskCheck.Length -ne $Secret.Length) { throw 'VERIFY_FAILED' } }
        finally { $taskCheck.Dispose() }
        [IO.File]::WriteAllText($taskTemp, $taskCipher, [Text.Encoding]::ASCII)
        if ([IO.File]::Exists($taskPath)) { [IO.File]::Replace($taskTemp, $taskPath, $taskBackup) }
        else { [IO.File]::Move($taskTemp, $taskPath) }
        return $true
    } catch { throw 'SECRET_SAVE_FAILED_PREVIOUS_FILE_PRESERVED' }
    finally {
        if ([IO.File]::Exists($taskTemp)) { [IO.File]::Delete($taskTemp) }
        if ([IO.File]::Exists($taskBackup)) { [IO.File]::Delete($taskBackup) }
    }
}

function Remove-MiaSecret([string]$Root, [string]$Provider) {
    [IO.File]::Delete((Get-MiaSecretPath $Root $Provider))
}

function Get-MiaSecretStatus([string]$Root) {
    foreach ($taskProvider in $script:MiaProviderKeys.Keys) {
        $taskState = 'NOT_CONFIGURED'
        try {
            $taskSecret = Read-MiaSecret $Root $taskProvider
            if ($null -ne $taskSecret) { $taskState = 'CONFIGURED_LOCAL'; $taskSecret.Dispose() }
        } catch { $taskState = 'UNREADABLE_LOCAL' }
        [pscustomobject]@{Provider=$taskProvider; Variable=$script:MiaProviderKeys[$taskProvider]; Status=$taskState}
    }
}

function New-MiaServiceEnvironment {
    param([string]$Root, [string]$Mode, [string]$Provider,
        [string[]]$OptionalProviders = @(), [string]$CredentialSource = 'Local')
    $taskEnvironment = @{}
    # Deliberately do not copy arbitrary Agent/provider credentials or Python hooks.
    foreach ($taskName in @('SystemRoot','WINDIR','SystemDrive','COMSPEC','PATH','PATHEXT',
        'TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA','PROGRAMDATA','OS')) {
        $taskValue = [Environment]::GetEnvironmentVariable($taskName, 'Process')
        if ($taskValue) { $taskEnvironment[$taskName] = $taskValue }
    }
    if ($Mode -eq 'Demo') {
        $taskEnvironment['MIA_RUNTIME_MODE'] = 'PUBLIC_DEMO'
        return $taskEnvironment
    }
    $taskEnvironment['MIA_RUNTIME_MODE'] = 'OWNER'
    $taskEnvironment['LLM_PROVIDER'] = $Provider.ToLowerInvariant()
    foreach ($taskSelected in @($Provider) + @($OptionalProviders)) {
        if (-not $script:MiaProviderKeys.Contains($taskSelected)) { throw 'UNKNOWN_PROVIDER' }
        $taskName = $script:MiaProviderKeys[$taskSelected]
        $taskValue = $null
        if ($CredentialSource -eq 'Environment') {
            $taskValue = [Environment]::GetEnvironmentVariable($taskName, 'Process')
        } else {
            $taskSecret = Read-MiaSecret $Root $taskSelected
            if ($null -ne $taskSecret) {
                $taskPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($taskSecret)
                try { $taskValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($taskPointer) }
                finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($taskPointer); $taskSecret.Dispose() }
            }
        }
        if ([string]::IsNullOrWhiteSpace($taskValue)) {
            throw ('MISSING_' + $taskName + ': run scripts\Configure-MiaProviders.ps1')
        }
        $taskEnvironment[$taskName] = $taskValue
    }
    if ($OptionalProviders -contains 'Massive') { $taskEnvironment['MARKET_PROVIDER'] = 'massive' }
    if ($OptionalProviders -contains 'Longbridge') { $taskEnvironment['MIA_RESEARCH_MCP'] = 'readonly' }
    return $taskEnvironment
}

function Resolve-MiaLauncherPath([string]$Path) {
    if ([IO.Path]::IsPathRooted($Path)) { return [IO.Path]::GetFullPath($Path) }
    if ($PWD.Provider.Name -ne 'FileSystem') { throw 'FILESYSTEM_WORKING_DIRECTORY_REQUIRED' }
    return [IO.Path]::GetFullPath((Join-Path $PWD.ProviderPath $Path))
}
