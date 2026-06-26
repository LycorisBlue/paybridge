# Copyright (c) 2026, AMOAMAN and contributors
# For license information, please see license.txt
"""Données d'exemple (fixtures) pour le flux de paiement BOA.

Génère et charge :
  - des comptes bancaires (Bank Account) BOA pour plusieurs fournisseurs, avec
    custom_rib / custom_bank_code / custom_swift_code — calqués sur le compte
    réel d'ORANGE CI ;
  - 100+ Purchase Invoice payables, réparties sur ces fournisseurs et des items
    de service réels du site.

Deux usages
-----------
1) (Re)générer les fichiers JSON de fixtures — sans base de données ::

       cd ~/bench-dev/apps/paybridge
       ~/bench-dev/env/bin/python -m paybridge.boa.demo.demo_data

   Produit  paybridge/boa/demo/bank_account.json  et  purchase_invoice.json.

2) Charger les données sur un site (insert + submit) — INDISPENSABLE pour que
   les factures soient « payables » par le module BOA ::

       cd ~/bench-dev
       bench --site <site> execute paybridge.boa.demo.demo_data.load

   Pour ajouter un lot supplémentaire (ex. 500 factures de plus, sans
   collision avec les DEMO-XXXX déjà soumis) :

       bench --site <site> execute paybridge.boa.demo.demo_data.load \\
         --kwargs '{"count": 500}'

   ``load`` détecte automatiquement le dernier ``DEMO-XXXX`` en base et
   redémarre la numérotation à ``last + 1``. Pour forcer un point de départ
   explicite, passer ``bill_no_start`` dans ``--kwargs``.

⚠️  AVANT DE LANCER ``load`` :
    - stopper ``bench start`` (Ctrl-C dans son terminal) ou
      ``supervisorctl stop all`` selon le déploiement, pour qu'aucun
      worker / scheduler / gunicorn ne tienne les verrous ;
    - tuer les ``bench execute …load`` orphelins d'une tentative précédente :
        pkill -9 -f "bench_helper frappe.*execute.*demo_data"
    - une seule instance de ``load`` doit tourner à la fois (un fichier-verrou
      ``demo_data/.load.lock`` est posé pour le garantir).

⚠️  Ce module n'est PAS enregistré dans `hooks.fixtures` : ces données ne
    doivent jamais être importées automatiquement au `migrate` (surtout pas
    en production). Les valeurs ci-dessous proviennent du site
    prod.amoaman.com.
"""

import fcntl
import json
import os
import random
from datetime import date, timedelta

# --- Constantes société / comptabilité (site prod.amoaman.com) --------------
COMPANY = "AMOAMAN & ASSOCIES"
CURRENCY = "XOF"
CREDIT_TO = "4011 - Fournisseurs - A&A"          # compte fournisseurs (payable)
EXPENSE_ACCOUNT = "6011 - Dans la Région - A&A"  # charge sur les lignes
COST_CENTER = "Principal - A&A"
PRICE_LIST = "Achat standard"

# --- Constantes bancaires (toutes les coordonnées sont chez BOA CI) ---------
BANK = "Bank of Africa Côte d'Ivoire"
BANK_CODE = "01089"   # « Code bban » BOA CI (override, comme le compte ORANGE)
SWIFT = "ORBKBFOG"    # BIC du compte ORANGE CI réel
RIB_BANK = "CI061"    # code banque (5)
RIB_BRANCH = "02002"  # code guichet (5)

#: Fournisseurs réels (≥4, ORANGE CI = modèle). Le RIB UEMOA (24 car.) est
#: reconstruit : banque(5) + guichet(5) + n°compte(12) + clé(2).
SUPPLIERS = [
    {"supplier": "ORANGE CI",      "account": "987654321098", "key": "47"},  # réel
    {"supplier": "YANGO",          "account": "100000000001", "key": "11"},
    {"supplier": "SUNU ASSURANCE", "account": "200000000002", "key": "22"},
    {"supplier": "AIRFRANCE",      "account": "300000000003", "key": "33"},
    {"supplier": "ACLYK",          "account": "400000000004", "key": "44"},
]

#: Items de service réels (is_stock_item = 0 → aucune gestion de stock/entrepôt).
ITEMS = [
    {"item_code": "PSV-2026-00024",          "uom": "JRS",   "rate": 150000},
    {"item_code": "PSV-2026-00018",          "uom": "TARIF", "rate": 2500000},
    {"item_code": "PSV-2026-00017",          "uom": "TARIF", "rate": 1800000},
    {"item_code": "PSV-2026-00015",          "uom": "JRS",   "rate": 200000},
    {"item_code": "PSV-2026-00013",          "uom": "JRS",   "rate": 200000},
    {"item_code": "PSV-2026-00006",          "uom": "JRS",   "rate": 175000},
    {"item_code": "Formation-2026-00001",    "uom": "Unité", "rate": 500000},
    {"item_code": "Solution-P2U-2026-00001", "uom": "Unité", "rate": 750000},
]

#: Nombre de factures à générer (> 100, réparties sur les fournisseurs).
INVOICE_COUNT = 120

#: Date de départ des factures (étalées sur ~6 mois de 2026).
START_DATE = date(2026, 1, 8)
TODAY = date(2026, 6, 18)

#: Préfixe du `bill_no` (Supplier Invoice No). Sert aussi à détecter les lots
#: existants pour reprendre la numérotation au bon endroit.
BILL_NO_PREFIX = "DEMO-"

HERE = os.path.dirname(os.path.abspath(__file__))
BANK_ACCOUNT_JSON = os.path.join(HERE, "bank_account.json")
PURCHASE_INVOICE_JSON = os.path.join(HERE, "purchase_invoice.json")

#: Fichier-verrou pour empêcher deux ``load`` simultanés (cause #1 des
#: ``Lock wait timeout`` quand plusieurs Ctrl-C ratés laissent des zombies).
LOAD_LOCK_FILE = os.path.join(HERE, ".load.lock")

#: Back-off (en secondes) entre les tentatives d'insertion en cas de verrou.
_RETRY_BACKOFF = [1, 3, 5, 10, 15]


def _rib(sup):
    """Reconstruit le RIB UEMOA complet (24 caractères) d'un fournisseur."""
    rib = RIB_BANK + RIB_BRANCH + sup["account"] + sup["key"]
    assert len(rib) == 24, f"RIB invalide ({len(rib)} car.) pour {sup['supplier']}"
    return rib


def build_bank_accounts():
    """Liste de docs Bank Account (un par fournisseur), calqués sur ORANGE CI."""
    docs = []
    for sup in SUPPLIERS:
        name = sup["supplier"]
        docs.append({
            "doctype": "Bank Account",
            "account_name": f"BOA - {name} - {BANK}",
            "bank": BANK,
            "party_type": "Supplier",
            "party": name,
            "is_default": 1,
            "custom_rib": _rib(sup),
            "custom_bank_code": BANK_CODE,
            "custom_swift_code": SWIFT,
        })
    return docs


def build_invoices(count=INVOICE_COUNT, bill_no_start=1):
    """Liste de docs Purchase Invoice payables, répartis sur les fournisseurs.

    Paramètres
    ----------
    count : int
        Nombre de factures à générer.
    bill_no_start : int
        Numéro de départ pour ``bill_no`` (``DEMO-XXXX``). Permet de générer
        des lots additionnels sans collision avec un lot précédent — ERPNext
        rejette deux ``Supplier Invoice No`` identiques. La graine du RNG et
        l'étalement des dates sont également décalés pour rester cohérents.
    """
    rng = random.Random(20260618 + bill_no_start)  # graine décalée par lot
    docs = []
    for i in range(count):
        sup = SUPPLIERS[i % len(SUPPLIERS)]["supplier"]
        # On garde l'étalement temporel relatif au tout premier lot
        day_offset = int((bill_no_start - 1 + i) * 1.3)
        posting = min(START_DATE + timedelta(days=day_offset), TODAY)
        due = posting + timedelta(days=30)
        # 1 à 3 lignes d'items de service tirées du pool
        lines = rng.sample(ITEMS, rng.randint(1, 3))
        items = []
        for it in lines:
            qty = rng.choice([1, 2, 3, 5, 7, 10])
            items.append({
                "item_code": it["item_code"],
                "qty": qty,
                "uom": it["uom"],
                "conversion_factor": 1,
                "rate": it["rate"],
                "expense_account": EXPENSE_ACCOUNT,
                "cost_center": COST_CENTER,
            })
        docs.append({
            "doctype": "Purchase Invoice",
            "company": COMPANY,
            "supplier": sup,
            "posting_date": posting.isoformat(),
            "set_posting_time": 1,
            "due_date": due.isoformat(),
            "bill_no": f"{BILL_NO_PREFIX}{bill_no_start + i:04d}",
            "bill_date": posting.isoformat(),
            "currency": CURRENCY,
            "conversion_rate": 1,
            "buying_price_list": PRICE_LIST,
            "credit_to": CREDIT_TO,
            "update_stock": 0,
            "items": items,
        })
    return docs


def dump(count=INVOICE_COUNT, bill_no_start=1):
    """Écrit les deux fichiers JSON de fixtures (aucune base requise)."""
    bank_accounts = build_bank_accounts()
    invoices = build_invoices(count, bill_no_start=bill_no_start)
    with open(BANK_ACCOUNT_JSON, "w", encoding="utf-8") as fh:
        json.dump(bank_accounts, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    with open(PURCHASE_INVOICE_JSON, "w", encoding="utf-8") as fh:
        json.dump(invoices, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"{len(bank_accounts)} Bank Account  -> {BANK_ACCOUNT_JSON}")
    print(f"{len(invoices)} Purchase Invoice -> {PURCHASE_INVOICE_JSON}")
    return {"bank_accounts": len(bank_accounts), "invoices": len(invoices)}


# ---------------------------------------------------------------------------
# Chargement en base — robuste aux verrous MariaDB + verrou inter-process
# ---------------------------------------------------------------------------

def _is_lock_error(exc):
    """Vrai si l'exception est un verrou MariaDB transitoire (1205 / 1213)."""
    msg = str(exc)
    return ("1205" in msg or "1213" in msg
            or "Lock wait timeout" in msg or "Deadlock" in msg)


def _set_session_lock_timeout(seconds):
    """Réduit innodb_lock_wait_timeout côté session : retry rapide sur verrou."""
    import frappe
    try:
        frappe.db.sql(f"SET SESSION innodb_lock_wait_timeout = {int(seconds)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ Impossible de régler innodb_lock_wait_timeout : {exc}")


def _show_lock_blockers():
    """Affiche les transactions qui bloquent les nôtres (debug).

    Nécessite le privilège MariaDB ``PROCESS``. Silencieux sinon.
    """
    import frappe
    try:
        rows = frappe.db.sql(
            """
            SELECT
              r.trx_mysql_thread_id  AS waiter,
              TIMESTAMPDIFF(SECOND, r.trx_wait_started, NOW()) AS waited_s,
              b.trx_mysql_thread_id  AS blocker,
              b.trx_state            AS blocker_state,
              TIMESTAMPDIFF(SECOND, b.trx_started, NOW()) AS blocker_age_s,
              LEFT(COALESCE(b.trx_query, ''), 120) AS blocker_query
            FROM information_schema.INNODB_LOCK_WAITS w
            JOIN information_schema.INNODB_TRX r ON r.trx_id = w.requesting_trx_id
            JOIN information_schema.INNODB_TRX b ON b.trx_id = w.blocking_trx_id
            """,
            as_dict=True,
        )
    except Exception:  # noqa: BLE001
        return  # privilège PROCESS absent → silencieux
    for r in rows:
        print(f"     ↳ thread {r['waiter']} attend depuis {r['waited_s']}s, "
              f"bloqué par thread {r['blocker']} "
              f"(état={r['blocker_state']}, trx depuis {r['blocker_age_s']}s)")
        if r.get("blocker_query"):
            print(f"        requête : {r['blocker_query']}")


def _kill_stale_locks_internal():
    """Tue les connexions avec une **transaction InnoDB** ouverte depuis > 30s.

    Nécessite le privilège MariaDB ``PROCESS``. Sinon, no-op silencieux.
    """
    import frappe
    try:
        current_id = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]
        rows = frappe.db.sql(
            """
            SELECT trx_mysql_thread_id AS id,
                   TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS age_s,
                   trx_state
            FROM information_schema.INNODB_TRX
            WHERE trx_mysql_thread_id != %s
              AND TIMESTAMPDIFF(SECOND, trx_started, NOW()) > 30
            """,
            (current_id,),
            as_dict=True,
        )
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "1227" in msg or "Access denied" in msg or "PROCESS" in msg:
            print("  ⚠ Pas de privilège PROCESS sur MariaDB — kill auto "
                  "désactivé. Utiliser 'bench --site <site> mariadb' "
                  "(SHOW FULL PROCESSLIST ; KILL <id>) si besoin.")
            return []
        raise

    killed = []
    for row in rows:
        try:
            frappe.db.sql(f"KILL {int(row['id'])}")
            killed.append(row["id"])
            print(f"  KILL {row['id']} "
                  f"(transaction ouverte depuis {row['age_s']}s, "
                  f"état={row['trx_state']})")
        except Exception as exc:  # noqa: BLE001
            print(f"  KILL {row['id']} a échoué : {exc}")
    return killed


def kill_stale_locks():
    """Tue les connexions MariaDB avec transaction zombie sur la base courante.

    À lancer :
        bench --site <site> execute paybridge.boa.demo.demo_data.kill_stale_locks
    """
    killed = _kill_stale_locks_internal()
    print(f"Connexions zombies tuées : {killed}")
    return killed


def _detect_next_bill_no_start():
    """Renvoie le prochain numéro libre pour ``bill_no = DEMO-XXXX``.

    Scrute ``tabPurchase Invoice`` et trouve le plus grand ``DEMO-XXXX`` déjà
    existant (toutes docstatus confondues, pour éviter les collisions sur des
    Draft / Cancelled qui occupent quand même le ``Supplier Invoice No``).
    Renvoie 1 si aucun lot précédent n'est trouvé.
    """
    import frappe
    row = frappe.db.sql(
        """
        SELECT bill_no
        FROM `tabPurchase Invoice`
        WHERE bill_no LIKE %s
        ORDER BY CAST(SUBSTRING(bill_no, %s) AS UNSIGNED) DESC
        LIMIT 1
        """,
        (BILL_NO_PREFIX + "%", len(BILL_NO_PREFIX) + 1),
    )
    if not row or not row[0][0]:
        return 1
    try:
        return int(row[0][0][len(BILL_NO_PREFIX):]) + 1
    except (ValueError, IndexError):
        return 1


def _set_site_config_key(key, value):
    """Met à jour une clé du site_config.json — robuste aux versions Frappe.

    Frappe a déplacé ``update_site_config`` plusieurs fois ; on essaie les
    emplacements connus, et on tombe en dernier recours sur une écriture
    directe du fichier.
    """
    import frappe
    # 1) emplacement moderne (Frappe v14+)
    try:
        from frappe.installer import update_site_config  # type: ignore
        update_site_config(key, value, validate=False)
        return
    except (ImportError, AttributeError):
        pass
    # 2) emplacement historique (anciennes versions)
    try:
        from frappe.utils import update_site_config  # type: ignore
        update_site_config(key, value)
        return
    except (ImportError, AttributeError):
        pass
    # 3) Fallback : écriture directe du JSON
    import json as _json
    config_path = frappe.get_site_path("site_config.json")
    with open(config_path, "r", encoding="utf-8") as fh:
        conf = _json.load(fh)
    if value is None:
        conf.pop(key, None)
    else:
        conf[key] = value
    with open(config_path, "w", encoding="utf-8") as fh:
        _json.dump(conf, fh, indent=1, ensure_ascii=False)
        fh.write("\n")


def _pause_scheduler():
    """Met le scheduler en pause via ``site_config.json``.

    ``pause_scheduler`` est une clé du site_config (lue par les workers),
    PAS un champ de System Settings. Retourne (was_paused, ok).
    ⚠ Les workers déjà en cours ne sont PAS interrompus : pour une isolation
    totale, stopper ``bench start`` (Ctrl-C) AVANT de lancer ``load``.
    """
    import frappe
    try:
        was_paused = bool(frappe.utils.cint(
            frappe.conf.get("pause_scheduler") or 0
        ))
        if not was_paused:
            _set_site_config_key("pause_scheduler", 1)
            frappe.local.conf.pause_scheduler = 1
        return was_paused, True
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ Impossible de mettre le scheduler en pause : {exc}")
        return False, False


def _resume_scheduler(was_paused, ok):
    """Restaure l'état précédent du scheduler."""
    if not ok or was_paused:
        return  # rien à restaurer
    import frappe
    try:
        _set_site_config_key("pause_scheduler", 0)
        frappe.local.conf.pause_scheduler = 0
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ Impossible de redémarrer le scheduler : {exc}")


def _acquire_load_lock():
    """Verrou inter-process : empêche deux ``load`` simultanés.

    Renvoie un file handle à garder ouvert pendant toute la durée du load
    (le verrou est relâché à la fermeture du fd). Lève RuntimeError si une
    autre instance tient déjà le verrou.
    """
    fh = open(LOAD_LOCK_FILE, "w", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise RuntimeError(
            "Une autre instance de demo_data.load tient déjà le verrou "
            f"({LOAD_LOCK_FILE}). Tuer les zombies :\n"
            "    pkill -9 -f 'bench_helper frappe.*execute.*demo_data'\n"
            "puis vérifier avec :\n"
            "    ps -ef | grep -E 'bench_helper.*demo_data' | grep -v grep"
        )
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return fh


def _release_load_lock(fh):
    """Relâche et supprime le fichier-verrou."""
    if fh is None:
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        fh.close()
    except OSError:
        pass
    try:
        os.remove(LOAD_LOCK_FILE)
    except OSError:
        pass


def load(submit=True, count=INVOICE_COUNT, max_retries=5,
         stop_on_consecutive_errors=10, kill_stale_first=False,
         lock_wait_timeout_s=5, pause_scheduler=True,
         bill_no_start=None):
    """Crée les comptes bancaires puis insère (et soumet) les factures.

    À lancer :
        bench --site <site> execute paybridge.boa.demo.demo_data.load

    Avant de lancer, s'assurer qu'AUCUN autre process ne touche au site :
        # mode dev (honcho / bench start) :
        Ctrl-C dans le terminal `bench start`
        pkill -9 -f "bench_helper frappe.*execute.*demo_data"   # zombies

        # mode prod (supervisor) :
        sudo supervisorctl stop all

    Un fichier-verrou (``demo_data/.load.lock``) refuse une exécution
    concurrente : si un précédent ``bench execute`` n'a pas été tué
    proprement, l'erreur sera explicite.

    Paramètres
    ----------
    submit : bool                       (défaut True)
    count : int                         (défaut 120)
    max_retries : int                   (défaut 5)
    stop_on_consecutive_errors : int    (défaut 10)
    kill_stale_first : bool             (défaut False ; nécessite PROCESS)
    lock_wait_timeout_s : int           (défaut 5 ; vs 50 MariaDB)
    pause_scheduler : bool              (défaut True ; via site_config.json)
    bill_no_start : int | None          (défaut None → auto-détection)
        Numéro de départ pour ``bill_no = DEMO-XXXX``. Si ``None``, le script
        détecte automatiquement le dernier ``DEMO-XXXX`` existant en base et
        démarre à ``last + 1`` — pratique pour ajouter un lot sans collision.

    Idempotent pour les comptes bancaires. Les factures le sont aussi tant
    que ``bill_no_start`` est laissé en auto-détection. Pour repartir de
    zéro, supprimer d'abord ``bill_no LIKE 'DEMO-%'``.
    """
    import time
    import frappe

    # --- Coercition des arguments (bench execute passe des strings) ---------
    def _b(v):
        return frappe.utils.cint(v) if isinstance(v, str) else bool(v)

    submit = _b(submit)
    kill_stale_first = _b(kill_stale_first)
    pause_scheduler = _b(pause_scheduler)
    count = int(count)
    max_retries = max(1, int(max_retries))
    stop_on_consecutive_errors = int(stop_on_consecutive_errors)
    lock_wait_timeout_s = int(lock_wait_timeout_s)
    if bill_no_start is not None:
        bill_no_start = int(bill_no_start)

    result = {"bank_accounts": [], "invoices": [], "errors": [], "killed": []}

    # --- Verrou inter-process (cause #1 des Lock wait timeout) --------------
    try:
        lock_fh = _acquire_load_lock()
    except RuntimeError as exc:
        print(f"❌ {exc}")
        result["errors"].append(str(exc))
        return result
    print(f"  🔒 Verrou load acquis (PID {os.getpid()})")

    was_paused, pause_ok = False, False
    try:
        # --- État propre + timeout court + pause scheduler ------------------
        try:
            frappe.db.rollback()
        except Exception:  # noqa: BLE001
            pass

        _set_session_lock_timeout(lock_wait_timeout_s)

        if kill_stale_first:
            result["killed"] = _kill_stale_locks_internal()

        if pause_scheduler:
            was_paused, pause_ok = _pause_scheduler()
            if pause_ok:
                print("  ⏸  Scheduler en pause pendant le chargement")

        # --- Auto-détection du point de départ DEMO-XXXX --------------------
        if bill_no_start is None:
            bill_no_start = _detect_next_bill_no_start()
            print(f"  ℹ bill_no_start auto-détecté : "
                  f"{BILL_NO_PREFIX}{bill_no_start:04d}")
        else:
            print(f"  ℹ bill_no_start (forcé) : "
                  f"{BILL_NO_PREFIX}{bill_no_start:04d}")

        def _insert_with_retry(doc_dict, do_submit=False, label=""):
            """Insère (+ soumet) + commit ; rollback + back-off sur verrou."""
            last_exc = None
            for attempt in range(max_retries):
                try:
                    doc = frappe.get_doc(doc_dict)
                    doc.insert(ignore_permissions=True)
                    if do_submit:
                        doc.submit()
                    frappe.db.commit()
                    return doc.name
                except Exception as exc:  # noqa: BLE001
                    try:
                        frappe.db.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    last_exc = exc
                    if _is_lock_error(exc) and attempt + 1 < max_retries:
                        wait = _RETRY_BACKOFF[
                            min(attempt, len(_RETRY_BACKOFF) - 1)
                        ]
                        print(f"  [retry {attempt + 1}/{max_retries}] {label} "
                              f"verrou — attente {wait}s")
                        _show_lock_blockers()
                        time.sleep(wait)
                        continue
                    raise
            raise last_exc

        # --- Comptes bancaires ----------------------------------------------
        for ba in build_bank_accounts():
            if frappe.db.exists("Bank Account",
                                {"party": ba["party"], "bank": ba["bank"]}):
                continue
            try:
                name = _insert_with_retry(ba, do_submit=False,
                                          label=f"BankAccount {ba['party']}")
                result["bank_accounts"].append(name)
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"Bank Account {ba['party']} : {exc}")

        # --- Factures -------------------------------------------------------
        consecutive_errors = 0
        invoices = build_invoices(count, bill_no_start=bill_no_start)
        for idx, pi in enumerate(invoices, start=1):
            label = f"PI#{idx:03d} {pi['supplier']} ({pi['bill_no']})"
            try:
                name = _insert_with_retry(pi, do_submit=submit, label=label)
                result["invoices"].append(name)
                consecutive_errors = 0
                if idx % 10 == 0:
                    print(f"  ✓ {idx}/{count} factures créées "
                          f"(dernière : {name} / {pi['bill_no']})")
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(
                    f"Purchase Invoice {pi['supplier']} "
                    f"[{pi['bill_no']}] : {exc}"
                )
                consecutive_errors += 1
                if consecutive_errors >= stop_on_consecutive_errors:
                    msg = (f"Arrêt anticipé après {consecutive_errors} erreurs "
                           f"consécutives. Vérifier :\n"
                           f"  ps -ef | grep -E 'bench_helper.*demo_data' "
                           f"| grep -v grep\n"
                           f"  ps -ef | grep -E 'frappe (serve|schedule|worker)' "
                           f"| grep -v grep\n"
                           f"…et tuer ce qui traîne avec 'pkill -9'.")
                    result["errors"].append(msg)
                    print(msg)
                    break

    finally:
        if pause_scheduler:
            _resume_scheduler(was_paused, pause_ok)
            if pause_ok and not was_paused:
                print("  ▶  Scheduler ré-activé")
        _release_load_lock(lock_fh)
        print("  🔓 Verrou load relâché")

    print(f"Connexions zombies tuées   : {len(result['killed'])}")
    print(f"Comptes bancaires créés    : {len(result['bank_accounts'])}")
    print(f"Factures créées            : {len(result['invoices'])} "
          f"(submit={submit})")
    if result["errors"]:
        print(f"Erreurs ({len(result['errors'])}) — 1ère : {result['errors'][0]}")
    return result


if __name__ == "__main__":
    dump()