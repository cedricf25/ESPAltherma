---
name: analyse-pac
description: Analyse la régulation de la pompe à chaleur Daikin Altherma (boucle compresseur, lois d'eau plancher et radiateurs, modulation, ΔT, débit, COP, cycles, ECS, dégivrages) à partir de l'historique Home Assistant, puis propose des réglages argumentés. À utiliser quand l'utilisateur demande d'analyser sa PAC, son chauffage, sa régulation, son COP, ses lois d'eau, ou s'il trouve un comportement bizarre (« la PAC ne monte pas », « l'eau n'est pas à la consigne »…).
argument-hint: "[durée, ex. 24h, 3j, ou « depuis 18h »]"
---

# Analyse de la régulation de la PAC

Installation : Daikin Altherma 3 H HT 18 kW, `EPRA18DV3` + `ETVZ16S23D6V` **bizone**, lue par
ESPAltherma et publiée dans Home Assistant. Voir le `CLAUDE.md` du dépôt pour le détail matériel.

## Règles

- **Lecture seule.** Ne jamais modifier les réglages de la PAC ni écrire dans Home Assistant pendant
  une analyse. Les réglages se proposent, l'utilisateur les applique sur l'interface de la PAC.
- **Vérifier `reglages.json` avant de conclure** sur les lois d'eau, la modulation ou le ΔT.
  Si l'utilisateur annonce un changement de réglage, mettre ce fichier à jour (avec `_maj`).
- Répondre en français, en commençant par le verdict, puis les chiffres qui le justifient.

## 1. Collecter

```bash
python3 -I /home/cedric/vscode/perso/ESPAltherma/.claude/skills/analyse-pac/analyse_pac.py --heures 24
python3 -I /home/cedric/vscode/perso/ESPAltherma/.claude/skills/analyse-pac/analyse_pac.py --debut 2026-10-08T18:00 --fin 2026-10-08T20:00
```

Choisir la période selon la question : **2-6 h** pour un comportement ponctuel (boucle compresseur),
**3-7 jours** pour juger une loi d'eau ou un COP (il faut plusieurs températures extérieures et une
maison stabilisée ; une remise en route à froid fausse les 24 premières heures).

Le script renvoie un JSON (pas de 1 min, quantiles p10/med/p90). Vérifier d'abord `couverture_%` :
une entité sous ~80 % rend la section correspondante fragile — le dire.

Pour creuser un instant précis, l'historique brut reste accessible : `~/.ha-token.env`
(`HA_URL`, `HA_TOKEN`), `GET /api/history/period/<début>?end_time=<fin>&filter_entity_id=…`
— **toujours passer `end_time`**, sinon HA tronque à 24 h après le début. L'entité
`sensor.althermasensors` porte toutes les valeurs brutes en attributs (lourd : 1 point / 6 s).

## 2. Interpréter

### Boucle compresseur (`compresseur`, `boucle_frigo`)

La PAC ne régule pas l'eau directement : elle calcule une **condensation visée** (`tc_cib`) à partir
de l'écart eau/consigne, et module le compresseur pour que la **condensation mesurée** (`tc`, saturation
haute pression) la rejoigne.

- `tc < tc_cib` → il manque de la chaleur, le compresseur accélère ; `tc > tc_cib` → il ralentit.
- Minimum mécanique : **20 Hz**. En dessous des besoins, il ne peut que s'arrêter.
- **Épisode « bloqué au minimum »** (`episodes_bloque_au_min`) : déjà observé le 2026-10-08. L'eau
  dépasse sa consigne → descente à 20 Hz → le détendeur se ferme (278 → 126 pas), le fluide
  s'accumule au condenseur (sous-refroidissement 4 → 11 K), la condensation mesurée monte au-dessus
  de la cible alors que l'eau baisse. Le compresseur reste au minimum jusqu'à ce que le détendeur se
  rouvre (~20-30 min). **Transitoire frigorifique, pas un problème de loi d'eau.** Alerter seulement
  s'il dure plus d'une heure, se répète à chaque cycle, ou si l'eau décroche de plus de 5 K.
- `temps_au_minimum_%` élevé et `demarrages` nombreux en mi-saison = PAC surdimensionnée pour le
  besoin du moment : la baisse de la loi d'eau (point doux) est le premier levier.
- Cycles courts : moins de ~20 min de marche ou plus de ~1,5 démarrage par heure de marche.
- Refoulement durablement > 90 °C ou sous-refroidissement > 15 K hors transitoire : à signaler
  (charge de fluide, détendeur) — c'est du ressort d'un frigoriste, ne pas proposer de réglage.

### Lois d'eau et modulation (`loi_eau`, `par_temperature_exterieure`)

- La PAC produit à la **consigne la plus haute**, celle des **radiateurs** (`lw_add`, zone additionnelle) ;
  la vanne M1S redescend l'eau pour le **plancher** (`lw_main`, zone principale). **C'est donc la loi
  radiateurs qui fixe le COP.** Le plancher n'a d'effet sur le compresseur que via le besoin.
- `modulation_*` = consigne réelle − loi théorique. Proche de 0 → la modulation (±5 K réglée) n'agit
  pas ; nettement positive → la Madoka réclame plus que la loi (loi trop basse ou consigne pièce
  haute) ; négative → loi trop haute, la modulation rattrape.
