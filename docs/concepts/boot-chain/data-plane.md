# 数据面

**数据面**回答连接哪个 target、使用什么凭据。身份阶段确认请求方是那台设备之后，控制面把当前绑定的模型投影成一组启动变量，固件按变量建立一条加密连接。此后块读写直接发生在设备与存储节点之间，不经过控制面。

## 准入

`/boot-vars` 是引导链上最关键的一次判断：凭据从这里发出去。它按顺序检查四件事：

1. **识别**：请求携带的 MAC 经设备台账反查绑定的 Worker；未带 MAC 或台账未命中时回退到 hostname；再不行才查 dnsmasq 投影（仅存量回退）。
2. **绑定即认证**：带 MAC 的请求必须来自该 Worker 的绑定设备。绑定关系本身就是认证依据，其他设备不能冒领该 Worker 的引导身份。
3. **TPM 证明**：身份阶段未通过的请求在此被拒；凭据只发给证明通过的设备。
4. **凭据条件**：Worker 已绑定、密钥库有条目；nvmet 后端还要求存储节点数据面端口就绪。

## 变量投影

通过检查后，控制面把模型投影成启动变量：

| 变量 | 内容 |
| --- | --- |
| `disk-nqn` | 默认盘的完整 NQN |
| `storager-ip` | 持有该盘的存储节点地址 |
| `hostnqn` | 该 Worker 的发起端 Host NQN |
| `nbft-secret` | DH-HMAC-CHAP 认证密钥 |
| `nbft-tls-psk` / `nbft-tls-port` | 由认证密钥派生的 TLS PSK 与数据面端口 |
| `menu-default` | 菜单超时后的启动项 |

非凭据变量只在显式关闭证明强制时下发；非 nvmet 后端不投影 NVMe-oF 密钥与 PSK。所有路径都不拼接明文 URI。

## 菜单

`menu.ipxe` 经 HTTPS 加载，显示当前默认盘的 OS 标识：

- 超时后启动默认盘；
- 要切换系统盘，在 WebUI 的 Worker 详情中设置另一块盘为默认盘，下一次启动生效；
- 启动项来自台账投影，不依赖盘内配置。

## 连接

`sanboot` 的目标形如：

```text
nvme://<storager>:<tls_port>/<disk_nqn>?secret=<dhhc1>&tls_psk=<psk>&tls_trsvcid=<tls_port>
```

连接分两层，各管一件事：DH-HMAC-CHAP 回答「谁可以连」，TLS 1.3 保护传输内容。两层共用同一端口 `4421`，固件自己完成 TLS 握手，没有明文回退端口。

## 失败

- 无法识别 Worker（MAC 未绑定、hostname 未命中、dnsmasq 投影无记录）→ 空脚本。
- 池中未绑定设备 → `menu-default=reboot`，短超时重启等待绑定；Worker 没有默认盘时同样返回 `reboot`。
- nvmet 后端缺认证密钥或端口未就绪 → 拒绝下发，原因码 `no_credential` / `port_not_ready`。
- 显式关闭证明强制时，nvmet 后端仍按 `unverified` 拒绝注入凭据。
- 设备侧握手失败 → PSK 未送达存储节点 keyring、`tlshd` 低于 1.5.0，或内核缺少 NVMe/TCP TLS 配置。

## 接下来

- 返回[引导链](../boot-chain.md)
- 证明结果从哪来：[设备身份](./device-identity.md)
- 盘、PSK 与 keyring：[存储与数据面](../storage.md)
- 连接交给内核之后：[交接给内核](./handover.md)