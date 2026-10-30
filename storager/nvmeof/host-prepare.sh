#!/usr/bin/env bash
# kurrent 存储节点宿主前置：nvmet 数据面（内核模块加载 + configfs 挂载 + tlshd + 重启持久化）。
#
# 内核 nvmet target 无法容器化：4421 数据面由宿主内核提供，节点部署前须加载
# nvmet / nvmet-tcp 并挂载 configfs，且跨重启持久化（否则重启后节点静默失效）；
# TLS 1.3 的内核握手 upcall 由宿主 tlshd（ktls-utils >= 1.5.0）承接。
# 幂等，可重复执行；就位操作需 root（tlshd 只检查，不代装发行版包）。
#
# 用法：
#   sudo bash host-prepare.sh     # 就位（modprobe + mount + /etc/modules-load.d + /etc/fstab）
#   bash host-prepare.sh --check  # 只读诊断、不做修改（无需 root；部署前排查用）
set -u

TARGET=/sys/kernel/config
MODULES="nvmet nvmet-tcp"
CONF=/etc/modules-load.d/kurrent-nvmet.conf
TLSHD_MIN=1.5.0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CHECK=false
[ "${1:-}" = "--check" ] && CHECK=true
failed=0
tlshd_bad=false

ok()   { printf '  ok   %s\n' "$*"; }
todo() { printf '  todo %s\n' "$*"; }
fail() { printf '  FAIL %s\n' "$*"; failed=1; }

module_loaded()      { [ -d "/sys/module/${1//-/_}" ]; }
configfs_mounted()   { awk -v t="$TARGET" '$2 == t && $3 == "configfs" { found = 1 } END { exit !found }' /proc/mounts; }
module_persisted()   { grep -qsx -- "$1" /etc/modules-load.d/*.conf; }
configfs_persisted() { awk -v t="$TARGET" '$1 !~ /^#/ && $2 == t && $3 == "configfs" { found = 1 } END { exit !found }' /etc/fstab; }

# 运行中的 tlshd：优先取 systemd 单元的 ExecStart 路径（可能与 PATH 上的不是同一份），
# 单元缺失时回落 command -v。
tlshd_bin() {
	local exec path
	if command -v systemctl >/dev/null 2>&1; then
		exec=$(systemctl show -p ExecStart --value tlshd 2>/dev/null)
		path=$(printf '%s\n' "$exec" | sed -n 's/.*path=\([^ ;}]*\).*/\1/p' | head -n1)
		if [ -n "$path" ] && [ -x "$path" ]; then
			printf '%s\n' "$path"
			return 0
		fi
	fi
	command -v tlshd 2>/dev/null || true
}

# tlshd -v 输出形如 "tlshd, built from ktls-utils 1.5.0 on ..."（写 stderr）
tlshd_version() {
	"$1" -v 2>&1 | sed -n 's/.*ktls-utils \([0-9][0-9.]*\).*/\1/p' | head -n1
}

version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; }

if ! $CHECK && [ "$(id -u)" -ne 0 ]; then
	printf '需 root 运行（sudo bash %s；只读诊断加 --check）\n' "$0" >&2
	exit 1
fi

printf '== 1/4 内核模块（%s）\n' "$MODULES"
for m in $MODULES; do
	if module_loaded "$m"; then
		ok "已加载: $m"
	elif $CHECK; then
		fail "未加载: $m（sudo bash $0 就位）"
	elif modprobe "$m" 2>/dev/null && module_loaded "$m"; then
		ok "已加载: $m（modprobe）"
	else
		fail "加载失败: $m（内核 $(uname -r) 不含该模块？）"
	fi
done
if ! $CHECK; then
	for m in $MODULES; do
		if module_persisted "$m"; then
			ok "重启持久化: $m"
		else
			[ -f "$CONF" ] || printf '# kurrent 存储节点：nvmet 数据面模块（由 host-prepare.sh 维护）\n' > "$CONF"
			printf '%s\n' "$m" >> "$CONF"
			todo "写入 $CONF: $m（重启自动加载）"
		fi
	done
fi

printf '== 2/4 configfs 挂载（%s）\n' "$TARGET"
if configfs_mounted; then
	ok "已挂载"
elif $CHECK; then
	fail "未挂载（sudo bash $0 就位）"
elif mount -t configfs configfs "$TARGET" 2>/dev/null && configfs_mounted; then
	ok "已挂载（mount）"
else
	fail "挂载失败: $TARGET（目录缺失或权限异常）"
fi
if ! $CHECK; then
	if configfs_persisted; then
		ok "重启持久化: /etc/fstab"
	else
		printf 'configfs %s configfs defaults 0 0\n' "$TARGET" >> /etc/fstab
		todo "写入 /etc/fstab（重启自动挂载）"
	fi
fi

printf '== 3/4 验证\n'
if [ -d "$TARGET/nvmet" ]; then
	ok "configfs nvmet 就绪: $TARGET/nvmet"
else
	fail "configfs nvmet 不可见（模块或挂载异常）"
fi

printf '== 4/4 NVMe/TCP TLS 握手服务（tlshd >= %s）\n' "$TLSHD_MIN"
bin=$(tlshd_bin)
if [ -z "$bin" ]; then
	tlshd_bad=true
	fail "未安装 tlshd（ktls-utils >= $TLSHD_MIN；内核 TLS 握手依赖它）"
else
	ver=$(tlshd_version "$bin")
	if [ -z "$ver" ]; then
		tlshd_bad=true
		fail "无法解析版本: $bin -v（需要 ktls-utils >= $TLSHD_MIN）"
	elif version_ge "$ver" "$TLSHD_MIN"; then
		ok "版本 $ver: $bin"
	else
		tlshd_bad=true
		fail "版本过低: $ver（需 >= $TLSHD_MIN；1.5.0 起进程 keyring 优先，旧版会让轮换后的旧 PSK 遮蔽新 PSK）"
	fi
	if command -v systemctl >/dev/null 2>&1; then
		state=$(systemctl is-active tlshd 2>/dev/null || true)
		if [ "$state" = "active" ]; then
			ok "服务运行中: tlshd"
		elif [ "$state" = "unknown" ]; then
			todo "无 tlshd systemd 单元（确认该服务由其他方式常驻）"
		else
			tlshd_bad=true
			fail "服务未运行: tlshd（${state:-未知状态}；sudo systemctl enable --now tlshd）"
		fi
	fi
fi

if [ "$failed" -ne 0 ]; then
	printf '宿主前置未就绪（见上方 FAIL 项）\n' >&2
	if $tlshd_bad; then
		printf '  tlshd: 安装/升级 ktls-utils >= %s 并 sudo systemctl enable --now tlshd；构建步骤见 %s\n' \
			"$TLSHD_MIN" "$SCRIPT_DIR/nvmet-host/README.md" >&2
	fi
	exit 1
fi
printf '宿主前置就绪：nvmet 数据面可对外服务\n'
