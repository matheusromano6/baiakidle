"""Monta o zip PRA COMPARTILHAR (dist/v{VERSION}-BaiakIdleBot.zip), usando
routines.json/settings.json PADRAO (tudo desabilitado, exceto 'Entregar Codex
e Vender Loot') - NAO as configuracoes pessoais de quem gerou o build (chefes
escolhidos, thresholds ajustados, hunt favorita etc). Assim quem recebe o bot
comeca do zero e configura do jeito dele, sem herdar nada de terceiros.

O nome do zip (e do .exe dentro dele) leva a versao NA FRENTE
(ex: 'v4.9.10-BaiakIdleBot.zip') pra dar pra diferenciar builds antigos dos
novos so olhando o nome do arquivo.

Nao mexe no routines.json/settings.json da pasta raiz (esses continuam sendo
os SEUS, usados quando voce roda 'python gui.py' ou builda pra uso proprio).

Uso: rode 'python build.bat' (ou build_ci.bat) primeiro pra gerar
dist/BaiakIdleBot.exe, depois 'python make_share_zip.py'.
"""

import json
import os
import shutil
import zipfile

import bot

DIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist")
EXE_PATH = os.path.join(DIST_DIR, "BaiakIdleBot.exe")
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
VERSIONED_EXE_NAME = f"v{bot.VERSION}-BaiakIdleBot.exe"
ZIP_PATH = os.path.join(DIST_DIR, f"v{bot.VERSION}-BaiakIdleBot.zip")


def main():
    if not os.path.exists(EXE_PATH):
        raise SystemExit(f"'{EXE_PATH}' nao existe - rode o build.bat primeiro.")

    tmp_dir = os.path.join(DIST_DIR, "_share_tmp")
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir)

    try:
        shutil.copy(EXE_PATH, os.path.join(tmp_dir, VERSIONED_EXE_NAME))
        shutil.copy(ICON_PATH, os.path.join(tmp_dir, "icon.ico"))

        with open(os.path.join(tmp_dir, "routines.json"), "w", encoding="utf-8") as file:
            json.dump(bot.DEFAULT_ROUTINES, file, ensure_ascii=False, indent=2)

        with open(os.path.join(tmp_dir, "settings.json"), "w", encoding="utf-8") as file:
            json.dump(bot.DEFAULT_SETTINGS, file, ensure_ascii=False, indent=2)

        if os.path.exists(ZIP_PATH):
            os.remove(ZIP_PATH)

        with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in os.listdir(tmp_dir):
                zf.write(os.path.join(tmp_dir, name), name)

        print(f"Pronto: {ZIP_PATH} (routines.json e settings.json com valores padrao)")
    finally:
        shutil.rmtree(tmp_dir)


if __name__ == "__main__":
    main()
