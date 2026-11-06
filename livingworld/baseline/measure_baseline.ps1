<#
.SYNOPSIS
  Samples system load while you play Skyrim VR with Mantella, for the Milestone 0 baseline.

.DESCRIPTION
  Every few seconds it records total and free RAM, CPU load, GPU load and VRAM (via nvidia-smi),
  and the memory and CPU time of SkyrimVR.exe and Mantella (Mantella.exe or python.exe).
  Results go to a CSV file. Stop it with Ctrl+C.

  Frame rate is not measured here. Use fpsVR or SteamVR's frame timing panel and note the numbers
  in the checklist (livingworld/baseline/BASELINE.md).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\livingworld\baseline\measure_baseline.ps1 -Label "vanilla-mantella"
#>
param(
    [string]$Label = "baseline",
    [int]$IntervalSeconds = 5,
    [string]$OutputFolder = "$env:USERPROFILE\Documents\My Games\Mantella\data\baseline"
)

New-Item -ItemType Directory -Force -Path $OutputFolder | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$outFile = Join-Path $OutputFolder "baseline_${Label}_${stamp}.csv"
$hasNvidiaSmi = $null -ne (Get-Command nvidia-smi -ErrorAction SilentlyContinue)
$totalRamMB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1MB)

"timestamp,label,total_ram_mb,free_ram_mb,cpu_pct,gpu_pct,vram_used_mb,vram_total_mb,skyrim_ram_mb,skyrim_cpu_s,mantella_ram_mb,mantella_cpu_s" | Out-File -FilePath $outFile -Encoding utf8
Write-Host "Writing to $outFile (Ctrl+C to stop)"
if (-not $hasNvidiaSmi) { Write-Warning "nvidia-smi not found: GPU columns will be empty." }

function Get-ProcStats([string[]]$names) {
    $procs = Get-Process -Name $names -ErrorAction SilentlyContinue
    if (-not $procs) { return @("", "") }
    $ram = [math]::Round(($procs | Measure-Object WorkingSet64 -Sum).Sum / 1MB)
    $cpu = [math]::Round(($procs | Measure-Object CPU -Sum).Sum, 1)
    return @($ram, $cpu)
}

while ($true) {
    $os = Get-CimInstance Win32_OperatingSystem
    $freeRamMB = [math]::Round($os.FreePhysicalMemory / 1KB)
    $cpuPct = [math]::Round((Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average)
    $gpuPct = ""; $vramUsed = ""; $vramTotal = ""
    if ($hasNvidiaSmi) {
        $gpu = (nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits) -split ","
        $gpuPct = $gpu[0].Trim(); $vramUsed = $gpu[1].Trim(); $vramTotal = $gpu[2].Trim()
    }
    $skyrim = Get-ProcStats @("SkyrimVR")
    $mantella = Get-ProcStats @("Mantella", "python")
    $line = "{0},{1},{2},{3},{4},{5},{6},{7},{8},{9},{10},{11}" -f (Get-Date -Format "s"), $Label, $totalRamMB, $freeRamMB, $cpuPct, $gpuPct, $vramUsed, $vramTotal, $skyrim[0], $skyrim[1], $mantella[0], $mantella[1]
    $line | Out-File -FilePath $outFile -Append -Encoding utf8
    Start-Sleep -Seconds $IntervalSeconds
}
