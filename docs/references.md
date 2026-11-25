# 引用与软件文档

流程调用以下工具时，参数约定依据各项目文档：

- [Unicycler Illumina-only 用法](https://github.com/rrwick/Unicycler#quick-usage)：只用 `-1`/`-2`；`-l` 是长读长输入。
- [Shovill 参数](https://github.com/tseemann/shovill#advanced-options)：双端 `--R1`/`--R2`、资源 `--cpus`/`--ram`、`--assembler skesa`。
- [MEGAHIT 用法](https://github.com/voutcn/megahit#usage)：双端 `-1`/`-2`，输出目录 `-o`。
- [QUAST 用法](https://github.com/ablab/quast#usage)：无参考评估；`report.tsv` 为可解析指标表。

镜像中的版本见 [installation.md](installation.md)。领域术语见 [CONTEXT.md](../CONTEXT.md)。架构决策记录在 [adr/](adr/)。
