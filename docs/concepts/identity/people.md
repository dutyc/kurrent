# 人员与证书

管理面只经 HTTPS `443` 进入。人类用户与 admin 使用 users CA 签发的**客户端证书**，控制面只接触 CSR（证书签名请求），不接触私钥。

## 唯一入口

控制面服务本身不监听 TCP，只监听容器内 unix socket；nginx 是唯一对端。请求先由 nginx 完成证书校验，再把身份注入 `X-Client-Cert-*` 与 `X-Client-Class` 头转发给控制面。这些头只信 nginx 注入，其他反代位置默认清空，防止有人绕过 nginx 伪造身份头。

## 三类 CA

管理面的证书按用途分成三个根 CA，各自签发各自的对象：

| CA | 签发对象 | 用途 |
| --- | --- | --- |
| users | 人类用户与 admin | 管理面 mTLS；nginx 判定为 `human` |
| nodes | agent / nvmet-host | 组件 enroll / renew、组件间 mTLS；判定为 `component` |
| control | 控制面自身客户端 | 控制面出站到组件；不进 443 |

分根的意义在于隔离影响面：吊销或泄露某一类证书，不会波及其他类。nginx 只信任 users + nodes 的信任包，按证书 issuer 判定主体类。控制面每个路由声明接受的主体类，**默认拒绝**：节点证书不能访问管理面，人类证书不能调用组件续期端点，control 域证书不进入 443。

## 人员证书

管理员或用户先在本机生成私钥与 CSR：

```bash
kurrent cert keygen alice --out ./certs
kurrent cert issue alice --csr ./certs/alice.csr
```

控制面只接收 CSR，不接触私钥。签发前要求该 CN 已在 `state/users.yml` 登记；组也来自该文件。

吊销可以精确到一张证书（序列号）或该用户的全部证书；重签 CRL（证书吊销列表）后 nginx reload，下一次 TLS 握手即拒绝。吊销同时删除该用户的相关会话。

## 授权

- 读请求只需已认证。
- 写请求需要 `admin` 组。
- `enroll` scope 只放行签发 bootstrap token。
- 会话写请求额外做同源校验。

## 接下来

- 返回[身份与访问](../identity.md)
- token 与会话：[管理 token 与会话](./tokens.md)
- 组与用户的声明来源：[声明式配置](../configuration.md)