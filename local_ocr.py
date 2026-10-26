"""Read local image text with Windows' installed OCR; no network or packages.

Only temporary job files are exchanged with Windows PowerShell. Recognized
text stays in memory and is never included in exported reports.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


SCRIPT = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
    $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await-Result($operation, [Type]$resultType) {
    $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
    $task.GetAwaiter().GetResult()
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { throw 'No installed local OCR language' }
$job = Get-Content -LiteralPath $env:COURSE_OCR_JOB -Raw -Encoding UTF8 | ConvertFrom-Json
$results = @()
foreach ($path in $job.paths) {
    $stream = $null; $bitmap = $null
    try {
        $file = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
        $stream = Await-Result ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync([Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8, [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied)) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $results += @{text=$result.Text; ok=$true}
    } catch {
        $results += @{text=''; ok=$false}
    } finally {
        if ($null -ne $bitmap) { $bitmap.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
}
$json = ConvertTo-Json -InputObject @($results) -Compress
[System.IO.File]::WriteAllText($job.output, $json, [System.Text.UTF8Encoding]::new($false))
'''


def read_image_text(paths):
    """Return one text (or None for an unreadable image) per supplied path.

    Failures never mean that an image is a default page. No OCR installation,
    language download, account access or cloud OCR is attempted.
    """
    paths = [str(Path(path).resolve()) for path in paths]
    if not paths:
        return []
    executable = shutil.which('powershell.exe') if os.name == 'nt' else None
    if executable is None:
        raise OSError('Windows 本机文字识别不可用。')
    encoded = base64.b64encode(SCRIPT.encode('utf-16-le')).decode('ascii')
    results = []
    # Bound each subprocess; batch setup once for many representatives.
    with tempfile.TemporaryDirectory(prefix='course-local-ocr-') as temporary:
        folder = Path(temporary)
        for offset in range(0, len(paths), 24):
            batch = paths[offset:offset+24]
            output = folder/'result.json'
            job = folder/'job.json'
            job.write_text(json.dumps({'paths': batch, 'output': str(output)}), encoding='utf-8')
            environment = dict(os.environ, COURSE_OCR_JOB=str(job))
            try:
                process = subprocess.run([executable, '-NoProfile', '-NonInteractive',
                    '-EncodedCommand', encoded], env=environment,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=30+len(batch)*5,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if process.returncode:
                    raise OSError('Windows 本机文字识别启动失败。')
                values = json.loads(output.read_text(encoding='utf-8'))
                if not isinstance(values, list) or len(values) != len(batch):
                    raise ValueError('Invalid OCR result count')
                for value in values:
                    if not isinstance(value, dict) or not isinstance(value.get('text'), str):
                        raise ValueError('Invalid OCR result')
                    results.append(value['text'] if value.get('ok') else None)
            except (subprocess.TimeoutExpired, ValueError) as error:
                raise OSError('Windows 本机文字识别未返回完整结果。') from error
    return results
