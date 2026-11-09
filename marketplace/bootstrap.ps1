param([string]$Repository = 'jianbai-design/PrimiGenius-community-plugins', [switch]$Update)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$gh = (Get-Command gh -ErrorAction Stop).Source
function Invoke-GhJson([string[]]$Arguments) {
    $attempts = if ($Arguments -contains '--method') { 1 } else { 3 }
    for ($attempt = 0; $attempt -lt $attempts; $attempt++) {
        $result = & $gh @Arguments
        if ($LASTEXITCODE -eq 0) { break }
        if ($attempt + 1 -eq $attempts) { throw ('GitHub command failed: ' + ($Arguments | Select-Object -First 3)) }
        Start-Sleep -Seconds (1 + $attempt)
    }
    return (($result -join "`n") | ConvertFrom-Json)
}
function Get-OptionalGhJson([string]$Route) {
    for ($attempt = 0; $attempt -lt 3; $attempt++) {
        $raw = & $gh api $Route 2>$null
        if ($LASTEXITCODE -eq 0) { return (($raw -join "`n") | ConvertFrom-Json) }
        $failure = $null
        try { $failure = ($raw -join "`n") | ConvertFrom-Json } catch {}
        if ($failure.status -eq '404') { return $null }
        Start-Sleep -Seconds (1 + $attempt)
    }
    throw ('Could not read GitHub resource; network/authentication failures are not treated as absence: ' + $Route)
}
$account = Invoke-GhJson @('api', 'user')
if ($account.id -ne 134767426) { throw 'Only the verified platform administrator can initialize this market.' }
if ($Repository -ne 'jianbai-design/PrimiGenius-community-plugins') { throw 'Unexpected repository; update the client and controller together before changing it.' }
$repositoryInfo = Get-OptionalGhJson ('repos/' + $Repository)
if (-not $repositoryInfo) {
    & $gh repo create $Repository --public --description 'PrimiGenius community plugins, collaboration and moderated releases'
    if ($LASTEXITCODE -ne 0) { throw 'Could not create market repository.' }
    $repositoryInfo = Invoke-GhJson @('api', ('repos/' + $Repository))
}
if ($repositoryInfo.owner.id -ne $account.id -or $repositoryInfo.private) { throw 'The market must be public and owned by the platform administrator.' }
$existingState = Get-OptionalGhJson ('repos/' + $Repository + '/contents/market-state.json?ref=main')
$initialized = $null -ne $existingState
if ($initialized) {
    $remote = Invoke-GhJson @('api', ('repos/' + $Repository + '/contents/market-public-key.pem?ref=main'))
    $publicKey = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($remote.content))
    [IO.File]::WriteAllText((Join-Path $projectRoot 'src/marketplace-public-key.pem'), $publicKey, [Text.UTF8Encoding]::new($false))
    if (-not $Update) {
        Write-Output 'Market already initialized; retained existing signing key and state.'
        exit 0
    }
}
if (-not $initialized -and $repositoryInfo.size -gt 0) { throw 'Repository is not an empty market; refusing to overwrite its contents.' }
$temporary = Join-Path $projectRoot ('marketplace/.bootstrap-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($temporary) | Out-Null
try {
    if (-not $initialized) {
    $keys = (& node -e "const c=require('crypto'); const p=c.generateKeyPairSync('rsa',{modulusLength:3072,publicKeyEncoding:{type:'spki',format:'pem'},privateKeyEncoding:{type:'pkcs8',format:'pem'}}); process.stdout.write(JSON.stringify(p));") | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Could not generate dedicated market signing key.' }
    $keys.privateKey | & $gh secret set MARKET_SIGNING_KEY --repo $Repository
    if ($LASTEXITCODE -ne 0) { throw 'Could not save market signing key to GitHub Actions secrets.' }
    $initial = @{schema=1; administrator_id=134767426; reviewers=@(); blocked=@(); plugins=@{}; requests=@{}; audit=@{}} 
    $initial.audit = @()
    $stateText = $initial | ConvertTo-Json -Depth 20
    $seed = @{message='Initialize GitHub-only community market'; branch='main'; content=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($stateText))}
    $payloadPath = Join-Path $temporary 'request.json'
    [IO.File]::WriteAllText($payloadPath, ($seed | ConvertTo-Json -Depth 20), [Text.UTF8Encoding]::new($false))
    $seedResult = Invoke-GhJson @('api','--method','PUT',('repos/' + $Repository + '/contents/market-state.json'),'--input',$payloadPath)
    $publicKey = $keys.publicKey
    } else {
        $payloadPath = Join-Path $temporary 'request.json'
        $reference = Invoke-GhJson @('api', ('repos/' + $Repository + '/git/ref/heads/main'))
        $seedResult = @{commit=@{sha=$reference.object.sha}}
    }
    $base = Invoke-GhJson @('api', ('repos/' + $Repository + '/git/commits/' + $seedResult.commit.sha))
    $files = @{
        'market_runner.py' = Join-Path $PSScriptRoot 'github/market_runner.py'
        'plugin_archive.py' = Join-Path $projectRoot 'backend/plugin_archive.py'
        'sign.js' = Join-Path $PSScriptRoot 'github/sign.js'
        '.github/workflows/market.yml' = Join-Path $PSScriptRoot 'github/workflow.yml'
    }
    $tree = @()
    foreach ($entry in $files.GetEnumerator()) {
        $tree += @{path=$entry.Key; mode='100644'; type='blob'; content=[IO.File]::ReadAllText($entry.Value)}
    }
    $tree += @{path='market-public-key.pem'; mode='100644'; type='blob'; content=$publicKey}
    $treeRequest = @{base_tree=$base.tree.sha; tree=$tree}
    [IO.File]::WriteAllText($payloadPath, ($treeRequest | ConvertTo-Json -Depth 20), [Text.UTF8Encoding]::new($false))
    $createdTree = Invoke-GhJson @('api','--method','POST',('repos/' + $Repository + '/git/trees'),'--input',$payloadPath)
    $commitRequest = @{message='Add verified marketplace review and publication workflow'; tree=$createdTree.sha; parents=@($seedResult.commit.sha)}
    [IO.File]::WriteAllText($payloadPath, ($commitRequest | ConvertTo-Json -Depth 20), [Text.UTF8Encoding]::new($false))
    $commit = Invoke-GhJson @('api','--method','POST',('repos/' + $Repository + '/git/commits'),'--input',$payloadPath)
    $refRequest = @{sha=$commit.sha; force=$false}
    [IO.File]::WriteAllText($payloadPath, ($refRequest | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    $null = Invoke-GhJson @('api','--method','PATCH',('repos/' + $Repository + '/git/refs/heads/main'),'--input',$payloadPath)
    [IO.File]::WriteAllText((Join-Path $projectRoot 'src/marketplace-public-key.pem'), $publicKey, [Text.UTF8Encoding]::new($false))
    $keys = $null
    Write-Output ('Initialized https://github.com/' + $Repository)
} finally {
    $resolvedTemporary = [IO.Path]::GetFullPath($temporary)
    $expectedRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'marketplace')) + [IO.Path]::DirectorySeparatorChar
    if (-not $resolvedTemporary.StartsWith($expectedRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe cleanup target.' }
    Remove-Item -LiteralPath $resolvedTemporary -Recurse -Force
}
