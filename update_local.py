"""
update_local.py
=================

Met à jour la copie locale du projet avec les dernières données poussées
par les workflows GitHub Actions (ANTILOPE + rattrapage mensuel
COMÉPHORE + hydrologie quotidienne), puis régénère les graphiques et la
page Sol / Hydrologie (depuis hydrologie_yavannome.json, sans réseau).

Usage :
    python update_local.py

Équivalent à :
    git pull
    python precip_visualize.py
    python soil_hydrology_update.py --render-only
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("update_local")


def main() -> int:
    log.info("git pull...")
    result = subprocess.run(["git", "pull"], capture_output=True, text=True)
    print(result.stdout, end="")
    if result.returncode != 0:
        log.error("git pull a échoué :\n%s", result.stderr)
        log.error(
            "Si le message mentionne des modifications locales non commitées, "
            "voir le README (section dépannage) avant de relancer."
        )
        return 1

    log.info("Régénération des graphiques...")
    import precip_visualize

    status = precip_visualize.main()
    if Path(__file__).with_name("hydrologie_yavannome.json").exists():
        log.info("Régénération de la page Sol / Hydrologie...")
        import soil_hydrology_update

        status = status or soil_hydrology_update.main(["--render-only"])
    return status


if __name__ == "__main__":
    sys.exit(main())
