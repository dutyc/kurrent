# 母盘

**母盘**是系统盘的克隆源，本身不参与启动。母盘与系统盘分开：母盘定制一次，系统盘按需克隆，装机与交付解耦。母盘放在存储节点声明 `spec.agent.diskDir` 下，文件名包含 `_tpl_` 标记（大小写不敏感）：

```text
/pool1/disk_dir/_tpl_ubuntu2604.img
```

后台扫描周期缓存母盘清单，包含文件名、大小与 mtime、GPT 扇区布局（`4k` / `512` / `none`）与人工登记标签（`os`、`os_version`、备注）。标签只是展示与选择元数据，不参与身份或数据面运算。

## 要求

- **4K 原生 GPT**。nvmet file backend 按底层文件系统块（典型 4096 B）导出，512 字节扇区 GPT 在固件侧会被视为无分区。Agent 拒绝克隆，nvmet-host 拒绝导出，引导会报 `Boot from SAN device ... failed`。
- **reflink 文件系统**。reflink 是文件系统级的写时复制克隆：克隆瞬间完成，只有后续写入才分配新块。btrfs / xfs 上克隆接近秒级；ext4 不支持 reflink，会退化为全量复制。

## 克隆系统盘

建盘时控制面要求母盘已存在于目标 Agent 本地，并按放置规则选择节点；克隆由 Agent 执行，控制面不搬运数据。放置规则见[绑定与放置](../entities/binding.md)。

空白盘是另一种来源：不依赖母盘，用于安装系统。

## 接下来

- 返回[存储与数据面](../storage.md)
- 系统盘标识与默认盘：[系统盘](../entities/system-disk.md)
- 引导报错的排查：[故障排查](../../tasks/troubleshooting.md)