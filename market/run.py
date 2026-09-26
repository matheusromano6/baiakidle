"""CLI do alerta de oportunidades no market do Baiak Idle (somente leitura).

  python run.py collect   - uma passada de coleta (historico + ativos)
  python run.py scan      - coleta e mostra/notifica oportunidades agora
  python run.py watch      - loop de coleta + notificacao a cada poll_seconds
  python run.py stats --name "tainted heart"  - estatisticas de um item

Para o painel web com acompanhamento de posicoes e lucro: python server.py
"""
import argparse
import time

import analyze
import notify
import scanner
from store import Store


def cmd_stats(cfg, store, name):
    since = analyze.now_ms() - cfg["history_days"] * 86_400_000
    keys = [r["key"] for r in store.all_keys_since(since)
            if name.lower() in r["key"].lower()]
    if not keys:
        print(f"nenhuma venda batendo '{name}' nos ultimos {cfg['history_days']}d")
        return
    for key in sorted(keys):
        st = analyze.stats(store.sales_for_key(key, since))
        if st:
            print(f"{key}\n  n={st['n']}  {st['per_day']:.1f}/dia  "
                  f"mediana={st['median']:.4g}  p25={st['p25']:.4g}  "
                  f"min={st['min']:.4g}  max={st['max']:.4g}")


def show(opps):
    if not opps:
        print("[scan] nenhuma oportunidade agora")
    for o in opps:
        print(notify.format_opp(o) + "\n")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["collect", "scan", "watch", "stats"])
    ap.add_argument("--name", default="", help="filtro para o comando stats")
    args = ap.parse_args()

    cfg = scanner.load_config()
    store = Store(cfg["db_path"])

    if args.cmd == "collect":
        scanner.collect(cfg, store, verbose=True)
    elif args.cmd == "scan":
        show(scanner.run_scan(cfg, store))
    elif args.cmd == "stats":
        cmd_stats(cfg, store, args.name)
    elif args.cmd == "watch":
        print(f"[watch] intervalo {cfg['poll_seconds']}s. Ctrl+C para parar.")
        while True:
            try:
                show(scanner.run_scan(cfg, store))
            except Exception as e:  # noqa: BLE001
                print(f"[erro] {e}")
            time.sleep(cfg["poll_seconds"])


if __name__ == "__main__":
    main()
