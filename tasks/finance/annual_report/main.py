"""ChiNext Annual Report Judge - End-to-End Verifiable.
"""

import json
import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import PureWindowsPath
from typing import Any, Dict, List

import pandas as pd
import cua_bench as cb
from tasks.common_config import GeneralTaskConfig

logger = logging.getLogger(__name__)


def win_join(*parts: str) -> str:
    return str(PureWindowsPath(*parts))


@dataclass
class TaskConfig(GeneralTaskConfig):
    TASK_TAG: str = "annual_report"
    TASK_CATEGORY: str = "finance"

    @property
    def file_list_url(self) -> str:
        return fr"{self.task_dir}\input\file_list.txt"

    @property
    def download_url(self) -> str:
        return fr"{self.remote_output_dir}\downloads"

    @property
    def task_description(self) -> str:
        return f"""
Goal: Download and extract ChiNext (创业板) company financial reports from East Money (东方财富网).

Tasks:
1. Download ALL ChiNext company annual reports and prospectuses for the past 6 years (2019-2024)
   - Target file list provided in: {self.file_list_url} (2993 files). Your output should match this list.
   - Save to: {self.download_url}

2. Parse PDFs to extract core technical personnel (核心技术人员) data:
   - Age (年龄, as of end of 2024)
   - Gender (性别)
   - Resume (简历)
   - Annual salary by year (年度薪酬): 2019-2024
   - Shareholding by year (持股数): 2019-2024
   - Nationality (国籍)
   - Highest education (最高学历)

3. Output: Single consolidated Excel file at {self.remote_output_dir}\final_dataset.xlsx
   - Single sheet with all data merged
   - Required columns: 识别码, 证券代码, 股票简称, 姓名, 性别, 年龄, 国籍, 最高学历, 简历,
                      2019薪酬, 2020薪酬, 2021薪酬, 2022薪酬, 2023薪酬, 2024薪酬,
                      2019持股, 2020持股, 2021持股, 2022持股, 2023持股, 2024持股
   - One row per person
   - Use NaN for missing values (do not use 0 or '/')
   
   **识别码 (Identifier) Format**: Combine company name and person name
   - Example: "华兴源创曹振军" (company: 华兴源创, person: 曹振军)
   - Use 股票简称 (not 证券代码) for company part
   - No separator between company and person name
   
   **最高学历 (Highest Education) Format**: 博士=1，硕士=2，本科=3，大专=4，其他=5，没披露=NaN

Verification: 
The task is considered successful if:
- All the requested annual report files are correctly stored (judged by MD5).
- The datacells in the final table are all accurate.
"""

    def to_metadata(self) -> Dict:
        md = super().to_metadata()
        md.update({"file_list_url": self.file_list_url, "download_url": self.download_url})
        return md


config = TaskConfig()


@cb.tasks_config(split="train")
def load():
    return [
        cb.Task(
            description=config.task_description,
            metadata=config.to_metadata(),
            computer={"provider": "computer", "setup_config": {"os_type": config.OS_TYPE}},
        )
    ]


@cb.setup_task(split="train")
async def start(task_cfg, session: cb.DesktopSession):
    logger.info("Setting up task: annual_report")
    outdir = task_cfg.metadata["remote_output_dir"]
    try:
        await session.remove_file(outdir)
        await session.makedirs(outdir)
        await session.makedirs(win_join(outdir, "downloads"))
    except Exception as e:
        logger.warning(f"Failed to setup tasks {config.TASK_TAG} via session: {e}")


async def _read_json_remote(session: cb.DesktopSession, path: str) -> Any:
    return json.loads(await session.read_file(path))


def _last_json(stdout: str) -> Dict[str, Any]:
    for ln in reversed([x.strip() for x in str(stdout).splitlines() if x.strip()]):
        try:
            return json.loads(ln)
        except Exception:
            continue
    return {}


