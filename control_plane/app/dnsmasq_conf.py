"""dnsmasq.conf 派生生成：spec.networking 是权威，本模块渲染完整 dnsmasq.conf（幂等写）。

生成时机：控制面启动时（main.py 钩子）。yml 变更 → 重启控制面即覆盖 conf；
dnsmasq 容器挂载本文件（docker-compose.yml ./dnsmasq/dnsmasq.conf:/etc/dnsmasq.conf）。
dnsmasq.conf 只在进程启动时读取，改 networking 需重启 dnsmasq 容器；而 hosts 台账
（dhcp-hostsfile）由 dnsmasq 自己监听文件变化，控制面不需要 reload。
"""

import ipaddress
import logging
from pathlib import Path

log = logging.getLogger("control-plane")

# 固定部分模板（dnsmasq 容器视角路径）：TFTP/架构识别/引导文件分发/hosts 台账/日志
_TEMPLATE = """\
# ── 本文件由控制面启动时按 control_plane/kurrent.yaml spec.networking 生成，勿手工编辑 ──
# 权威来源：spec.networking.{{interface,subnet,dhcpRange,gateway,dns}}

# 监听指定网卡（host 网络下有效，来自 spec.networking.interface）
interface={interface}
bind-interfaces
dhcp-range=::,static

# DHCP 地址池（来自 spec.networking：subnet 推导掩码，dhcpRange 池起止，租期 12h 固定）
dhcp-range={dhcp_range},{netmask},12h
dhcp-option=3,{gateway}
dhcp-option=6,{dns}

# 启用 TFTP 服务
enable-tftp
tftp-root=/var/tftp

# 架构识别（PXE Client Architecture Option 93）
dhcp-match=set:bios,option:client-arch,0        # Legacy BIOS
dhcp-match=set:efi64,option:client-arch,7       # UEFI x64 (EFI BC)
dhcp-match=set:efi64,option:client-arch,9       # UEFI x64 (EFI x86_64)

# 引导文件分发（带标签的规则必须放在默认规则之前）
dhcp-boot=tag:efi64,snponly.efi                    # UEFI → snponly.efi
dhcp-boot=tag:bios,undionly.kpxe                # Legacy → undionly.kpxe

# 识别UEFI iPXE 二次请求，下发下一跳引导文件 boot.ipxe
dhcp-userclass=set:ipxe,iPXE
dhcp-boot=tag:ipxe,boot.ipxe

# 静态主机名分配（用于 iSCSI IQN 动态生成）
# 指定额外读取的主机配置文件
dhcp-hostsfile=/etc/dnsmasq.d/dhcp-hosts.conf
dhcp-leasefile=/var/lib/misc/dnsmasq.leases

# 日志（调试用）
log-dhcp
log-queries
"""


def render_dnsmasq_conf(interface: str, subnet: str, dhcp_range: str, gateway: str, dns: str) -> str:
    """按 networking 五键渲染完整 dnsmasq.conf；subnet（CIDR）推导 netmask。"""
    netmask = str(ipaddress.ip_network(subnet, strict=False).netmask)
    return _TEMPLATE.format(
        interface=interface, dhcp_range=dhcp_range, netmask=netmask, gateway=gateway, dns=dns,
    )


def ensure_dnsmasq_conf(path: str | Path) -> bool:
    """幂等生成：内容与现文件一致则不写；返回是否发生了写入。"""
    from .config import CONFIG

    net = CONFIG.spec.networking
    rendered = render_dnsmasq_conf(net.interface, net.subnet, net.dhcp_range, net.gateway, net.dns)
    conf_path = Path(path)
    if conf_path.is_file() and conf_path.read_text(encoding="utf-8") == rendered:
        return False
    conf_path.write_text(rendered, encoding="utf-8")
    log.info("dnsmasq: rendered %s from spec.networking", conf_path)
    return True
