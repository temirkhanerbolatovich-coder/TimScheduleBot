<#
Скачивает свежую копию базы с сервера на этот компьютер (проверяет целостность, сжимает в ZIP).
  .\pull_backup.ps1
Копии лежат в папке backups-offsite рядом со скриптом. База содержит личные планы: храни в защищённом месте.
#>
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$target = $env:TIMSCHEDULE_SSH_TARGET
if (-not $target) { throw 'Set TIMSCHEDULE_SSH_TARGET (ubuntu@your-server).' }
$key = Join-Path $env:USERPROFILE '.ssh\timschedulebot_deploy'
$ssh = @('-i', $key, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10')
$out = Join-Path $PSScriptRoot 'backups-offsite'
New-Item -ItemType Directory -Force $out | Out-Null
ssh.exe @ssh $target 'sudo -n systemctl start timschedulebot-backup.service'
$name = (ssh.exe @ssh $target "sudo -n bash -c 'ls -1t /var/lib/timschedulebot/backups/plans-*.sqlite3 | head -1'").Trim()
if (-not $name) { throw 'Копия не найдена на сервере.' }
$tmpRemote = '/home/ubuntu/timschedulebot-stage/pull.sqlite3'
ssh.exe @ssh $target "sudo -n install -m 600 -o ubuntu -g ubuntu $name $tmpRemote"
$local = Join-Path $out ([IO.Path]::GetFileName($name))
scp.exe @ssh "${target}:$tmpRemote" $local
ssh.exe @ssh $target "rm -f $tmpRemote"
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $py -X utf8 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); assert c.execute('PRAGMA quick_check').fetchone()[0]=='ok'; print('Целостность копии: ok')" $local
Compress-Archive -Path $local -DestinationPath "$local.zip" -Force
Remove-Item $local
Write-Host "Сохранено: $local.zip. ZIP не зашифрован: держи папку backups-offsite на диске с BitLocker."