async def verify_files_remote(session: cb.DesktopSession, output_dir: str, reference_dir: str) -> float:
    """50 points max. -1 per wrong/missing file. MD5 computed on remote (parallel)."""
    manifest = win_join(reference_dir, "file_manifest.json")
    downloads = win_join(output_dir, "downloads")
    ps1_path = win_join(reference_dir, "_verify_files_md5.ps1")

    # Runspace parallel hashing, compatible with Windows PowerShell 5.1
    ps1 = r"""param(
  [Parameter(Mandatory=$true)][string]$ManifestPath,
  [Parameter(Mandatory=$true)][string]$DownloadsDir
)

$max = 20
$throttle = [Math]::Min(8, [Environment]::ProcessorCount)
if ($throttle -lt 1) { $throttle = 1 }

$sw = [System.Diagnostics.Stopwatch]::StartNew()

# Force UTF-8 decoding for manifest JSON (critical for Chinese filenames)
$m = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json

$total = ($m.PSObject.Properties | Measure-Object).Count
$correct = 0

$missing = 0; $size_bad = 0; $hash_bad = 0; $err = 0
$missing_ex = @(); $size_ex = @(); $hash_ex = @(); $err_ex = @()

# Build hash todo list only for existing + size OK files
$todo = New-Object System.Collections.Generic.List[object]

foreach ($p in $m.PSObject.Properties) {
  $fn = $p.Name
  $exp = $p.Value
  $fp = Join-Path $DownloadsDir $fn

  if (!(Test-Path -LiteralPath $fp)) {
    $missing++
    if ($missing_ex.Count -lt $max) { $missing_ex += $fn }
    continue
  }

  try {
    $size = (Get-Item -LiteralPath $fp).Length
    $emin = [double]$exp.size * 0.95
    $emax = [double]$exp.size * 1.05
    if ($size -lt $emin -or $size -gt $emax) {
      $size_bad++
      if ($size_ex.Count -lt $max) { $size_ex += $fn }
      continue
    }

    $todo.Add([pscustomobject]@{ fn=$fn; fp=$fp; exp_hash=($exp.hash.ToLower()) })
  } catch {
    $err++
    if ($err_ex.Count -lt $max) { $err_ex += $fn }
    continue
  }
}

# Parallel hash using runspace pool
$pool = [RunspaceFactory]::CreateRunspacePool(1, $throttle)
$pool.Open()

$tasks = New-Object System.Collections.Generic.List[object]

$scriptBlock = {
  param($path)
  try {
    (Get-FileHash -LiteralPath $path -Algorithm MD5).Hash.ToLower()
  } catch {
    ""
  }
}

foreach ($item in $todo) {
  $ps = [PowerShell]::Create()
  $ps.RunspacePool = $pool
  [void]$ps.AddScript($scriptBlock).AddArgument($item.fp)
  $handle = $ps.BeginInvoke()
  $tasks.Add([pscustomobject]@{
    ps=$ps; handle=$handle; fn=$item.fn; exp_hash=$item.exp_hash
  })
}

foreach ($t in $tasks) {
  try {
    $res = $t.ps.EndInvoke($t.handle)
    $t.ps.Dispose()
    $hash = ""
    if ($res -is [System.Array] -and $res.Length -gt 0) { $hash = [string]$res[0] }
    elseif ($res) { $hash = [string]$res }

    if ([string]::IsNullOrEmpty($hash)) {
      $err++
      if ($err_ex.Count -lt $max) { $err_ex += $t.fn }
      continue
    }

    if ($hash -eq $t.exp_hash) {
      $correct++
    } else {
      $hash_bad++
      if ($hash_ex.Count -lt $max) { $hash_ex += $t.fn }
    }
  } catch {
    $err++
    if ($err_ex.Count -lt $max) { $err_ex += $t.fn }
    continue
  }
}

$pool.Close()
$pool.Dispose()

$sw.Stop()

@{
  total=$total; correct=$correct; throttle=$throttle; hashed_count=$todo.Count; elapsed_sec=[Math]::Round($sw.Elapsed.TotalSeconds, 3);
  missing_count=$missing; size_mismatch_count=$size_bad; hash_mismatch_count=$hash_bad; error_count=$err;
  missing_examples=$missing_ex; size_mismatch_examples=$size_ex; hash_mismatch_examples=$hash_ex; error_examples=$err_ex
} | ConvertTo-Json -Compress
"""

    try:
        await session.write_file(ps1_path, ps1)
        cmd = (
            f'powershell -NoProfile -ExecutionPolicy Bypass -File "{ps1_path}" '
            f'-ManifestPath "{manifest}" -DownloadsDir "{downloads}"'
        )
        r = await session.run_command(cmd, check=False)
        stats = _last_json(r.get("stdout", ""))
        if not stats:
            logger.warning("File MD5 check produced no parsable JSON.")
            logger.info("Raw stdout (first 500 chars): %s", str(r.get("stdout", ""))[:500])
            logger.info("Raw stderr (first 500 chars): %s", str(r.get("stderr", ""))[:500])
            return 0.0

        total = int(stats.get("total", 0))
        correct = int(stats.get("correct", 0))
        wrong = max(0, total - correct)
        score = float(max(0, 50 - wrong))

        logger.info(
            "File check: total=%s correct=%s wrong=%s score=%.2f/50 (hashed=%s throttle=%s elapsed_sec=%s)",
            total, correct, wrong, score,
            stats.get("hashed_count", None),
            stats.get("throttle", None),
            stats.get("elapsed_sec", None),
        )
        logger.info(
            "File failures: missing=%s size=%s hash=%s error=%s",
            stats.get("missing_count", 0),
            stats.get("size_mismatch_count", 0),
            stats.get("hash_mismatch_count", 0),
            stats.get("error_count", 0),
        )
        for k, label in [
            ("missing_examples", "Missing examples"),
            ("size_mismatch_examples", "Size mismatch examples"),
            ("hash_mismatch_examples", "Hash mismatch examples"),
            ("error_examples", "Error examples"),
        ]:
            ex = stats.get(k, [])
            if isinstance(ex, list) and ex:
                logger.warning("%s (up to 20): %s", label, ", ".join(map(str, ex)))

        return score
    except Exception as e:
        logger.warning(f"MD5 verification failed: {e}")
        return 0.0


