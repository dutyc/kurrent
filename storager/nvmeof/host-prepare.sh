#!/usr/bin/env bash
# kurrent 存储节点宿主前置：nvmet 数据面（内核模块加载 + configfs 挂载 + 重启持久化）。
#
# 内核 nvmet target 无法容器化：4420 数据面由宿主内核提供，节点部署前须加载
# nvmet / nvmet-tcp 并挂载 configfs，且跨重启持久化（否则重启后节点静默失效）。
# 幂等，可重复执行；就位操作需 root。
#
# 用法：
#   sudo bash host-prepare.sh     # 就位（modprobe + mount + /etc/modules-load.d + /etc/fstab）
#   bash host-prepare.sh --check  # 只读诊断、不做修改（无需 root；部署前排查用）
set -u

TARGET=/sys/kernel/config
MODULES="nvmet nvmet-tcp"
CONF=/etc/modules-load.d/kurrent-nvmet.conf
CHECK=false
[ "${1:-}" = "--check" ] && CHECK=true
failed=0

ok()   { printf '  ok   %s\n' "$*"; }
todo() { printf '  todo %s\n' "$*"; }
fail() { printf '  FAIL %s\n' "$*"; failed=1; }

module_loaded()      { [ -d "/sys/module/${1//-/_}" ]; }
configfs_mounted()   { awk -v t="$TARGET" '$2 == t && $3 == "configfs" { found = 1 } END { exit !found }' /proc/mounts; }
module_persisted()   { grep -qsx -- "$1" /etc/modules-load.d/*.conf; }
configfs_persisted() { awk -v t="$TARGET" '$1 !~ /^#/ && $2 == t && $3 == "configfs" { found = 1 } END { exit !found }' /etc/fstab; }

if ! $CHECK && [ "$(id -u)" -ne 0 ]; then
	printf '需 root 运行（sudo bash %s；只读诊断加 --check）\n' "$0" >&2
	exit 1
fi

printf '== 1/3 内核模块（%s）\n' "$MODULES"
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

printf '== 2/3 configfs 挂载（%s）\n' "$TARGET"
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

printf '== 3/3 验证\n'
if [ -d "$TARGET/nvmet" ]; then
	ok "configfs nvmet 就绪: $TARGET/nvmet"
else
	fail "configfs nvmet 不可见（模块或挂载异常）"
fi

if [ "$failed" -ne 0 ]; then
	printf '宿主前置未就绪（见上方 FAIL 项）\n' >&2
	exit 1
fi
printf '宿主前置就绪：nvmet 数据面可对外服务\n'
