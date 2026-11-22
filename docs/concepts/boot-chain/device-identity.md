# 设备身份

**设备身份**回答请求方是不是那台设备，并给出可复核的结论：这台机器在台账中是哪一条，它的启动环境是否仍与基线一致。首跳只确认了代码可信，还不知道请求方是谁。信任根放在硬件里：换一块系统盘不会改变设备身份，系统在不同机器之间流动，身份始终锚定在物理机器上。

## 信任根：TPM AK

首次启动时，固件在本机 TPM 内创建证明密钥（AK，一种受限签名密钥，ECC P-256 或 RSA-2048），私钥不可导出。「受限」意味着这把密钥只能对 TPM 内部产生的数据签名，不能被当作普通签名密钥滥用；「不可导出」意味着它只能代表这台物理机器。后续启动复用同一 AK，已存在的 AK 不会被重建。控制面只记录 AK 公钥（SPKI DER 编码）、AK name 与密钥类型，不持有私钥。

AK 不可在线轮换：设备恢复或更换主板时，走删除登记、在注册窗口内重新入池的管理员流程。

## 登记与入池

固件向 `/devices/report` 上报 MAC、UUID、SMBIOS、CPU、内存、网卡等 11 项指纹，以及 AK 公钥、AK name、类型与 Secure Boot 状态。

控制面按 MAC 登记，但**登记不产生权限**：发现一台机器不等于授权它启动：

- 未知设备只有在注册窗口开启、放行额度未用尽、携带有效 AK 且 `sb=1`（Secure Boot 已开启）时才入池；
- 窗口关闭后，已有设备的上报仍更新指纹；未知设备不入池、不写台账；
- 无 TPM 或 Secure Boot 未开启的设备不入池；
- 已入池但未绑定的设备不会获得任何启动凭据。

## 挑战-证明

登记之后，每次启动都要重新证明。设备先向 `/devices/challenge` 请求一个一次性 nonce（随机数）；设备不存在、未认领或已吊销时返回 404。随后固件用 AK 对 PCR 0/2/4/7 做 SHA-256 quote：

```text
extraData = nonce    （原样写入，不做编码变换）
qualifiedSigner = AK
pcrDigest = SHA-256( PCR 0 / 2 / 4 / 7 )
```

nonce 的作用是防重放：它只用一次，旧的 quote 无法拿来冒充新的启动。PCR 读取与 quote 在同一次 TPM 调用内完成，避免两次读取之间启动环境发生变化造成的竞态。quote 与签名随 `/boot-vars` 提交，控制面依次校验结构、AK 签名、nonce 一次性与绑定、PCR 摘要。`report`、`challenge` 与 `boot-vars` 按来源 IP 限速。

## 平台基线

平台基线在首次证明通过时记录。之后 PCR 摘要一旦变化，控制面按 `pcr_mismatch` 拒绝，并保留待批摘要；管理员核对变更确属预期后，通过 `POST /devices/{mac}/approve-baseline` 批准新基线。

首次记录的基线可信度取决于当时的装机环境：如果首次证明发生在攻击者控制下，基线会被污染。因此首次登记应核对装机环境。

## 原因码

| 原因码 | 含义 | 出现位置 |
| --- | --- | --- |
| `no_tpm` | 上报没有有效 AK | 登记 |
| `sb_disabled` | Secure Boot 未开启 | 登记 |
| `window_closed` | 窗口关闭或额度用尽 | 登记 |
| `ak_mismatch` | 已知设备换了一把 AK | 认领 |
| `ak_unregistered` | 台账无 AK，无从验签 | `/boot-vars` |
| `quote_invalid` | quote 缺失、格式或签名错误 | `/boot-vars` |
| `nonce_invalid` | nonce 过期、已用或绑定错误 | `/boot-vars` |
| `pcr_mismatch` | PCR 摘要与基线不符 | `/boot-vars`（留待批） |
| `missing_mac` | 强制模式下未携带 MAC | `/boot-vars` |

设备侧看不到原因码：被拒绝时 `/boot-vars` 返回空脚本，设备没有可用变量。原因码写入审计 `state/audit.jsonl`，并记录为设备的 `attestation.last_reason`（由 `GET /devices` 返回，WebUI 设备详情显示证明状态与基线）。排障入口在管理员一侧，不向设备泄露判断细节。

## 边界

证明只能说明「请求方持有该设备的 AK，并能对指定 PCR 生成有效 quote」，不证明启动后的系统未被控制；运行期被控的设备仍可在轮换窗口内取得当前 PSK。PCR 选择与 AK 创建也需逐型号验证，AMD fTPM 与独立 TPM 的行为不同。展开见[安全模型](../../concepts/architecture/security.md)。

## 接下来

- 返回[引导链](../boot-chain.md)
- 首跳如何建立信任：[首跳](./first-hop.md)
- 证明通过之后：[数据面](./data-plane.md)
- AK、quote 格式与信任锚：[固件契约](../../reference/firmware-contract.md)