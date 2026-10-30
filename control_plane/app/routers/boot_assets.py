"""引导链清单端点：菜单项与其依赖文件是否就位（只读）。"""

from fastapi import Depends, HTTPException

from .. import boot_assets, boot_pki
from ..auth import authenticate, authorize_request
from ..config import settings
from ..principal import PrincipalRouter

router = PrincipalRouter(
    dependencies=[Depends(authenticate), Depends(authorize_request)],
    principal={"human", "automation"},
)


@router.get("/boot-assets")
def get_boot_assets():
    try:
        return boot_assets.inventory(
            settings.dnsmasq_conf,
            settings.tftp_dir,
            boot_ca_path=boot_pki.boot_ca_dir(settings.pki_dir) / boot_pki.CA_CERT_FILENAME,
            boot_https_port=settings.boot_https_port,
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, f"boot asset source missing: {exc.filename}") from exc
