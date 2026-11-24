BACTERIAL_ASSEMBLY_MODULE_ID = "bacterial-assembly"

# 分析类别。目录只渲染至少含一个已启用模块的类别。
ANALYSIS_CATEGORIES = (
    {
        "id": "genome-analysis",
        "name": "基因组分析",
    },
)

ANALYSIS_MODULES = (
    {
        "id": BACTERIAL_ASSEMBLY_MODULE_ID,
        "name": "细菌基因组组装",
        "code": "BA",
        "description": "Illumina 双端 reads 质控、组装与 QUAST 评估。",
        "purpose": "对一对 Illumina 双端 FASTQ 做质量过滤和短读长组装，并用 QUAST 评估每份组装结果。",
        # 本应用中该分析模块的版本，不是镜像里各工具的固定版本。
        "version": "1.0",
        "inputs": "Illumina 双端 FASTQ",
        "software": ("fastp", "SPAdes", "Unicycler", "Shovill/SKESA", "MEGAHIT", "QUAST"),
        "category": "genome-analysis",
        "endpoint": "dashboard",
    },
)


def usage_state_label(queued: int, running: int, cancelling: int = 0) -> str:
    parts = []
    if queued:
        parts.append("排队 {}".format(queued))
    if running:
        parts.append("分析中 {}".format(running))
    if cancelling:
        parts.append("取消中 {}".format(cancelling))
    return " · ".join(parts) if parts else "空闲"


def group_enabled_modules(modules, usage_by_module=None, categories=ANALYSIS_CATEGORIES):
    """Group enabled modules under their categories.

    A category with no enabled module is omitted. An enabled module whose
    category is not in the registry is still listed, without a category name.
    """
    usage_by_module = usage_by_module or {}
    names = {category["id"]: category["name"] for category in categories}
    grouped = {}
    seen = []
    for module in modules:
        category_id = module.get("category") or ""
        if category_id not in grouped:
            grouped[category_id] = {
                "id": category_id,
                "name": names.get(category_id, ""),
                "modules": [],
            }
            seen.append(category_id)
        usage = usage_by_module.get(module["id"], {})
        queued = int(usage.get("queued") or 0)
        running = int(usage.get("running") or 0)
        cancelling = int(usage.get("cancelling") or 0)
        card = dict(module)
        card["queued_count"] = queued
        card["running_count"] = running
        card["cancelling_count"] = cancelling
        card["state_label"] = usage_state_label(queued, running, cancelling)
        card["last_used_at"] = usage.get("last_used_at")
        card["enabled"] = True
        grouped[category_id]["modules"].append(card)

    ordered = []
    placed = set()
    for category in categories:
        bucket = grouped.get(category["id"])
        if bucket and bucket["modules"]:
            ordered.append(bucket)
            placed.add(category["id"])
    for category_id in seen:
        if category_id not in placed and grouped[category_id]["modules"]:
            ordered.append(grouped[category_id])
    return ordered
