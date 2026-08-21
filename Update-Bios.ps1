<#
.SYNOPSIS
    Actualiza el BIOS/UEFI desde Windows usando la herramienta oficial del fabricante.

.DESCRIPTION
    Detecta el fabricante del equipo y delega el flasheo en la utilidad soportada
    por esa marca (nunca escribe la flash directamente):

        Dell    -> Dell Command | Update (dcu-cli.exe)
        HP      -> HP Client Management Script Library (modulo HPCMSL)
        Lenovo  -> LSUClient (Lenovo System Update via PowerShell) / Thin Installer
        Otros   -> instalador .exe del fabricante que le pases con -InstallerPath

    Antes de tocar nada aplica las comprobaciones de seguridad habituales:
    privilegios de administrador, alimentacion de red / bateria suficiente,
    espacio en disco, ausencia de reinicios pendientes y suspension de BitLocker
    (para que el cambio de PCRs del TPM no provoque la pantalla de clave de
    recuperacion en el arranque siguiente).

.PARAMETER CheckOnly
    Solo comprueba si hay BIOS nuevo. No descarga ni instala nada.

.PARAMETER Reboot
    Permite que la herramienta reinicie el equipo al terminar. Por defecto NO
    se reinicia: el flasheo real ocurre durante el POST del siguiente arranque.

.PARAMETER BiosPassword
    Contrasena de administrador del BIOS, si el equipo la tiene configurada.

.PARAMETER InstallerPath
    Ruta al .exe de actualizacion descargado del fabricante. Necesario para
    marcas sin CLI oficial (ASUS, MSI, Gigabyte, ASRock, Acer...).

.PARAMETER InstallerArgs
    Argumentos de instalacion silenciosa para -InstallerPath. Por defecto '/s'.

.PARAMETER MinBattery
    Porcentaje minimo de bateria exigido en portatiles. Por defecto 40.

.PARAMETER CheckWindowsUpdate
    Ademas, lista actualizaciones de firmware ofrecidas por Windows Update.

.PARAMETER LogPath
    Fichero de log. Por defecto C:\ProgramData\BiosUpdate\bios-update.log

.EXAMPLE
    .\Update-Bios.ps1 -CheckOnly
    Solo informa: modelo, version de BIOS actual y si hay una mas reciente.

.EXAMPLE
    .\Update-Bios.ps1 -WhatIf
    Simula todo el proceso sin aplicar cambios.

.EXAMPLE
    .\Update-Bios.ps1 -Reboot -Confirm:$false
    Actualiza y reinicia sin pedir confirmacion (para despliegues desatendidos).

.EXAMPLE
    .\Update-Bios.ps1 -InstallerPath 'C:\Temp\ASUS-BIOS-1402.exe' -InstallerArgs '/s','/norestart'
    Marca sin CLI: ejecuta el instalador oficial con las comprobaciones previas.

.NOTES
    Ejecutar en una consola PowerShell ELEVADA (Ejecutar como administrador).
    Codigos de salida: 0 = OK / sin cambios, 1 = error, 3010 = reinicio pendiente.
#>

#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [switch]$CheckOnly,
    [switch]$Reboot,
    [string]$BiosPassword,
    [string]$InstallerPath,
    [string[]]$InstallerArgs = @('/s'),
    [ValidateRange(0, 100)][int]$MinBattery = 40,
    [switch]$CheckWindowsUpdate,
    [string]$LogPath = "$env:ProgramData\BiosUpdate\bios-update.log"
)

$ErrorActionPreference = 'Stop'
$script:LogFile   = $LogPath
$script:LogDir    = Split-Path -Path $LogPath -Parent
$script:NeedsBoot = $false

#region ---------------------------------------------------------- utilidades

function Initialize-Logging {
    if (-not (Test-Path -LiteralPath $script:LogDir)) {
        New-Item -Path $script:LogDir -ItemType Directory -Force | Out-Null
    }
}

function Write-Log {
    param(
        [Parameter(Mandatory)][string]$Message,
        [ValidateSet('INFO', 'STEP', 'OK', 'WARN', 'ERROR')][string]$Level = 'INFO'
    )
    $line = '[{0}] [{1,-5}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    $color = switch ($Level) {
        'OK'    { 'Green' }
        'WARN'  { 'Yellow' }
        'ERROR' { 'Red' }
        'STEP'  { 'Cyan' }
        default { 'Gray' }
    }
    Write-Host $line -ForegroundColor $color
    try { Add-Content -LiteralPath $script:LogFile -Value $line -Encoding UTF8 } catch { }
}