async def verify_data_remote(session: cb.DesktopSession, output_dir: str, reference_dir: str) -> float:
    """50 points max. -1 per wrong sample."""
    samples_path = win_join(reference_dir, "data_samples.json")
    table_path = win_join(output_dir, "final_dataset.xlsx")

    try:
        samples = await _read_json_remote(session, samples_path)
    except Exception as e:
        logger.warning(f"Failed to load data_samples.json: {e}")
        return 0.0

    if not await session.exists(table_path):
        logger.warning("final_dataset.xlsx not found: %s", table_path)
        return 0.0

    try:
        df = pd.read_excel(BytesIO(await session.read_bytes(table_path)))
    except Exception as e:
        logger.warning(f"Failed to read final_dataset.xlsx: {e}")
        return 0.0

    if "识别码" not in df.columns:
        logger.warning("Missing required column: 识别码")
        return 0.0

    try:
        df_idx = df.set_index("识别码", drop=False, verify_integrity=True)
    except Exception as e:
        logger.warning(f"Failed to set '识别码' as a unique index: {e}. This may indicate duplicate entries and will cause slow lookups.")
        df_idx = df

    correct = 0
    total = len(samples)
    mismatches: List[Dict[str, Any]] = []

    for s in samples:
        try:
            row_id = s["row_id"]
            col = s["column"]
            exp = s["value"]
            mtype = s.get("match_type", "exact")

            row = df_idx.loc[row_id]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            act = row[col] if col in row.index else None

            ok = False
            if pd.isna(act) and pd.isna(exp):
                ok = True
            elif pd.isna(act) or pd.isna(exp):
                ok = False
            else:
                if mtype == "contains":
                    ok = str(exp).replace(" ", "") in str(act).replace(" ", "")
                else:
                    try:
                        ok = abs(float(act) - float(exp)) < 0.01
                    except Exception:
                        ok = str(act).strip() == str(exp).strip()

            if ok:
                correct += 1
            elif len(mismatches) < 20:
                mismatches.append({"row_id": row_id, "column": col, "match_type": mtype, "expected": exp, "actual": act})
        except KeyError as e:
            if len(mismatches) < 20:
                mismatches.append({"error": f"Lookup failed for key: {e}", "sample": s})
        except Exception as e:
            if len(mismatches) < 20:
                mismatches.append({"error": str(e), "sample": s})

    wrong = total - correct
    score = float(max(0, 50 - wrong))
    logger.info("Data check: total=%s correct=%s wrong=%s score=%.2f/50", total, correct, wrong, score)
    if mismatches:
        logger.warning("Data mismatch examples (up to 20):")
        for m in mismatches:
            logger.warning("%s", m)
    return score


@cb.evaluate_task(split="train")
async def evaluate(task_cfg, session: cb.DesktopSession) -> list:
    outdir = task_cfg.metadata["remote_output_dir"]
    refdir = task_cfg.metadata["reference_dir"]
    try:
        file_score = await verify_files_remote(session, outdir, refdir)
        data_score = await verify_data_remote(session, outdir, refdir)
        final = (file_score + data_score) / 100.0
        logger.info("Final: %.4f (file=%.2f/50, data=%.2f/50)", final, file_score, data_score)
        return [final]
    except Exception as e:
        logger.warning(f"Evaluation error: {e}")
        return [0.0]
