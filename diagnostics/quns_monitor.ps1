$ErrorActionPreference='SilentlyContinue'
$SP = Split-Path $MyInvocation.MyCommand.Path
$log = Join-Path $SP 'wamon.log'
$aumid = '5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App'
$nkey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\$aumid"
$ndir = "$env:LOCALAPPDATA\Microsoft\Windows\Notifications"
$idm  = "$ndir\wpnidm"

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class Q {
  [DllImport("shell32.dll")] public static extern int SHQueryUserNotificationState(out int s);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder t, int c);
  [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h, out int pid);
  public static string State() { int s; SHQueryUserNotificationState(out s);
    switch(s){case 1:return "NOT_PRESENT";case 2:return "BUSY(fullscreen+D3D)";case 3:return "D3D_FULLSCREEN";
    case 4:return "PRESENTATION_MODE";case 5:return "ACCEPTS_NOTIFICATIONS";case 6:return "QUIET_TIME(DND)";
    case 7:return "APP_FULLSCREEN";default:return "UNKNOWN("+s+")";} }
  public static string Fg() { IntPtr h=GetForegroundWindow(); var sb=new StringBuilder(200); GetWindowText(h,sb,200);
    int pid; GetWindowThreadProcessId(h,out pid);
    string n=""; try { n=System.Diagnostics.Process.GetProcessById(pid).ProcessName; } catch {}
    return n+" | "+sb.ToString(); }
}
"@

function Dnd {
  $b = "HKCU:\Software\Microsoft\Windows\CurrentVersion\CloudStore\Store\DefaultAccount\Current"
  $k = Get-ChildItem $b -Recurse | Where-Object { $_.PSChildName -eq 'windows.data.donotdisturb.quiethourssettings' }
  $d = (Get-ItemProperty $k.PSPath).Data
  if (-not $d) { return '?' }
  $s = -join ($d | ForEach-Object { if ($_ -ge 32 -and $_ -lt 127) { [char]$_ } else { '' } })
  if ($s -match 'Profile\.(\w+)') { return $Matches[1] } else { return $s }
}

function WaRows {
  $tmp = Join-Path $SP 'snap'
  Remove-Item "$tmp*" -Force
  Copy-Item "$ndir\wpndatabase.db"     "$tmp.db"     -Force
  Copy-Item "$ndir\wpndatabase.db-wal" "$tmp.db-wal" -Force
  Copy-Item "$ndir\wpndatabase.db-shm" "$tmp.db-shm" -Force
  $q = "select count(*) from Notification n join NotificationHandler h on n.HandlerId=h.RecordId where h.PrimaryId like '%WhatsApp%';"
  return (& sqlite3 "$tmp.db" $q)
}

function W($m) { $line = (Get-Date -f 'HH:mm:ss.fff') + '  ' + $m; Add-Content $log $line; Write-Output $line }

W ("MONITOR START  dnd=" + (Dnd) + "  quns=" + [Q]::State() + "  waRowsInActionCenter=" + (WaRows))
$lastAdd = (Get-ItemProperty $nkey).LastNotificationAddedTime
$lastQ   = [Q]::State()
$lastD   = Dnd
$lastIdm = (Get-ChildItem $idm).Count

while ($true) {
  Start-Sleep -Milliseconds 700
  $q = [Q]::State(); $d = Dnd
  if ($q -ne $lastQ) { W ("  state -> $q   fg: " + [Q]::Fg()); $lastQ = $q }
  if ($d -ne $lastD) { W ("  DND profile -> $d"); $lastD = $d }

  $add = (Get-ItemProperty $nkey).LastNotificationAddedTime
  if ($add -ne $lastAdd) {
    $lastAdd = $add
    $cnt = (Get-ItemProperty $nkey).PeriodicNotificationCount
    W ("*** WHATSAPP TOAST at " + [DateTime]::FromFileTime($add).ToString('HH:mm:ss') +
       "  quns=$q  dnd=$d  count=$cnt")
    W ("      foreground: " + [Q]::Fg())
    W ("      in action center immediately: " + (WaRows))
    Start-Sleep -Seconds 4
    $n2 = WaRows; $i2 = (Get-ChildItem $idm).Count
    W ("      after 4s: actionCenterRows=$n2  imgCacheFiles=$i2 (was $lastIdm)")
    if ($n2 -eq 0) { W ("      >>> toast NOT in action center 4s later = retracted or never persisted") }
    $lastIdm = $i2
  }
  $i = (Get-ChildItem $idm).Count
  if ($i -ne $lastIdm) { W ("  wpnidm image cache: $lastIdm -> $i"); $lastIdm = $i }
}