function Get-HardwareInfo {
    $cs   = Get-CimInstance -ClassName Win32_ComputerSystem
    $bios = Get-CimInstance -ClassName Win32_BIOS
    $bb   = Get-CimInstance -ClassName Win32_BaseBoard -ErrorAction SilentlyContinue

    $secureBoot = try {
        if (Confirm-SecureBootUEFI) { 'UEFI (Secure Boot activado)' } else { 'UEFI (Secure Boot desactivado)' }
    } catch { 'BIOS legacy o no consultable' }

    [pscustomobject]@{
        Fabricante   = $cs.Manufacturer.Trim()
        Modelo       = $cs.Model.Trim()
        PlacaBase    = if ($bb) { ('{0} {1}' -f $bb.Manufacturer, $bb.Product).Trim() } else { 'n/d' }
        NumeroSerie  = $bios.SerialNumber
        VersionBIOS  = $bios.SMBIOSBIOSVersion
        FechaBIOS    = if ($bios.ReleaseDate) { $bios.ReleaseDate.ToString('yyyy-MM-dd') } else { 'n/d' }
        Firmware     = $secureBoot
    }
}

function Resolve-Vendor {
    param([Parameter(Mandatory)][string]$Manufacturer)

    switch -Regex ($Manufacturer) {
        'Dell'                { 'Dell';      break }
        'HP|Hewlett'          { 'HP';        break }
        'Lenovo|IBM'          { 'Lenovo';    break }
        'ASUS'                { 'ASUS';      break }
        'Micro-?Star|\bMSI\b' { 'MSI';       break }
        'Gigabyte'            { 'Gigabyte';  break }
        'ASRock'              { 'ASRock';    break }
        'Acer'                { 'Acer';      break }
        'Microsoft'           { 'Microsoft'; break }
        default               { 'Desconocido' }
    }
}

#endregion

#region ------------------------------------------------ comprobaciones previas

function Test-PowerState {
    $batteries = @(Get-CimInstance -ClassName Win32_Battery -ErrorAction SilentlyContinue)

    if ($batteries.Count -eq 0) {
        Write-Log 'Sin bateria detectada (sobremesa): se asume alimentacion de red.' 'INFO'
        return $true
    }

    $battery = $batteries[0]
    $charge  = [int]$battery.EstimatedChargeRemaining
    # BatteryStatus 1 = descargando; cualquier otro valor implica corriente alterna.
    $onAc    = ($battery.BatteryStatus -ne 1)

    Write-Log ("Bateria: {0}% | Alimentacion: {1}" -f $charge, $(if ($onAc) { 'red electrica' } else { 'bateria' }))

    if (-not $onAc) {
        Write-Log 'El equipo NO esta conectado a la corriente. Conecta el cargador.' 'ERROR'
        return $false
    }
    if ($charge -lt $MinBattery) {
        Write-Log ("Bateria al {0}%, por debajo del minimo exigido ({1}%)." -f $charge, $MinBattery) 'ERROR'
        return $false
    }
    return $true
}

function Test-PendingReboot {
    $keys = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending',
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
    )
    foreach ($key in $keys) {
        if (Test-Path -LiteralPath $key) { return $true }
    }
    $sm = Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' `
                           -Name 'PendingFileRenameOperations' -ErrorAction SilentlyContinue
    return [bool]($sm -and $sm.PendingFileRenameOperations)
}

function Test-FreeSpace {
    param([int]$MinimumMB = 2048)

    $drive  = Get-PSDrive -Name ($env:SystemDrive.TrimEnd(':')) -ErrorAction SilentlyContinue
    if (-not $drive) { return $true }
    $freeMB = [math]::Round($drive.Free / 1MB)

    Write-Log ("Espacio libre en {0} {1} MB" -f $env:SystemDrive, $freeMB)
    if ($freeMB -lt $MinimumMB) {
        Write-Log ("Se necesitan al menos {0} MB libres." -f $MinimumMB) 'ERROR'
        return $false
    }
    return $true
}

function Assert-Prerequisites {
    Write-Log 'Comprobaciones previas' 'STEP'

    $ok = $true
    if (-not (Test-PowerState))  { $ok = $false }
    if (-not (Test-FreeSpace))   { $ok = $false }

    if (Test-PendingReboot) {
        Write-Log 'Hay un reinicio pendiente. Reinicia y vuelve a lanzar el script.' 'ERROR'
        $ok = $false
    }

    if (-not $ok) { throw 'No se cumplen los requisitos minimos para actualizar el BIOS.' }
    Write-Log 'Comprobaciones previas superadas.' 'OK'
}

