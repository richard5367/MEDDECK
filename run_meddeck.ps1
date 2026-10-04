$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# Check whether .env already contains OPENAI_API_KEY.
$envFile = Join-Path $PSScriptRoot ".env"
$hasEnvKey = $false
if (Test-Path $envFile) {
    $content = Get-Content $envFile -Raw
    if ($content -match "OPENAI_API_KEY=\s*(\S+)") {
        $hasEnvKey = $true
    }
}

if (-not [string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)) {
    $hasEnvKey = $true
}

if ($hasEnvKey) {
    Write-Host "An existing OPENAI_API_KEY configuration was detected (.env / environment)." -ForegroundColor Green
    $resp = Read-Host "Press Enter to use the configured key, or type 'NEW' to enter another"
    if ($resp.Trim().ToUpper() -eq "NEW") {
        $secureKey = Read-Host "Enter a new OpenAI key (hidden input)" -AsSecureString
        $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
        $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
        if (-not [string]::IsNullOrWhiteSpace($apiKey)) {
            $env:OPENAI_API_KEY = $apiKey
        }
    }
} else {
    $secureKey = Read-Host "Enter a new OpenAI key (hidden input), or press Enter if it is configured in .env" -AsSecureString
    if ($secureKey.Length -gt 0) {
        $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
        $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
        if (-not [string]::IsNullOrWhiteSpace($apiKey)) {
            $env:OPENAI_API_KEY = $apiKey
        }
    }
}

if (-not [string]::IsNullOrWhiteSpace($content) -and $content -match "OPENAI_MODEL=\s*(\S+)") {
    $env:OPENAI_MODEL = $matches[1]
} elseif ([string]::IsNullOrWhiteSpace($env:OPENAI_MODEL)) {
    $env:OPENAI_MODEL = "llama3.2"
}
if ([string]::IsNullOrWhiteSpace($env:PORT)) {
    $env:PORT = "5000"
}

Write-Host "Starting MEDDECK at http://127.0.0.1:$env:PORT" -ForegroundColor Cyan
& python meddeck.py
