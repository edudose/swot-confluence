# Update-Bios.ps1

Script de PowerShell para actualizar el BIOS/UEFI desde Windows usando la
herramienta **oficial de cada fabricante**. El script nunca escribe la memoria
flash directamente: detecta la marca y delega en la utilidad soportada.

| Fabricante | Herramienta usada | Origen |
|---|---|---|
| Dell | `dcu-cli.exe` (Dell Command \| Update) | winget: `Dell.CommandUpdate` |
| HP | módulo `HPCMSL` | PSGallery |
| Lenovo | módulo `LSUClient` | PSGallery |
| Microsoft (Surface) | Windows Update | integrado |
| ASUS / MSI / Gigabyte / ASRock / Acer | `.exe` del fabricante vía `-InstallerPath` | web de soporte |

## Comprobaciones de seguridad previas

- Requiere consola **elevada** (`#Requires -RunAsAdministrator`).
- Verifica alimentación de red y batería mínima (`-MinBattery`, por defecto 40 %).
- Comprueba espacio libre en disco (2 GB) y ausencia de reinicios pendientes.
- **Suspende BitLocker** con `-RebootCount 2` para evitar la pantalla de clave de
  recuperación cuando cambian los PCRs del TPM tras el flasheo.
- Valida la **firma Authenticode** de cualquier `.exe` pasado con `-InstallerPath`.
- Soporta `-WhatIf` y pide confirmación (`ConfirmImpact = 'High'`).

## Uso

```powershell
# 1) Solo mirar: modelo, versión actual y si hay BIOS nuevo
.\Update-Bios.ps1 -CheckOnly

# 2) Simulacro completo sin tocar nada
.\Update-Bios.ps1 -WhatIf

# 3) Actualizar (pide confirmación, no reinicia solo)
.\Update-Bios.ps1

# 4) Desatendido: actualiza y reinicia
.\Update-Bios.ps1 -Reboot -Confirm:$false

# 5) Equipo con contraseña de BIOS
.\Update-Bios.ps1 -BiosPassword 'MiClaveBIOS'

# 6) Marca sin CLI (ASUS, MSI, Gigabyte...)
.\Update-Bios.ps1 -InstallerPath 'C:\Temp\ASUS-BIOS-1402.exe' -InstallerArgs '/s','/norestart'

# 7) Además, ver qué firmware ofrece Windows Update
.\Update-Bios.ps1 -CheckOnly -CheckWindowsUpdate
```

Si la política de ejecución lo bloquea:

```powershell
powershell -ExecutionPolicy Bypass -File .\Update-Bios.ps1 -CheckOnly
```

## Códigos de salida

| Código | Significado |
|---|---|
| `0` | Correcto o no había nada que actualizar |
| `1` | Error (motivo en el log) |
| `3010` | Correcto, pero hace falta reiniciar para completar el flasheo |

Log por defecto: `C:\ProgramData\BiosUpdate\bios-update.log`

## Avisos

- El flasheo real ocurre durante el **POST del siguiente arranque**, no dentro de
  Windows. No apagues ni desconectes el equipo durante ese proceso.
- Anota la **clave de recuperación de BitLocker** antes de empezar
  (`manage-bde -protectors -get C:`).
- Un BIOS actualizado puede **resetear ajustes** del firmware (orden de arranque,
  perfiles XMP, virtualización). Anótalos antes.
- En equipos clónicos/ensamblados el fabricante que reporta Windows es el de la
  **placa base**; comprueba el modelo exacto antes de aplicar nada.
