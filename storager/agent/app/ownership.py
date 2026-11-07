"""状态挂载目录属主对齐。

容器以 root 运行时新建的证书/日志归 root，宿主目录却属于操作员；不对齐会让
宿主侧的备份与迁移无法读取。无权限（如只读挂载）时静默跳过。
"""

import os
from pathlib import Path


def align_owner(path: Path, ref: Path | None = None) -> None:
    """把文件属主对齐参考目录（缺省为所在目录）；同属主时不做事。"""
    path = Path(path)
    anchor = Path(ref) if ref is not None else path.parent
    try:
        st = anchor.stat()
        current = path.stat()
        if current.st_uid == st.st_uid and current.st_gid == st.st_gid:
            return
        os.chown(path, st.st_uid, st.st_gid)
    except OSError:
        pass


def align_tree(root: Path) -> None:
    """递归把状态树内的属主对齐根目录（容器启动时修正历史 root 属主）。"""
    root = Path(root)
    try:
        anchor = root.stat()
    except OSError:
        return
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            target = Path(dirpath) / name
            try:
                st = target.stat()
                if st.st_uid != anchor.st_uid or st.st_gid != anchor.st_gid:
                    os.chown(target, anchor.st_uid, anchor.st_gid)
            except OSError:
                pass
