# Custom Fields — Module BOA / Domibus

Liste complète des champs personnalisés ajoutés par le module **BOA** de l'app
PayBridge. Ils sont versionnés via la fixture `paybridge/fixtures/custom_field.json`
(`module = "BOA"`), donc réinstallables avec `bench migrate`.

**Principe directeur : source unique = l'enregistrement `Bank Account` ERPNext.**
Toutes les coordonnées bancaires d'un tiers se saisissent **à un seul endroit**,
son compte bancaire. On s'appuie au maximum sur les champs standard ; un custom
field n'est créé que lorsqu'aucun équivalent natif n'existe.

**Total : 4 custom fields fonctionnels (+ 1 section break), sur 2 DocTypes ERPNext.**

---

## 📍 DocType `Bank Account` (3 champs fonctionnels + 1 section)

Emplacement : nouvelle section repliable **« Coordonnées BOA / Domibus »**,
insérée **après le champ `branch_code`**.

| # | Fieldname | Type | Label | Inséré après | Rôle |
|---|-----------|------|-------|--------------|------|
| 1 | `custom_boa_section` | Section Break | Coordonnées BOA / Domibus | `branch_code` | En-tête de section (repliable) |
| 2 | `custom_rib` | Data | RIB | `custom_boa_section` | RIB / BBAN UEMOA complet (24 car. : code banque 5 + guichet 5 + compte 12 + clé 2). **Source unique** du virement BOA. Validé à l'enregistrement. Si vide → dérivé de l'IBAN |
| 3 | `custom_bank_code` | Data | Code banque | `custom_rib` | Code banque (5 car., colonne « Code bban » du H2H). **Override facultatif** : si vide → dérivé de `custom_rib[:5]`. À ne saisir que si le code banque diffère des 5 premiers car. du RIB. Normalisé/contrôlé à l'enregistrement |
| 4 | `custom_swift_code` | Data | Code SWIFT / BIC | `custom_bank_code` | BIC de la banque du bénéficiaire (colonne 7 du H2H). Saisi sur le compte car on paie des banques variées. Si vide → hérité de `Bank.swift_number` |

> **RIB ≠ numéro de compte.** Le champ standard `bank_account_no` reste réservé au
> **numéro de compte** générique (cœur ERPNext : rapprochement, Payment Entry) et
> n'est pas lu par PayBridge. Le **code banque** (colonne « Code bban » du fichier
> H2H) est par défaut **dérivé** (`custom_rib[:5]`) ; `custom_bank_code` permet de
> le **forcer** lorsqu'il ne correspond pas aux 5 premiers caractères du RIB.

---

## 📍 DocType `Employee` (1 champ)

Emplacement : onglet **Salary Information**, inséré **après le champ `bank_ac_no`**
(N° de compte bancaire).

| # | Fieldname | Type | Label | Inséré après | Options | Rôle |
|---|-----------|------|-------|--------------|---------|------|
| 3 | `custom_boa_bank_account` | Link | Compte Bancaire (Virement BOA) | `bank_ac_no` | → `Bank Account` | Lie le salarié à son `Bank Account` (source unique des coordonnées). Nécessaire : ERPNext n'offre pas de lien natif Employee → Bank Account (contrairement à `Supplier.default_bank_account`) |

---

## Logique de résolution des coordonnées bancaires

Chaque tiers pointe vers **un** `Bank Account`, d'où tout est dérivé
(`paybridge/boa/domibus/bank_utils.py`) :

| Donnée H2H | Source (sur le `Bank Account`) |
|------------|--------------------------------|
| Compte à débiter/créditer (RIB) | `custom_rib` (repli : `iban` → `iban[4:]`) |
| Code bban (= code banque) | `custom_bank_code` (override) sinon **dérivé** = `custom_rib[:5]` |
| Code SWIFT (BIC) | `custom_swift_code` (override), sinon `Bank.swift_number` |
| Bénéficiaire / titulaire | `account_name` |

Associations tiers → `Bank Account` :

- **Fournisseur** (Purchase Invoice) → `Supplier.default_bank_account` (standard)
- **Employé** (Expense Claim / Salary Slip) → `Employee.custom_boa_bank_account`
- **Compte émetteur (débit)** → `BOA Domibus Settings.debit_bank_account` (lien),
  à défaut le compte société par défaut d'ERPNext
- **Repli générique** → `Bank Account` lié par `{party_type, party, is_default}`

---

## Notes

- Aucun custom field sur `Supplier` (on réutilise `default_bank_account` natif).
- Aucun custom field sur les documents sources (`Purchase Invoice`, `Expense
  Claim`, `Salary Slip`).
- Le compte émetteur (BOA Domibus Settings) n'a plus de champs `debit_*` manuels :
  ses coordonnées proviennent du `Bank Account` lié (`debit_bank_account`).
- Le RIB et le code banque sont normalisés et contrôlés à l'enregistrement du
  `Bank Account` (`paybridge/boa/bank_account.py::validate_rib` et
  `::validate_bank_code`, câblés via `doc_events`).
- Champ historique retiré (migration `paybridge/patches/v0/migrate_rib_and_drop_legacy_fields.py`) :
  `Bank Account.custom_bban_code` → migré vers `custom_rib` (priorité au RIB
  réellement transmis : `bank_account_no` → `iban[4:]` → `custom_bban_code`).
  `custom_swift_code` est conservé.