- Juger une loi **par tranche de température extérieure** : la pente se corrige au point froid
  (−10 °C), le décalage en mi-saison au point doux (+20 °C). Une tranche ne renseigne que sur le
  point de la droite le plus proche.
- Confort : `etage_moins_consigne` (thermostat 1er étage → radiateurs) et `piece_madoka_moins_consigne`
  (Madoka, située dans la zone plancher — confirmé par le propriétaire le 2026-10-08).
- Indices dans `indices` : ce sont des pistes calculées par seuils, à recouper, jamais des verdicts.

Recommandations de loi d'eau :
- **Un seul point à la fois, par pas de 1-2 °C**, puis **3 jours d'observation** avant le suivant.
- Pièces au-dessus de la consigne ou eau durablement sous la loi avec confort tenu → baisser.
- Pièces sous la consigne alors que l'eau suit la loi → monter.
- Ordre de grandeur de gain : ~2-3 % de COP par °C d'eau en moins. Références constructeur :
  SCOP 4,51 à 35 °C d'eau, 3,58 à 55 °C.
- Repères : plancher 30-35 °C à −10 °C ; radiateurs basse température 45-55 °C. La loi radiateurs
  actuelle (60 °C à −10 °C) est une loi de chaudière : piste d'amélioration prioritaire cet hiver.

### ΔT et débit (`delta_t`)

- ΔT côté PAC (R2T − R4T) faible (2-3 K) et pompe principale au minimum (20 %) : normal ici. Les
  pompes de zone imposent le débit (~32 l/min) ; le ΔT cible secondaire (12) est inatteignable et
  n'a pas d'effet néfaste. Un ΔT faible favorise plutôt le COP.
- ΔT plancher (Shelly) : 3-6 K normal ; > 8 K → débit plancher insuffisant (pompe de zone, embouage) ;
  < 2 K durablement → dalle saturée, loi plancher sans doute trop haute.
- Le débit mesuré (B2L) est celui du circuit PAC. Les débits par zone et la vitesse des pompes de
  zone ne sont pas disponibles.

### COP et énergie (`energie`)

- `cop_chauffage` est pondéré par l'énergie (Σ chaleur / Σ électricité, Shelly EM), bien plus
  fiable que la moyenne du capteur `sensor.espaltherma_cop`.
- La chaleur est estimée par débit × (R1T − R4T) : imprécise quand le ΔT est faible (±0,5 K de sonde
  = ±20 % à 2 K). Juger le COP sur plusieurs jours, et l'inscrire dans la tendance par tranche
  d'extérieur plutôt que sur une valeur isolée.
- Comparer à l'attendu selon `temp_eau_moy_en_marche` (interpoler entre 4,5 à 35 °C et 3,6 à 55 °C,
  plus haut par temps doux).

### ECS, dégivrages, appoint

- Ballon (R5T) vs consigne ECS ; le passage en ECS se voit au mode et à la montée de R5T.
- Dégivrages : normaux sous ~7 °C extérieur et par temps humide ; fréquents au-dessus de 10 °C =
  anormal.
- Appoint électrique (BUH) enclenché hors grand froid ou dégivrage : à signaler (loi trop haute pour
  la puissance, consigne ECS trop haute, réglage d'équilibre).

## Pièges connus

- Le départ plancher de l'ESP (`sensor.espaltherma_temp_r7t`, registre `0x64,10`) est quantifié par
  pas de 2,56 °C : utiliser les sondes Shelly (`sensor.temperature_depart_sol_temperature`,
  `sensor.temperature_retour_sol_temperature`).
- La position de la vanne M1S n'est pas publiée par la machine.
- `sensor.espaltherma_target_delta_primaire` = ΔT cible **secondaire** chauffage (12),
  `…_secondaire` = ΔT de **refroidissement** (3) : les `entity_id` sont trompeurs.
- `sensor.temperature_depart_eau_temperature` s'appelle « retour eau » dans HA mais mesure un départ.
- **Aucun thermostat ne régule l'étage** (l'ancien thermostat Netatmo a été supprimé le 2026-10-08).
  Côté PAC, la demande de la zone radiateurs (« Thermostat 2 ») est à ON en permanence : les
  radiateurs ne sont régulés que par la loi d'eau (la modulation Madoka ne concerne que le plancher).
  Le script mesure l'étage à la Mezzanine (`sensor.thermometre_mezzanine_temperature`) et le compare
  à `confort_etage` de `reglages.json` — un repère de confort, pas une consigne. D'autres
  thermomètres existent par pièce (`sensor.thermometre_*`, `sensor.meter_*`).
- Heures : HA renvoie de l'UTC, le script affiche l'heure locale.

## 3. Restituer

1. Verdict en une ou deux phrases (tout va bien / point d'attention / réglage à faire).
2. Un tableau court des chiffres clés de la période.
3. Pour chaque point d'attention : ce qu'on observe → le mécanisme → ce qu'il faut faire ou surveiller.
4. Réglages proposés : zone, point de la loi, valeur actuelle → valeur proposée, quand réévaluer.
   Si les données ne suffisent pas (période trop courte, maison pas stabilisée), le dire et proposer
   la période d'observation adaptée plutôt qu'un réglage.
