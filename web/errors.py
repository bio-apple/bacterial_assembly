"""Stable API error codes and the Chinese text a user can act on.

Technical detail stays in the job log and app.logger. These strings do not
include tracebacks.
"""

import errno
import signal

DISK_FULL = "磁盘不足。服务器存储空间不足，这次写入没有完成。"
UPLOAD_FAILED = "文件上传错误。文件没有写入服务器，请重新上传。"
FASTQ_INVALID = "FASTQ 格式错误。请上传成对、非空的 FASTQ 文件（.fastq、.fq、.fastq.gz 或 .fq.gz）。"
FASTQ_EMPTY = "FASTQ 格式错误。文件是空的，或没有成对的 FASTQ。"
FASTQ_RECORDS = "FASTQ 格式错误。记录无法通过检查，文件没有进入可分析状态。"
FASTQ_CHECK_UNAVAILABLE = "FASTQ 记录检查没有运行。服务器上没有可用的检查程序，文件没有进入可分析状态。"
PARAMETER_KMER = "参数校验错误。SPAdes k-mer 必须是不超过 127 的奇数，并用逗号分隔。"
PARAMETER_THREADS = "参数校验错误。线程数由系统自动分配（automatic），不能手动指定内存。"
TOOL_FAILED = "软件运行错误。分析软件异常退出。完整记录在任务日志中。"
TOOL_PARTIAL = "软件运行错误。部分方法失败，可下载已有结果（partial results）并查看任务日志。"
RESOURCE_FAILED = "资源不足。无法读取 CPU 或内存，或进程因系统资源被终止。完整记录在任务日志中。"
RESOURCE_START = "资源不足。无法读取 CPU 或内存，任务没有开始。"
REPORT_FAILED = "报告生成错误。组装已经完成，比较或 QUAST 报告没有生成。完整记录在任务日志中。"
INTERNAL = "服务器处理这次请求时出错。管理员可以查看服务器日志。"
WORKER_INTERRUPTED = "软件运行错误。工作进程在任务运行时停止，请重新提交。完整记录在任务日志中。"

RESOURCE_MARKERS = (
    "could not read physical system memory",
    "system memory is unavailable",
    "could not read memory",
    "could not read cpu",
    "invalid internal assembly thread allocation",
)


def parameter_message(detail: str) -> str:
    return "参数校验错误。{}".format(detail)


def explain_job_failure(returncode, error, log_text: str, stages, partial: bool) -> tuple[str, str]:
    """Classify a finished job. Resource and disk checks use only detectable signals."""
    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
        return "disk_full", DISK_FULL
    lowered = (log_text or "").lower()
    resource = returncode == -signal.SIGKILL or (
        isinstance(error, OSError) and error.errno == errno.ENOMEM
    )
    if not resource:
        resource = any(marker in lowered for marker in RESOURCE_MARKERS)
    if resource:
        return "resource_unavailable", RESOURCE_FAILED
    assembly_done = False
    report_failed = False
    for stage in stages or []:
        if not isinstance(stage, dict):
            continue
        kind = stage.get("kind")
        state = stage.get("state")
        if kind == "assembly" and state == "done":
            assembly_done = True
        if state == "failed" and (
            kind in ("quast", "comparison") or stage.get("id") == "comparison"
        ):
            report_failed = True
    if assembly_done and report_failed and not partial:
        return "report_failed", REPORT_FAILED
    if partial:
        return "tool_failed", TOOL_PARTIAL
    return "tool_failed", TOOL_FAILED
