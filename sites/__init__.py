"""站点注册表：新增站点时在这里登记即可。"""
from typing import Dict

from core.base import BaseSite, Context
from core.config import Config

from .nodeseek import NodeSeekSite
from .shaobing import ShaoBingSite
from .v2ex import V2exSite

REGISTRY = {
    NodeSeekSite.name: NodeSeekSite,
    V2exSite.name: V2exSite,
    ShaoBingSite.name: ShaoBingSite,
}


def build_sites(cfg: Config, ctx: Context) -> Dict[str, BaseSite]:
    sites: Dict[str, BaseSite] = {}
    for key, site_cfg in cfg.sites.items():
        if not site_cfg.enabled:
            continue
        cls = REGISTRY.get(key)
        if cls is None:
            ctx.logger.warning("未知站点 %s，已跳过", key)
            continue
        if not site_cfg.accounts:
            ctx.logger.warning("站点 %s 无账号配置，已跳过", key)
            continue
        sites[key] = cls(ctx, site_cfg)
    return sites
