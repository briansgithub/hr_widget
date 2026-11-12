[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$paths = @(
    "\Processor Information(_Total)\Processor Frequency",
    "\Processor Information(_Total)\% Processor Performance",
    "\Processor Information(_Total)\% Processor Utility",
    "\Processor Information(_Total)\% Performance Limit",
    "\Processor Information(_Total)\Performance Limit Flags"
)

# One sample is consumed while priming the stream, followed by two ramp samples
# and six recorded samples.
Get-Counter -Counter $paths -SampleInterval 1 -MaxSamples 9 | ForEach-Object {
    $values = [ordered]@{}
    foreach ($sample in $_.CounterSamples) {
        $path = $sample.Path.ToLowerInvariant()
        if ($path.EndsWith("\processor frequency")) {
            $values.frequency_mhz = [double]$sample.CookedValue
        } elseif ($path.EndsWith("\% processor performance")) {
            $values.processor_performance_percent = [double]$sample.CookedValue
        } elseif ($path.EndsWith("\% processor utility")) {
            $values.processor_utility_percent = [double]$sample.CookedValue
        } elseif ($path.EndsWith("\% performance limit")) {
            $values.performance_limit_percent = [double]$sample.CookedValue
        } elseif ($path.EndsWith("\performance limit flags")) {
            $values.performance_limit_flags = [double]$sample.CookedValue
        }
    }
    [pscustomobject]$values | ConvertTo-Json -Compress
}
