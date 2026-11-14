<#
Выпуск новой версии TimScheduleBot на сервер одной командой:
  .\deploy.ps1            # тесты, архив, проверка хеша, установка, проверка запуска
  .\deploy.ps1 -DryRun    # только тесты и сборка архива, без обращения к серверу
При ошибке на этапе установки сервер сам возвращает прежний код. База и токен не меняются.
#>
param([switch]$DryRun)
$ErrorActionPreference = 'Continue'  # stderr нативных команд не считается ошибкой; проверяем коды возврата
Set-Location $PSScriptRoot
$release = Get-Date -Format 'yyyyMMdd-HHmmss'
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

Write-Host '[1/5] Тесты на Windows'
& $py -X utf8 -m unittest -q
if ($LASTEXITCODE -ne 0) { throw 'Тесты не прошли: выпуск отменён.' }

Write-Host '[2/5] Сборка архива (без .env, базы и ключей)'
$tmp = Join-Path $env:TEMP "timschedulebot-$release"
New-Item -ItemType Directory -Path $tmp | Out-Null
$archive = Join-Path $tmp 'release.tar.gz'
$items = @(Get-ChildItem -File -Filter *.py | Where-Object { $_.Name -ne 'get_id.py' } | ForEach-Object Name) + @('deploy', 'requirements.txt', 'example_note.txt')
& "$env:SystemRoot\System32\tar.exe" -czf $archive @items
if ($LASTEXITCODE -ne 0) { throw 'Не удалось собрать архив.' }
$localHash = (Get-FileHash $archive -Algorithm SHA256).Hash.ToLower()
Write-Host "    SHA-256: $localHash"
if ($DryRun) { Write-Host "DryRun: архив $archive собран, сервер не затронут."; return }

$target = $env:TIMSCHEDULE_SSH_TARGET
if (-not $target) { throw 'Set TIMSCHEDULE_SSH_TARGET (ubuntu@your-server).' }
$key = Join-Path $env:USERPROFILE '.ssh\timschedulebot_deploy'
$ssh = @('-i', $key, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10')
$stage = "/home/ubuntu/timschedulebot-stage/$release"

Write-Host '[3/5] Передача и проверка хеша'
ssh.exe @ssh $target "mkdir -p -m 700 $stage"
if ($LASTEXITCODE -ne 0) { throw 'SSH недоступен.' }
scp.exe @ssh $archive "${target}:$stage/release.tar.gz"
if ($LASTEXITCODE -ne 0) { throw 'Не удалось передать архив.' }
$remoteHash = (ssh.exe @ssh $target "sha256sum $stage/release.tar.gz").Split(' ')[0]
if ($remoteHash -ne $localHash) { throw 'SHA-256 не совпал: выпуск отменён.' }

Write-Host '[4/5] Установка на сервере'
ssh.exe @ssh $target "cd $stage && tar -xzf release.tar.gz && bash deploy/remote_release.sh $release"
if ($LASTEXITCODE -ne 0) { throw 'Установка не удалась; сервер вернул прежнюю версию. Смотри вывод выше.' }

Write-Host '[5/5] Готово. Проверь /status в Telegram.'
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue

