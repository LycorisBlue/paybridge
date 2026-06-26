# Données d'exemple — flux de paiement BOA

Fixtures de démonstration pour tester le module BOA (sélection de factures à
payer, génération du fichier H2H, envoi Domibus).

## Contenu

| Fichier | Contenu |
|---|---|
| `demo_data.py` | Générateur + chargeur (source de vérité) |
| `bank_account.json` | 5 comptes bancaires BOA — un par fournisseur, calqués sur le compte réel d'**ORANGE CI** (`custom_rib`, `custom_bank_code`, `custom_swift_code`) |
| `purchase_invoice.json` | **120** Purchase Invoice payables, réparties sur 5 fournisseurs (ORANGE CI, YANGO, SUNU ASSURANCE, AIRFRANCE, ACLYK) et des items de service réels |

Toutes les valeurs (société `AMOAMAN & ASSOCIES`, comptes `4011`/`6011`,
fournisseurs, items, banque `Bank of Africa Côte d'Ivoire`) proviennent du site
`prod.amoaman.com`. Le RIB d'ORANGE CI est réel ; les 4 autres sont fictifs mais
au format UEMOA valide (24 car.) et chez la même banque (code `01089`, BIC
`ORBKBFOG`).

## Charger les données (insert **+ submit**)

```bash
cd ~/bench-dev
bench --site <site> execute paybridge.boa.demo.demo_data.load
```

C'est le **seul** moyen d'obtenir des factures *payables* : la soumission
(`docstatus = 1`, `outstanding_amount > 0`) est requise par le module BOA. Le
chargeur est idempotent pour les comptes bancaires (il ignore ceux déjà créés —
ex. ORANGE CI qui existe déjà).

> ⚠️ Ne pas charger sur la production. Utiliser un site de test/sandbox.
> `bench import-doc purchase_invoice.json` n'insère qu'en **brouillon** et ne
> soumet pas → factures non payables.

## Régénérer les JSON (sans base de données)

```bash
cd ~/bench-dev/apps/paybridge
~/bench-dev/env/bin/python -m paybridge.boa.demo.demo_data
```

Ces fixtures ne sont **pas** enregistrées dans `hooks.fixtures` : elles ne
seront jamais importées automatiquement au `bench migrate`.
