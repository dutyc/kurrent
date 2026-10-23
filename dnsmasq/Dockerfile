# dnsmasq 来自 Alpine 官方仓库：发行版负责安全回补，版本更新走重建镜像。
# v3.24 main 为 2.92_p2（即上游 2.92rel2），编译选项含 DHCP/DHCPv6/TFTP、不含 DNSSEC。
FROM alpine:3.24@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a

RUN apk add --no-cache dnsmasq=2.92_p2-r0 \
    && mkdir -p /var/lib/misc /var/tftp

ENTRYPOINT ["/usr/sbin/dnsmasq"]