function Suspend-BitLockerProtection {
    if (-not (Get-Command -Name 'Get-BitLockerVolume' -ErrorAction SilentlyContinue)) {
        Write-Log 'Modulo BitLocker no disponible (Windows Home): se omite.' 'INFO'
        return
    }

    $protected = @(Get-BitLockerVolume -ErrorAction SilentlyContinue |
                   Where-Object { $_.ProtectionStatus -eq 'On' })

    if ($protected.Count -eq 0) {
        Write-Log 'BitLocker no esta activo en ningun volumen.' 'INFO'
        return
    }

    foreach ($volume in $protected) {
        Write-Log ("Suspendiendo BitLocker en {0} (2 reinicios)." -f $volume.MountPoint) 'STEP'
        # RebootCount 2: el flasheo consume un arranque extra en el POST.
        Suspend-BitLocker -MountPoint $volume.MountPoint -RebootCount 2 | Out-Null
    }
    Write-Log 'BitLocker suspendido. Ten a mano la clave de recuperacion por si acaso.' 'WARN'
}

#endregion

#region ---------------------------------------------- instaladores auxiliares

function Install-WithWinget {
    param(
        [Parameter(Mandatory)][string]$Id,
        [Parameter(Mandatory)][string]$DisplayName
    )

    if (-not (Get-Command -Name 'winget.exe' -ErrorAction SilentlyContinue)) {
        Write-Log 'winget no esta disponible en este equipo.' 'WARN'
        return $false
    }

    Write-Log ("Instalando {0} con winget..." -f $DisplayName) 'STEP'
    & winget.exe install --id $Id --exact --silent `
        --accept-package-agreements --accept-source-agreements --disable-interactivity

    if ($LASTEXITCODE -ne 0) {
        Write-Log ("winget devolvio el codigo {0}." -f $LASTEXITCODE) 'WARN'
        return $false
    }
    return $true
}

function Install-VendorModule {
    param([Parameter(Mandatory)][string]$Name)

    if (Get-Module -ListAvailable -Name $Name) {
        Import-Module -Name $Name -Force
        Write-Log ("Modulo {0} ya presente." -f $Name) 'INFO'
        return
    }

    Write-Log ("Instalando el modulo {0} desde PSGallery..." -f $Name) 'STEP'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    if (-not (Get-PackageProvider -Name NuGet -ListAvailable -ErrorAction SilentlyContinue)) {
        Install-PackageProvider -Name NuGet -MinimumVersion '2.8.5.201' -Force | Out-Null
    }
    Set-PSRepository -Name PSGallery -InstallationPolicy Trusted -ErrorAction SilentlyContinue

    Install-Module -Name $Name -Force -AllowClobber -AcceptLicense -Scope AllUsers
    Import-Module -Name $Name -Force
}

#endregion

#region -------------------------------------------------- rutinas por marca

function Invoke-DellBiosUpdate {
    $paths = @(
        (Join-Path $env:ProgramFiles 'Dell\CommandUpdate\dcu-cli.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Dell\CommandUpdate\dcu-cli.exe')
    )
    $dcu = $paths | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

    if (-not $dcu) {
        Write-Log 'Dell Command | Update no encontrado.' 'WARN'
        if (Install-WithWinget -Id 'Dell.CommandUpdate' -DisplayName 'Dell Command | Update') {
            $dcu = $paths | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        }
    }
    if (-not $dcu) {
        throw 'Instala Dell Command | Update desde https://www.dell.com/support/kbdoc/es-es/000177325'
    }

    Write-Log ("Usando {0}" -f $dcu) 'INFO'
    $scanLog = Join-Path $script:LogDir 'dcu-scan.log'

    Write-Log 'Buscando actualizaciones de BIOS...' 'STEP'
    & $dcu /scan -updateType=bios -outputLog="$scanLog" | Out-Null
    $code = $LASTEXITCODE

    switch ($code) {
        0   { Write-Log 'Hay una actualizacion de BIOS disponible.' 'OK' }
        500 { Write-Log 'El BIOS ya esta en la ultima version. Nada que hacer.' 'OK'; return }
        1   { Write-Log 'Se requiere reinicio antes de continuar.' 'ERROR'; return }
        3   { throw 'dcu-cli: este equipo no es un Dell.' }
        4   { throw 'dcu-cli: hacen falta privilegios de administrador.' }
        5   { throw 'dcu-cli: hay un reinicio pendiente de una operacion anterior.' }
        default { throw ("dcu-cli /scan devolvio el codigo {0}. Revisa {1}" -f $code, $scanLog) }
    }

    if ($CheckOnly) { return }

    $applyLog = Join-Path $script:LogDir 'dcu-apply.log'
    $cliArgs  = @(
        '/applyUpdates'
        '-updateType=bios'
        '-silent'
        '-autoSuspendBitLocker=enable'
        ('-reboot={0}' -f $(if ($Reboot) { 'enable' } else { 'disable' }))
        ('-outputLog={0}' -f $applyLog)
    )

    if ($BiosPassword) {
        # DCU exige la contrasena cifrada; se configura una sola vez con /configure.
        Write-Log 'Registrando la contrasena del BIOS en DCU (/configure)...' 'STEP'
        & $dcu /configure -silent ('-biosPassword={0}' -f $BiosPassword) | Out-Null
    }

    Write-Log 'Aplicando la actualizacion de BIOS...' 'STEP'
    & $dcu @cliArgs | Out-Null
    $code = $LASTEXITCODE

    switch ($code) {
        0   { Write-Log 'Actualizacion aplicada correctamente.' 'OK' }
        1   { Write-Log 'Aplicada. Se necesita reiniciar para completar el flasheo.' 'OK'
              $script:NeedsBoot = $true }
        500 { Write-Log 'No habia nada que aplicar.' 'OK' }
        default { throw ("dcu-cli /applyUpdates devolvio {0}. Revisa {1}" -f $code, $applyLog) }
    }
}

function Invoke-HPBiosUpdate {
    Install-VendorModule -Name 'HPCMSL'

    $current = Get-HPBIOSVersion
    Write-Log ("Version de BIOS segun HPCMSL: {0}" -f $current) 'INFO'

    if (-not (Get-HPBIOSUpdates -Check)) {
        Write-Log 'El BIOS ya esta en la ultima version. Nada que hacer.' 'OK'
        return
    }

    $latest = Get-HPBIOSUpdates -Latest
    Write-Log ("Actualizacion disponible: {0} ({1})" -f $latest.Ver, $latest.Date) 'OK'

    if ($CheckOnly) { return }

    $flash = @{
        Flash     = $true
        Yes       = $true
        BitLocker = 'suspend'
        Force     = $true
    }
    if ($BiosPassword) { $flash['Password'] = $BiosPassword }

    Write-Log 'Preparando el flasheo del BIOS (HPCMSL)...' 'STEP'
    Get-HPBIOSUpdates @flash

    Write-Log 'Flasheo programado. Se aplicara en el POST del proximo arranque.' 'OK'
    $script:NeedsBoot = $true
}

function Invoke-LenovoBiosUpdate {
    Install-VendorModule -Name 'LSUClient'

    Write-Log 'Consultando el catalogo de Lenovo...' 'STEP'
    $updates = @(Get-LSUpdate | Where-Object { $_.Category -match 'BIOS|UEFI' })

    if ($updates.Count -eq 0) {
        Write-Log 'El BIOS ya esta en la ultima version. Nada que hacer.' 'OK'
        return
    }

    foreach ($update in $updates) {
        Write-Log ("Disponible: {0} [{1}]" -f $update.Title, $update.ID) 'OK'
    }
    if ($CheckOnly) { return }

    $downloadDir = Join-Path $env:TEMP 'LSUPackages'
    Write-Log ("Descargando paquetes en {0}..." -f $downloadDir) 'STEP'
    $updates | Save-LSUpdate -Path $downloadDir -Verbose

    Write-Log 'Instalando la actualizacion de BIOS...' 'STEP'
    $updates | Install-LSUpdate -Path $downloadDir -SaveBIOSUpdateInfoToRegistry

    Write-Log 'Instalado. Se aplicara en el POST del proximo arranque.' 'OK'
    $script:NeedsBoot = $true
}

function Invoke-GenericBiosUpdate {
    param([Parameter(Mandatory)][string]$Vendor)

    if (-not $InstallerPath) {
        Write-Log ("{0} no ofrece una CLI oficial para actualizar el BIOS desde Windows." -f $Vendor) 'WARN'
        Write-Log 'Descarga el .exe de tu modelo en la web de soporte del fabricante y' 'WARN'
        Write-Log 'vuelve a lanzar el script con -InstallerPath "C:\ruta\al\bios.exe".'    'WARN'
        Write-Log 'Alternativa mas segura: copiar el fichero de BIOS a un USB FAT32 y'    'WARN'
        Write-Log 'usar la herramienta integrada en el firmware (ASUS EZ Flash, MSI'      'WARN'
        Write-Log 'M-Flash, Gigabyte Q-Flash, ASRock Instant Flash).'                     'WARN'
        return
    }

    if (-not (Test-Path -LiteralPath $InstallerPath)) {
        throw ("No existe el instalador indicado: {0}" -f $InstallerPath)
    }

    $signature = Get-AuthenticodeSignature -LiteralPath $InstallerPath
    Write-Log ("Firma digital: {0} - {1}" -f $signature.Status, $signature.SignerCertificate.Subject) 'INFO'
    if ($signature.Status -ne 'Valid') {
        throw 'El instalador no tiene una firma digital valida. Abortando por seguridad.'
    }

    if ($CheckOnly) {
        Write-Log 'Modo -CheckOnly: no se ejecuta el instalador.' 'INFO'
        return
    }

    Write-Log ("Ejecutando {0} {1}" -f $InstallerPath, ($InstallerArgs -join ' ')) 'STEP'
    $process = Start-Process -FilePath $InstallerPath -ArgumentList $InstallerArgs `
                             -Wait -PassThru -NoNewWindow

    Write-Log ("El instalador termino con el codigo {0}." -f $process.ExitCode) 'INFO'
    if ($process.ExitCode -notin 0, 3010) {
        throw ("El instalador fallo (codigo {0})." -f $process.ExitCode)
    }
    $script:NeedsBoot = $true
}

