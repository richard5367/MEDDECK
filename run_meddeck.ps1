$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$secureKey = Read-Host "Pega una clave nueva de OpenAI (entrada oculta)" -AsSecureString
$keyPointer = [IntPtr]::Zero
$apiKey = $null

try {
    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    if ([string]::IsNullOrWhiteSpace($apiKey)) {
        throw "La clave de OpenAI no puede estar vacia."
    }

    $env:OPENAI_API_KEY = $apiKey
    if ([string]::IsNullOrWhiteSpace($env:OPENAI_MODEL)) {
        $env:OPENAI_MODEL = "gpt-4o-mini"
    }
    if ([string]::IsNullOrWhiteSpace($env:PORT)) {
        $env:PORT = "5009"
    }

    Write-Host "Clave recibida para este proceso (no se valida ni se muestra)."
    Write-Host "Iniciando MEDDECK en http://127.0.0.1:$env:PORT"
    & python meddeck.py
    if ($LASTEXITCODE -ne 0) {
        throw "MEDDECK termino con codigo $LASTEXITCODE."
    }
}
finally {
    Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
    if ($keyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    }
    if ($secureKey) {
        $secureKey.Dispose()
    }
    $apiKey = $null
}
