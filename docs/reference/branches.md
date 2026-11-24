# 仓库与分支

Kurrent 的代码分属两个仓库：主仓库 [kurrent](https://github.com/dutyc/kurrent) 承载控制面、存储节点、CLI、WebUI 与引导脚本；固件仓库 [kurrent-firmware](https://github.com/dutyc/kurrent-firmware) 构建设备固件。

默认部署使用两个仓库的 `main` 分支；无 TPM 设备使用两侧的 `legacy/no-tpm` 分支。其余分支为研究或归档线，不提供部署支持。

## 主仓库

| 分支 | 内容 | 状态 |
| --- | --- | --- |
| `main` | 部署线。设备身份为 TPM 2.0 AK，每次引导提交 PCR 0/2/4/7 quote 并与平台基线比对；数据面 NVMe/TCP + TLS 1.3，单端口 `4421` | 持续更新 |
| `legacy/no-tpm` | 无 TPM 版本线。设备身份为注册时登记的 ECDSA P-256 公钥，引导以一次性 nonce 验签；不含平台证明与 Secure Boot 签名首跳 | 可选部署线 |
| `iscsi-classic` | 2026-08 的 iSCSI 原型：iPXE + iSCSI 引导，控制面 `4838`、API `4839`，盘后端为 stgt/lio | 归档，仅作历史参考 |

## 固件仓库

| 分支 | 内容 | 状态 |
| --- | --- | --- |
| `main` | 部署线。提供 TPM 2.0 证明与 Secure Boot 签名首跳，`build-sb.sh` 产出 `dist/secureboot/snponly-sb.efi` | 持续更新 |
| `legacy/no-tpm` | 与主仓同名分支配套的固件：软件设备密钥与 TOFU，不签名 | 可选 |
| `research/nvme-of` | NVMe-oF 引导的早期研究 | 研究 |
| `legacy-iscsi` | iSCSI 时期的固件实现 | 归档 |

两侧同名分支配套使用：`main` 按 TPM AK 与 PCR 基线准入设备，固件侧缺少 AK、quote 或签名首跳时引导不会完成；`legacy/no-tpm` 以注册的设备公钥验签，不使用 TPM 与签名首跳。

信任链细节见[引导信任](../concepts/boot-chain/trust.md)与[安全模型](../concepts/architecture/security.md)。
