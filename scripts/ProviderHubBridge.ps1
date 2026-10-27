# Private IPC for the Python hub. Secrets enter/leave only redirected stdin/stdout.
param([Parameter(Mandatory=$true)][ValidateSet('read','status','set','remove')][string]$Operation,
      [Parameter(Mandatory=$true)][string]$Provider)
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
. (Join-Path $PSScriptRoot 'ProviderSecrets.ps1')
try {
    $taskRoot = Get-MiaSecretRoot
    switch ($Operation) {
        'status' { Get-MiaSecretStatus $taskRoot | Where-Object Provider -EQ $Provider | ConvertTo-Json -Compress }
        'set' {
            $taskInput = [Console]::In.ReadToEnd() | ConvertFrom-Json
            $taskSecure = ConvertTo-SecureString $taskInput.secret -AsPlainText -Force
            try { [void](Save-MiaSecret $taskRoot $Provider $taskSecure) } finally { $taskSecure.Dispose() }
            [Console]::Out.Write('{"ok":true}')
        }
        'remove' { Remove-MiaSecret $taskRoot $Provider; [Console]::Out.Write('{"ok":true}') }
        'read' {
            $taskSecure = Read-MiaSecret $taskRoot $Provider
            if ($null -eq $taskSecure) { [Console]::Out.Write('null'); break }
            $taskPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($taskSecure)
            try {
                @{secret=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($taskPointer)} | ConvertTo-Json -Compress
            } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($taskPointer); $taskSecure.Dispose() }
        }
    }
} catch { [Console]::Error.Write('VAULT_OPERATION_FAILED'); exit 1 }
