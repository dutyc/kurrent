# 设备

设备代表一台物理机器。在 Kurrent 里，机器只是一个可替换的位置：系统盘与 Worker 身份在换机时保留，设备本身不承载系统。设备是绑定关系的权威侧——「哪台机器以谁的身份启动」这条事实记录在设备台账里。它以 MAC 为唯一标识，记录硬件指纹、TPM 身份与证明状态。

## 标识与记录

首次网络启动时，固件上报 11 项指纹（MAC、UUID、SMBIOS、CPU、内存、网卡等）与 TPM AK 公钥；控制面以 MAC 为唯一键登记。

| 字段 | 含义 |
| --- | --- |
| `mac` | 设备唯一标识；绑定关系的权威落点 |
| `uuid` / `fingerprint` | 申报性质的硬件信息，用于识别与审计 |
| `state` | `pooled` / `bound` / `revoked` |
| `bound_worker_id` | 绑定的 Worker；未绑定为空 |
| `identity` | TPM 2.0 AK 公钥、名称与算法；注册窗口内认领，之后不可在线更换 |
| `pcr_baseline` | PCR 0/2/4/7 的平台基线摘要与记录时间；首次证明通过时记录 |
| `attestation` | `state`（`unproven` / `proven` / `mismatch`）、上次证明时间与原因码 |
| `source` | `ipxe`（自动上报）或 `manual`（人工登记/导入） |

## 生命周期

```text
pooled ──绑定──► bound ──吊销──► revoked
  ▲                │
  └────解绑─────────┘
```

- 入池只表示「这台机器来过」，不产生引导权限；设备绑定到 Worker 后，才成为该 Worker 的引导节点。把发现与授权分开，未知机器就不会自动获得启动凭据。
- 自动入池只在注册窗口开启且上报带有效 AK 时发生；窗口关闭后，已有设备的上报仍更新指纹，未知设备不入池、不写台账。人工登记与吊销清理不受窗口限制。
- `revoked` 设备不再接受上报；dnsmasq 绑定文件会写入 `MAC,ignore`，被吊销设备拿不到租约。

## 相关页面

- 返回[实体模型](../entities.md)
- 绑定与换绑：[绑定与放置](./binding.md)
- 设备如何证明自己：[设备身份](../boot-chain/device-identity.md)