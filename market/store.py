"""Armazenamento SQLite: vendas historicas, oportunidades vivas, posicoes."""
import re
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS sales (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL, kind TEXT NOT NULL, name TEXT,
  tier INTEGER, qty INTEGER, gold_amount INTEGER,
  up_level INTEGER DEFAULT 0, ftier INTEGER DEFAULT 0,
  final_price INTEGER NOT NULL, unit_price REAL NOT NULL,
  bid_count INTEGER, created_at INTEGER, ends_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sales_key ON sales(key, ends_at);

CREATE TABLE IF NOT EXISTS opportunities (
  auction_id INTEGER PRIMARY KEY,
  key TEXT, name TEXT, kind TEXT,
  current_price INTEGER, unit_price REAL,
  median REAL, p25 REAL, discount REAL, suggested_max INTEGER,
  est_saving INTEGER, tier TEXT, basis TEXT,
  rarity INTEGER, attr_score REAL, attrs_text TEXT,
  supply INTEGER, days_supply REAL, supply_pressure REAL, closing_6h INTEGER,
  verdict TEXT, sales_per_day REAL, n INTEGER,
  bid_count INTEGER, ends_at INTEGER,
  first_seen INTEGER, last_seen INTEGER, notified_at INTEGER,
  dismissed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  auction_id INTEGER,
  key TEXT, name TEXT, kind TEXT,
  max_bid INTEGER, buy_price INTEGER, median_at_buy REAL,
  sell_max INTEGER, use_max INTEGER,
  opened_at INTEGER,
  auction_status TEXT DEFAULT 'active',
  last_auction_price INTEGER, last_bid_count INTEGER, auction_ends_at INTEGER,
  state TEXT DEFAULT 'bidding',
  sell_price INTEGER, sold_at INTEGER, fee_coins INTEGER,
  note TEXT
);

CREATE TABLE IF NOT EXISTS opp_log (
  auction_id INTEGER PRIMARY KEY,
  key TEXT, name TEXT, kind TEXT, rarity INTEGER,
  detected_at INTEGER, detected_price INTEGER, detected_discount REAL,
  fair REAL, fair_lot REAL, suggested_max INTEGER, breakeven INTEGER,
  est_saving INTEGER, tier TEXT, basis TEXT, ftier INTEGER, attrs_text TEXT,
  flash INTEGER DEFAULT 0, ends_at INTEGER,
  closed_at INTEGER, final_price INTEGER, final_bids INTEGER, outcome TEXT
);

CREATE TABLE IF NOT EXISTS characters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, vocation TEXT, level INTEGER, active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS watch (
  auction_id INTEGER PRIMARY KEY,
  name TEXT, kind TEXT, key TEXT, note TEXT, added_at INTEGER,
  fair REAL, fair_lot REAL, suggested_max INTEGER,
  current_price INTEGER, bid_count INTEGER, ends_at INTEGER,
  status TEXT DEFAULT 'active', alerted INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS gold_snap (
  auction_id INTEGER, ts INTEGER, price INTEGER, bids INTEGER,
  ends_at INTEGER, gold_amount INTEGER
);
CREATE INDEX IF NOT EXISTS idx_gold_snap ON gold_snap(auction_id, ts);

CREATE TABLE IF NOT EXISTS bounties (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, rarities TEXT DEFAULT '', note TEXT DEFAULT '',
  active INTEGER DEFAULT 1, added_at INTEGER,
  last_checked INTEGER, last_hit_at INTEGER, notified_ids TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
"""

# estados de posicao: bidding -> holding -> sold  |  terminal: lost


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.lock = threading.Lock()

    def _migrate(self):
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(opportunities)")}
        for col, decl in (("est_saving", "INTEGER"), ("tier", "TEXT"),
                          ("basis", "TEXT"), ("rarity", "INTEGER"),
                          ("attr_score", "REAL"), ("attrs_text", "TEXT"),
                          ("supply", "INTEGER"), ("days_supply", "REAL"),
                          ("verdict", "TEXT"), ("supply_pressure", "REAL"),
                          ("closing_6h", "INTEGER"), ("trend", "REAL")):
            if col not in cols:
                self.db.execute(f"ALTER TABLE opportunities ADD COLUMN {col} {decl}")

        scols = {r["name"] for r in self.db.execute("PRAGMA table_info(sales)")}
        if "up_level" not in scols:
            self.db.execute("ALTER TABLE sales ADD COLUMN up_level INTEGER DEFAULT 0")
            for r in self.db.execute(
                    "SELECT id, key FROM sales WHERE kind='gear'").fetchall():
                m = re.search(r"\|u(\d+)", r["key"])
                if m and m.group(1) != "0":
                    self.db.execute("UPDATE sales SET up_level=? WHERE id=?",
                                    (int(m.group(1)), r["id"]))
        if "ftier" not in scols:
            self.db.execute("ALTER TABLE sales ADD COLUMN ftier INTEGER DEFAULT 0")
            # chave de gear ganhou sufixo |f{ftier}; as antigas eram nao-forjadas
            self.db.execute("UPDATE sales SET key = key || '|f0' "
                            "WHERE kind='gear' AND key NOT LIKE '%|f%'")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_sales_gear "
                        "ON sales(name, tier, up_level, ftier, ends_at)")

        lcols = {r["name"] for r in self.db.execute("PRAGMA table_info(opp_log)")}
        if "fair_lot" not in lcols:
            self.db.execute("ALTER TABLE opp_log ADD COLUMN fair_lot REAL")
            # aproxima pelas linhas antigas: breakeven = fair_lot * (1 - rake 0.10)
            self.db.execute("UPDATE opp_log SET fair_lot = breakeven / 0.9 "
                            "WHERE fair_lot IS NULL AND breakeven IS NOT NULL")
        for col in ("ftier", "attrs_text", "flash"):
            if col not in lcols:
                self.db.execute(f"ALTER TABLE opp_log ADD COLUMN {col} "
                                + ("TEXT" if col == "attrs_text" else "INTEGER"))

        wcols = {r["name"] for r in self.db.execute("PRAGMA table_info(watch)")}
        for col, decl in (("est", "INTEGER DEFAULT 0"), ("basis", "TEXT"), ("n", "INTEGER")):
            if col not in wcols:
                self.db.execute(f"ALTER TABLE watch ADD COLUMN {col} {decl}")

        bcols = {r["name"] for r in self.db.execute("PRAGMA table_info(bounties)")}
        for col in ("kinds", "vocs", "classes", "attrs"):   # filtros da busca salva
            if col not in bcols:
                self.db.execute(f"ALTER TABLE bounties ADD COLUMN {col} TEXT DEFAULT ''")

        pcols = {r["name"] for r in self.db.execute("PRAGMA table_info(positions)")}
        if "sell_max" not in pcols:
            self.db.execute("ALTER TABLE positions ADD COLUMN sell_max INTEGER")
            self.db.execute("ALTER TABLE positions ADD COLUMN use_max INTEGER")
            # backfill aproximado das posicoes de gear ja existentes
            self.db.execute("UPDATE positions SET use_max = ROUND(median_at_buy), "
                            "sell_max = ROUND(median_at_buy * 0.75) "
                            "WHERE kind='gear' AND use_max IS NULL")

        # gold passou a ter chave por faixa de tamanho (gold:p/m/g)
        if self.db.execute("SELECT 1 FROM sales WHERE key='gold' LIMIT 1").fetchone():
            self.db.execute(
                "UPDATE sales SET key = CASE "
                " WHEN gold_amount < 150000000 THEN 'gold:p' "
                " WHEN gold_amount >= 1000000000 THEN 'gold:g' "
                " ELSE 'gold:m' END "
                "WHERE kind='gold' AND key='gold'")
        self.db.commit()

    def _run(self, sql, args=(), commit=False, fetch=None):
        with self.lock:
            cur = self.db.execute(sql, args)
            out = cur.fetchall() if fetch == "all" else cur.fetchone() if fetch == "one" else cur.rowcount
            if commit:
                self.db.commit()
            return out

    def commit(self):
        with self.lock:
            self.db.commit()

    # -- meta / settings ---------------------------------------------------
    def get_setting(self, k, default=None):
        row = self._run("SELECT v FROM settings WHERE k=?", (k,), fetch="one")
        return row["v"] if row else default

    def set_setting(self, k, v):
        self._run("INSERT INTO settings(k,v) VALUES(?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)), commit=True)

    # -- sales ------------------------------------------------------------
    def add_sale(self, row, nz):
        return self._run(
            "INSERT OR IGNORE INTO sales"
            "(id,key,kind,name,tier,qty,gold_amount,up_level,ftier,final_price,"
            " unit_price,bid_count,created_at,ends_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], nz["key"], nz["kind"], nz["name"], nz["tier"], nz["qty"],
             nz["gold_amount"], nz.get("up", 0), nz.get("ftier", 0),
             row["currentPrice"], nz["unit_price"], row.get("bidCount"),
             row.get("createdAt"), row.get("endsAt")))

    def sales_for_key(self, key, since_ms):
        return self._run("SELECT unit_price, ends_at, bid_count FROM sales "
                         "WHERE key=? AND ends_at>=?", (key, since_ms), fetch="all")

    def stack_units_sold(self, key, since_ms):
        """Unidades vendidas de um empilhavel desde 'since_ms' (demanda real)."""
        row = self._run("SELECT COALESCE(SUM(qty), 0) AS u FROM sales WHERE key=? AND ends_at>=?",
                        (key, since_ms), fetch="one")
        return row["u"] if row else 0

    def sales_for_name(self, name_like, since_ms):
        return self._run(
            "SELECT key, kind, name, tier, up_level, ftier, qty, final_price, "
            "unit_price, bid_count, ends_at FROM sales "
            "WHERE name LIKE ? AND ends_at >= ? ORDER BY ends_at DESC",
            (name_like, since_ms), fetch="all")

    def gold_sales_window(self, since_ms):
        """Vendas de gold (preco por 1M, tamanho do lote, fim) ordenadas no
        tempo - base da taxa de mercado."""
        return self._run("SELECT unit_price, gold_amount, ends_at FROM sales "
                         "WHERE kind='gold' AND unit_price>0 AND ends_at>=? "
                         "ORDER BY ends_at", (since_ms,), fetch="all")

    # -- gold_snap: retrato dos leiloes de gold perto do fim (pra aprender
    # depois a probabilidade de fechar abaixo de um teto, ja com os lances) --
    def add_gold_snaps(self, rows):
        with self.lock:
            self.db.executemany(
                "INSERT INTO gold_snap(auction_id,ts,price,bids,ends_at,gold_amount)"
                " VALUES(?,?,?,?,?,?)", rows)
            self.db.commit()

    def prune_gold_snaps(self, before_ms):
        self._run("DELETE FROM gold_snap WHERE ts < ?", (before_ms,), commit=True)

    def gold_snap_counts(self):
        r = self._run("SELECT COUNT(*) AS n, COUNT(DISTINCT auction_id) AS a "
                      "FROM gold_snap", fetch="one")
        return r["n"], r["a"]

    def gold_sales_all(self, since_ms):
        return self._run("SELECT unit_price, ends_at FROM sales "
                         "WHERE kind='gold' AND ends_at>=?", (since_ms,), fetch="all")

    def gear_sales_keys(self, since_ms):
        """Vendas de equipamento sem upgrade/forja (nome, raridade, chave com
        atributos, preco) - base da estimativa 'por tipo'."""
        return self._run(
            "SELECT name, tier, key, final_price FROM sales WHERE kind='gear' "
            "AND up_level=0 AND ftier=0 AND ends_at>=?", (since_ms,), fetch="all")

    def gear_sales_all_fields(self, since_ms):
        return self._run(
            "SELECT name, tier, up_level, ftier, final_price, bid_count, ends_at "
            "FROM sales WHERE kind='gear' AND ends_at>=? ORDER BY ends_at",
            (since_ms,), fetch="all")

    def gear_sales_by_tier(self, name, tier, up_level, since_ms):
        return self._run(
            "SELECT final_price AS unit_price, ends_at, key FROM sales "
            "WHERE kind='gear' AND name=? AND tier=? AND up_level=? AND ftier=0 "
            "AND ends_at>=?",
            (name, tier, up_level, since_ms), fetch="all")

    def all_keys_since(self, since_ms):
        return self._run("SELECT DISTINCT key FROM sales WHERE ends_at>=?",
                         (since_ms,), fetch="all")

    # -- opportunities --------------------------------------------------
    def upsert_opportunity(self, o):
        now = int(time.time() * 1000)
        self._run(
            "INSERT INTO opportunities"
            "(auction_id,key,name,kind,current_price,unit_price,median,p25,"
            " discount,suggested_max,est_saving,tier,basis,rarity,attr_score,"
            " attrs_text,supply,days_supply,supply_pressure,closing_6h,verdict,"
            " trend,sales_per_day,n,bid_count,ends_at,first_seen,last_seen)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(auction_id) DO UPDATE SET "
            " current_price=excluded.current_price, unit_price=excluded.unit_price,"
            " median=excluded.median, p25=excluded.p25, discount=excluded.discount,"
            " suggested_max=excluded.suggested_max, est_saving=excluded.est_saving,"
            " tier=excluded.tier, basis=excluded.basis, rarity=excluded.rarity,"
            " attr_score=excluded.attr_score, attrs_text=excluded.attrs_text,"
            " supply=excluded.supply, days_supply=excluded.days_supply,"
            " supply_pressure=excluded.supply_pressure, closing_6h=excluded.closing_6h,"
            " verdict=excluded.verdict, trend=excluded.trend,"
            " sales_per_day=excluded.sales_per_day,"
            " n=excluded.n, bid_count=excluded.bid_count, ends_at=excluded.ends_at,"
            " last_seen=excluded.last_seen",
            (o["auction_id"], o["key"], o["name"], o["kind"], o["current_price"],
             o["unit_price"], o["median"], o["p25"], o["discount"], o["suggested_max"],
             o["est_saving"], o["tier"], o.get("basis"), o.get("rarity"),
             o.get("attr_score"), o.get("attrs_text"), o.get("supply"),
             o.get("days_supply"), o.get("supply_pressure"), o.get("closing_6h"),
             o.get("verdict"), o.get("trend"), o["sales_per_day"], o["n"],
             o["bid_count"], o["ends_at"], now, now))

    def mark_notified(self, auction_id):
        self._run("UPDATE opportunities SET notified_at=? WHERE auction_id=? "
                  "AND notified_at IS NULL",
                  (int(time.time() * 1000), auction_id), commit=True)

    def unnotified_opportunities(self):
        return self._run("SELECT * FROM opportunities WHERE notified_at IS NULL "
                         "AND dismissed=0", fetch="all")

    def list_opportunities(self, active_only=False):
        sql = "SELECT * FROM opportunities WHERE dismissed=0"
        args = ()
        if active_only:
            sql += " AND ends_at > ?"
            args = (int(time.time() * 1000),)
        return self._run(sql + " ORDER BY discount DESC", args, fetch="all")

    def dismiss_opportunity(self, auction_id):
        self._run("UPDATE opportunities SET dismissed=1 WHERE auction_id=?",
                  (auction_id,), commit=True)

    def drop_opportunity(self, auction_id):
        self._run("DELETE FROM opportunities WHERE auction_id=? AND auction_id "
                  "NOT IN (SELECT auction_id FROM positions)",
                  (auction_id,), commit=True)

    def prune_opportunities(self, keep_ids, older_than_ms):
        keep = set(keep_ids)
        rows = self._run("SELECT auction_id, ends_at FROM opportunities", fetch="all")
        drop = [r["auction_id"] for r in rows
                if r["auction_id"] not in keep and (r["ends_at"] or 0) < older_than_ms]
        if not drop:
            return 0
        with self.lock:
            has_pos = {r["auction_id"] for r in self.db.execute(
                "SELECT DISTINCT auction_id FROM positions")}
            drop = [i for i in drop if i not in has_pos]
            self.db.executemany("DELETE FROM opportunities WHERE auction_id=?",
                                [(i,) for i in drop])
            self.db.commit()
        return len(drop)

    # -- positions ------------------------------------------------------
    def create_position(self, o, max_bid, note=""):
        now = int(time.time() * 1000)
        lot = o["current_price"] / o["unit_price"] if o.get("unit_price") else 1
        self._run(
            "INSERT INTO positions"
            "(auction_id,key,name,kind,max_bid,median_at_buy,sell_max,use_max,"
            " opened_at,auction_status,last_auction_price,last_bid_count,"
            " auction_ends_at,state,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (o["auction_id"], o["key"], o["name"], o["kind"], max_bid, o["median"],
             o.get("suggested_max"), round(o["median"] * lot),
             now, "active", o["current_price"], o["bid_count"], o["ends_at"],
             "bidding", note), commit=True)

    def list_positions(self):
        return self._run("SELECT * FROM positions ORDER BY opened_at DESC", fetch="all")

    def get_position(self, pid):
        return self._run("SELECT * FROM positions WHERE id=?", (pid,), fetch="one")

    def open_auction_positions(self):
        return self._run(
            "SELECT * FROM positions WHERE auction_status='active' "
            "AND state IN ('bidding','holding')", fetch="all")

    def update_position(self, pid, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._run(f"UPDATE positions SET {cols} WHERE id=?",
                  (*fields.values(), pid), commit=True)

    def delete_position(self, pid):
        self._run("DELETE FROM positions WHERE id=?", (pid,), commit=True)

    # -- opp_log (historico das analises) --------------------------
    def log_opportunity(self, o):
        """Registra a 1a vez que uma oportunidade foi detectada."""
        lot = o["current_price"] / o["unit_price"] if o.get("unit_price") else 1
        return self._run(
            "INSERT OR IGNORE INTO opp_log"
            "(auction_id,key,name,kind,rarity,detected_at,detected_price,"
            " detected_discount,fair,fair_lot,suggested_max,breakeven,est_saving,"
            " tier,basis,ftier,attrs_text,flash,ends_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (o["auction_id"], o["key"], o["name"], o["kind"], o.get("rarity"),
             int(time.time() * 1000), o["current_price"], o["discount"],
             o["median"], o["median"] * lot, o["suggested_max"], o.get("breakeven"),
             o["est_saving"], o["tier"], o.get("basis"), o.get("ftier"),
             o.get("attrs_text"), 1 if o.get("flash_item") else 0,
             o["ends_at"]), commit=True)

    def get_opp_log(self, auction_id):
        return self._run("SELECT * FROM opp_log WHERE auction_id=?",
                         (auction_id,), fetch="one")

    def get_sale(self, sale_id):
        return self._run("SELECT final_price, bid_count FROM sales WHERE id=?",
                         (sale_id,), fetch="one")

    def close_opp_log(self, auction_id, final_price, final_bids, outcome):
        self._run(
            "UPDATE opp_log SET closed_at=?, final_price=?, final_bids=?, outcome=? "
            "WHERE auction_id=? AND closed_at IS NULL",
            (int(time.time() * 1000), final_price, final_bids, outcome, auction_id),
            commit=True)

    def pending_opp_logs(self, before_ms):
        return self._run("SELECT * FROM opp_log WHERE closed_at IS NULL "
                         "AND ends_at < ?", (before_ms,), fetch="all")

    def list_opp_log(self, since_ms, flash_only=False):
        sql = ("SELECT l.*, (SELECT COUNT(*) FROM positions p "
               "WHERE p.auction_id=l.auction_id) AS acted "
               "FROM opp_log l WHERE detected_at >= ?")
        if flash_only:
            sql += " AND flash=1"
        return self._run(sql + " ORDER BY detected_at DESC", (since_ms,), fetch="all")

    def prune_opp_log(self, before_ms):
        self._run("DELETE FROM opp_log WHERE detected_at < ?", (before_ms,), commit=True)

    # -- characters ---------------------------------------------------
    def list_characters(self):
        return self._run("SELECT * FROM characters ORDER BY name", fetch="all")

    def add_character(self, name, vocation, level):
        self._run("INSERT INTO characters(name,vocation,level,active) "
                  "VALUES(?,?,?,1)", (name, vocation, int(level)), commit=True)

    def update_character(self, cid, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._run(f"UPDATE characters SET {cols} WHERE id=?",
                  (*fields.values(), cid), commit=True)

    def delete_character(self, cid):
        self._run("DELETE FROM characters WHERE id=?", (cid,), commit=True)

    # -- watch (acompanhando pelo id) -------------------------------
    def add_watch(self, w):
        self._run(
            "INSERT OR REPLACE INTO watch"
            "(auction_id,name,kind,key,note,added_at,fair,fair_lot,suggested_max,"
            " est,basis,n,current_price,bid_count,ends_at,status,alerted)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (w["auction_id"], w["name"], w["kind"], w["key"], w.get("note", ""),
             int(time.time() * 1000), w.get("fair"), w.get("fair_lot"),
             w.get("suggested_max"), int(w.get("est") or 0), w.get("basis"), w.get("n"),
             w["current_price"], w["bid_count"],
             w["ends_at"], "active"), commit=True)

    def list_watch(self):
        return self._run("SELECT * FROM watch ORDER BY ends_at", fetch="all")

    def active_watch(self):
        return self._run("SELECT * FROM watch WHERE status='active'", fetch="all")

    def update_watch(self, auction_id, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._run(f"UPDATE watch SET {cols} WHERE auction_id=?",
                  (*fields.values(), auction_id), commit=True)

    def delete_watch(self, auction_id):
        self._run("DELETE FROM watch WHERE auction_id=?", (auction_id,), commit=True)

    # -- bounties (procurar por oferta) ----------------------------
    def add_bounty(self, name, rarities="", note="", kinds="", vocs="",
                   classes="", attrs=""):
        self._run("INSERT INTO bounties(name,rarities,note,kinds,vocs,classes,"
                  "attrs,active,added_at) VALUES(?,?,?,?,?,?,?,1,?)",
                  (name, rarities, note, kinds, vocs, classes, attrs,
                   int(time.time() * 1000)), commit=True)

    def list_bounties(self):
        return self._run("SELECT * FROM bounties ORDER BY added_at DESC", fetch="all")

    def active_bounties(self):
        return self._run("SELECT * FROM bounties WHERE active=1 ORDER BY added_at DESC",
                         fetch="all")

    def update_bounty(self, bid, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._run(f"UPDATE bounties SET {cols} WHERE id=?",
                  (*fields.values(), bid), commit=True)

    def delete_bounty(self, bid):
        self._run("DELETE FROM bounties WHERE id=?", (bid,), commit=True)
