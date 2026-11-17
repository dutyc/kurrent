# 控制面声明

控制面声明（`ControlPlaneConfiguration`）定义控制面的网络、PKI、服务器证书、引导策略、认证策略、注册窗口与凭据轮换。它是这些参数的唯一输入：dnsmasq 配置与 PKI 都在启动时按它派生，而不是各自维护一份配置。

```yaml
apiVersion: kurrent.io/v1
kind: ControlPlaneConfiguration
metadata:
  name: kurrent-cp
spec:
  networking:        # 必填：五项部署事实
    interface: enp3s0
    subnet: 192.168.80.0/24
    dhcpRange: 192.168.80.50,192.168.80.100
    gateway: 192.168.80.2
    dns: 223.5.5.5
  pki: {}            # bootstrap token、组件证书、renew 阈值、CRL 周期
  serverCert: {}     # 服务器证书 SAN/天数/是否随声明轮换
  boot: {}           # 架构、菜单超时、自动启动超时
  auth: {}           # 会话/证书/token 策略
  registration: {}   # 注册窗口 TTL 与放行额度上限
  credentials: {}    # DHHC-1 轮换与推送对账周期
  agentTimeoutSec: 10
```

除 `spec.networking` 与 `metadata.name` 外，其余块都有默认值。完整键表见[配置参考](../../reference/configuration.md)。

## 网络声明

`spec.networking` 是 dnsmasq 配置的唯一权威。控制面把声明里的五项网络事实翻译成 dnsmasq 的对应配置：

- `interface` 写入 `interface=`；
- `subnet` 推导 netmask；
- `dhcpRange` 写入地址池；
- `gateway` / `dns` 写入 DHCP option 3/6。

控制面启动时按声明生成 `<state>/control-plane/dnsmasq/dnsmasq.conf`。**生成文件不得手工编辑**，否则下次启动时将被覆盖。`kurrent config status` 会报告生成文件是否与声明一致。

## 策略块

| 块 | 约束的运行时行为 |
| --- | --- |
| `pki` | bootstrap token 一次性与 TTL、组件证书有效期、renew 阈值、CRL 重签周期 |
| `serverCert` | 管理面服务器证书的 SAN 与天数；默认只在变更时报告漂移 |
| `boot` | 引导架构、菜单超时与自动启动超时 |
| `auth` | 会话、人员证书与管理 token 的有效期上限 |
| `registration` | 注册窗口 TTL 与放行额度上限；不自动开窗 |
| `credentials` | DHHC-1 定期轮换与推送对账周期 |

## 相关页面

- 返回[声明式配置](../configuration.md)
- 节点侧声明：[节点声明](./node.md)
- 校验与生效：[校验与生效](./validation.md)