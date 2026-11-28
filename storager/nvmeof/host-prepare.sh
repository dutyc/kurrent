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
# 只读诊断用 `kurrent host check --role storage`（无需 root，部署前排查用）。
set -u

if [ "$#" -ne 0 ]; then
	printf 'unsupported argument %s: this script only performs root-required setup; for read-only diagnostics use kurrent host check --role storage\n' "$*" >&2
	exit 2
fi

TARGET=/sys/kernel/config
MODULES="nvmet nvmet-tcp"
CONF=/etc/modules-load.d/kurrent-nvmet.conf
TLSHD_MIN=1.5.0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
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

if [ "$(id -u)" -ne 0 ]; then
	printf 'must run as root (sudo bash %s); for read-only diagnostics use kurrent host check --role storage\n' "$0" >&2
	exit 1
fi

printf '== 1/4 kernel modules (%s)\n' "$MODULES"
for m in $MODULES; do
	if module_loaded "$m"; then
		ok "loaded: $m"
	elif modprobe "$m" 2>/dev/null && module_loaded "$m"; then
		ok "loaded: $m (modprobe)"
	else
		fail "load failed: $m (does kernel $(uname -r) provide this module?)"
	fi
done
for m in $MODULES; do
	if module_persisted "$m"; then
		ok "persistent across reboot: $m"
	else
		[ -f "$CONF" ] || printf '# kurrent 存储节点：nvmet 数据面模块（由 host-prepare.sh 维护）\n' > "$CONF"
		printf '%s\n' "$m" >> "$CONF"
		todo "wrote $CONF: $m (loaded automatically on boot)"
	fi
done

printf '== 2/4 configfs mount (%s)\n' "$TARGET"
if configfs_mounted; then
	ok "mounted"
elif mount -t configfs configfs "$TARGET" 2>/dev/null && configfs_mounted; then
	ok "mounted (mount)"
else
	fail "mount failed: $TARGET (missing directory or permission problem)"
fi
if configfs_persisted; then
	ok "persistent across reboot: /etc/fstab"
else
	printf 'configfs %s configfs defaults 0 0\n' "$TARGET" >> /etc/fstab
	todo "wrote /etc/fstab (mounted automatically on boot)"
fi

printf '== 3/4 verification\n'
if [ -d "$TARGET/nvmet" ]; then
	ok "configfs nvmet ready: $TARGET/nvmet"
else
	fail "configfs nvmet not visible (module or mount problem)"
fi

printf '== 4/4 NVMe/TCP TLS handshake service (tlshd >= %s)\n' "$TLSHD_MIN"
bin=$(tlshd_bin)
if [ -z "$bin" ]; then
	tlshd_bad=true
	fail "tlshd not installed (ktls-utils >= $TLSHD_MIN; the kernel TLS handshake depends on it)"
else
	ver=$(tlshd_version "$bin")
	if [ -z "$ver" ]; then
		tlshd_bad=true
		fail "cannot parse the version: $bin -v (ktls-utils >= $TLSHD_MIN required)"
	elif version_ge "$ver" "$TLSHD_MIN"; then
		ok "version $ver: $bin"
	else
		tlshd_bad=true
		fail "version too old: $ver (need >= $TLSHD_MIN; since 1.5.0 the process keyring takes precedence; older versions let the old PSK mask the new one after a rotation)"
	fi
	if command -v systemctl >/dev/null 2>&1; then
		state=$(systemctl is-active tlshd 2>/dev/null || true)
		if [ "$state" = "active" ]; then
			ok "service running: tlshd"
		elif [ "$state" = "unknown" ]; then
			todo "no tlshd systemd unit (make sure the service runs some other way)"
		else
			tlshd_bad=true
			fail "service not running: tlshd (${state:-unknown}; sudo systemctl enable --now tlshd)"
		fi
	fi
fi

if [ "$failed" -ne 0 ]; then
	printf 'host prerequisites are not ready (see FAIL items above)\n' >&2
	if $tlshd_bad; then
		printf '  tlshd: install/upgrade ktls-utils >= %s and run sudo systemctl enable --now tlshd; build steps: %s\n' \
			"$TLSHD_MIN" "$SCRIPT_DIR/nvmet-host/README.md" >&2
	fi
	exit 1
fi
printf 'host prerequisites ready: the nvmet data plane can serve\n'