function Show-WindowsUpdateFirmware {
    Write-Log 'Consultando firmware ofrecido por Windows Update...' 'STEP'
    try {
        $session  = New-Object -ComObject 'Microsoft.Update.Session'
        $searcher = $session.CreateUpdateSearcher()
        $result   = $searcher.Search("IsInstalled=0 and Type='Driver'")

        $firmware = @($result.Updates | Where-Object { $_.Title -match 'firmware|BIOS|UEFI' })
        if ($firmware.Count -eq 0) {
            Write-Log 'Windows Update no ofrece firmware pendiente.' 'INFO'
            return
        }
        foreach ($item in $firmware) {
            Write-Log ("Windows Update ofrece: {0}" -f $item.Title) 'OK'
        }
    } catch {
        Write-Log ("No se pudo consultar Windows Update: {0}" -f $_.Exception.Message) 'WARN'
    }
}

#endregion

#region -------------------------------------------------------------- main

try {
    Initialize-Logging

    Write-Host ''
    Write-Log '=== Actualizacion de BIOS/UEFI desde Windows ===' 'STEP'

    $hardware = Get-HardwareInfo
    $hardware | Format-List | Out-String | Write-Host
    $hardware.PSObject.Properties | ForEach-Object {
        Add-Content -LiteralPath $script:LogFile -Value ("[hw] {0}: {1}" -f $_.Name, $_.Value)
    }

    $vendor = Resolve-Vendor -Manufacturer $hardware.Fabricante
    Write-Log ("Fabricante detectado: {0}" -f $vendor) 'INFO'

    if ($CheckWindowsUpdate) { Show-WindowsUpdateFirmware }

    Assert-Prerequisites

    if (-not $CheckOnly) {
        $target = '{0} {1} (BIOS {2})' -f $hardware.Fabricante, $hardware.Modelo, $hardware.VersionBIOS
        if (-not $PSCmdlet.ShouldProcess($target, 'Actualizar el firmware del sistema')) {
            Write-Log 'Operacion cancelada por el usuario.' 'WARN'
            exit 0
        }
        Suspend-BitLockerProtection
    }

    switch ($vendor) {
        'Dell'   { Invoke-DellBiosUpdate }
        'HP'     { Invoke-HPBiosUpdate }
        'Lenovo' { Invoke-LenovoBiosUpdate }
        'Microsoft' {
            Write-Log 'Surface: el firmware se distribuye exclusivamente por Windows Update.' 'INFO'
            Show-WindowsUpdateFirmware
        }
        default  { Invoke-GenericBiosUpdate -Vendor $vendor }
    }

    if ($script:NeedsBoot) {
        Write-Log 'IMPORTANTE: el flasheo se completa durante el POST del siguiente arranque.' 'WARN'
        Write-Log 'NO apagues ni desconectes el equipo mientras dure ese proceso.'             'WARN'

        if ($Reboot) {
            Write-Log 'Reiniciando en 30 segundos... (Ctrl+C para cancelar)' 'STEP'
            Start-Sleep -Seconds 30
            Restart-Computer -Force
        } else {
            Write-Log 'Reinicia manualmente cuando puedas para aplicar el BIOS.' 'OK'
            exit 3010
        }
    }

    Write-Log ("Finalizado. Log completo en {0}" -f $script:LogFile) 'OK'
    exit 0
}
catch {
    Write-Log $_.Exception.Message 'ERROR'
    Write-Log ("Traza: {0}" -f $_.ScriptStackTrace) 'ERROR'
    exit 1
}

#endregion
