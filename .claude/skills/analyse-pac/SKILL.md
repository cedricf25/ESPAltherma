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
  Un décalage (offset) se note en **historique** (`decalage: [{"depuis", "valeur"}]`), sans effacer
  les entrées précédentes, pour que les périodes antérieures restent jugées avec l'ancien décalage ;
  l'heure exacte se lit à la chute de `lw_main` / `lw_add` dans HA.
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

Puis lire `regime` : le propriétaire **coupe souvent la PAC et chauffe au bois**. Lois d'eau, pièces,
`par_temperature_exterieure` et `cop_chauffage` ne portent que sur les `heures_analysables` : PAC en
chauffe (`climate.room_temperature` = `heat`) depuis au moins `stabilisation_h` (24 h), hors
`periodes_bois_probable`. Sous ~12 h analysables, **ne pas juger les lois d'eau** : le dire, et
proposer d'attendre une période de chauffe PAC seule. La boucle compresseur, l'ECS et les dégivrages
restent jugeables sur toute la période. `cop_chauffage_toutes_heures` inclut les heures de remise en
route : à citer seulement faute de mieux, en le précisant.

Le bois n'est pas instrumenté : il est **déduit** quand le salon ou la salle à manger dépassent la
consigne Madoka de plus de `bois.seuil_k`. Une journée de soleil peut donner le même signe ; en cas
de doute, demander au propriétaire s'il a fait du feu.

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
- **Plancher relevé par temps froid** : quand l'évaporation passe sous 0 °C (en pratique sous ~3 °C
  dehors), l'unité extérieure ne descend plus à 20 Hz et reste vers **32 Hz** (~2,1 kW électriques),
  même quand la condensation dépasse sa cible de plusieurs K. Du 11 au 25/11/2026, quand
  `tc > tc_cib + 2` : 32 Hz en médiane entre 0 et 3 °C (2 % du temps de marche à 20 Hz, évaporation
  moyenne −2,7 °C), contre 20 Hz au-dessus de 3 °C (59 à 80 % du temps à 20 Hz, évaporation positive).
  Protection de la machine, non réglable. Conséquence : par temps froid, si la maison demande moins que
  ces 32 Hz, l'eau monte jusqu'au seuil d'arrêt (consigne radiateurs + ~3,5 K avec [9-04] à 2) et la
  PAC s'arrête 7-8 min (cas du 25/11/2026 à 05:45). Ce n'est pas un défaut de modulation ; des cycles
  d'environ 1 h restent tout à fait corrects.
- **Épisode « bloqué au minimum »** (`episodes_bloque_au_min`) : déjà observé le 2026-10-08. L'eau
  dépasse sa consigne → descente à 20 Hz → le détendeur se ferme (278 → 126 pas), le fluide
  s'accumule au condenseur (sous-refroidissement 4 → 11 K), la condensation mesurée monte au-dessus
  de la cible alors que l'eau baisse. Le compresseur reste au minimum jusqu'à ce que le détendeur se
  rouvre (~20-30 min). **Transitoire frigorifique, pas un problème de loi d'eau.** Alerter seulement
  s'il dure plus d'une heure, se répète à chaque cycle, ou si l'eau décroche de plus de 5 K.
- **Pic de protection** (`pics_protection`, observé le 2026-10-09 par ~6 °C ext.) : à 24-27 Hz le
  détendeur se ferme (< 90 pas) et l'évaporation passe sous 0 °C ; l'unité extérieure remonte alors
  d'office à ~32 Hz alors que `tc_cib` baisse, l'eau dépasse la consigne radiateurs de ~2,5 K et la
  PAC s'arrête (thermo OFF). Protection de la machine, sans gravité ; levier indirect : loi radiateurs
  plus basse. À signaler à l'installateur seulement s'il persiste par temps doux ou eau basse
  (`pics_a_6C_ou_plus`, `pics_suivis_d_un_arret_sous_7_min`). Du 11 au 18/11/2026 : 28 pics, dont 12 à
  6 °C ou plus ; le 14/11, évaporation jusqu'à −10 °C pour 6-7,6 °C dehors, détendeur à 46-48 pas.
- `temps_au_minimum_%` élevé et `demarrages` nombreux en mi-saison = PAC surdimensionnée pour le
  besoin du moment : la baisse de la loi d'eau (point doux) est le premier levier.
- Cycles courts : moins de ~20 min de marche ou plus de ~1,5 démarrage par heure de marche.
  Mécanisme observé le 2026-10-10 (11 °C dehors, loi radiateurs −6) : démarrage à 28 Hz, 20 Hz au
  bout de ~5 min, l'eau monte jusqu'à la consigne radiateurs **+2,2 K**, arrêt, puis **7 min d'arrêt
  fixes** (temporisation anti-court-cycle : l'eau repasse sous la consigne bien avant le redémarrage).
  Cycle de ~30 min, soit 2 démarrages/h. Leviers, du plus gratuit au plus coûteux : remonter la loi
  **plancher** tant qu'elle reste sous la consigne radiateurs (la production ne change pas, la dalle
  absorbe plus) ; augmenter la surmodulation **[9-04]** (menu [4.B], 1-4 °C, défaut 1 °C) ; laisser les
  vannes thermostatiques ouvertes. Baisser la loi radiateurs raccourcit les cycles (moins d'émission).
  Remonter la loi plancher n'a d'intérêt que si le bas n'est pas déjà au-dessus de la consigne.
  Lire `par_temperature_exterieure` (`demarrages_par_h_marche`, `cycles_moins_10_min`) et
  `eau_moins_consigne_rad_a_l_arret` : du 11 au 18/11/2026, 3,5 démarrages/h entre 12 et 15 °C contre
  1,1 ou moins sous 9 °C, arrêt à la consigne +2,5 K alors que [9-04] vaut 1.
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
- Confort (`pieces`, `plus_froide_*`) : chaque pièce est comparée à **sa** consigne — la consigne
  Madoka au rez-de-chaussée (Madoka, salon, salle à manger, tous en zone plancher), la consigne de
  la **vanne thermostatique** à l'étage (radiateurs, consignes dans `reglages.json`).
  - Pièce d'étage au-dessus de sa vanne → l'eau arrive trop chaude, la vanne freine : marge pour
    baisser la loi radiateurs. **C'est la pièce la plus en retard qui fixe le minimum de la loi**
    (`plus_froide_etage_moins_vanne`) ; si elle est au-dessus de sa vanne, toute la zone l'est.
  - La **Mezzanine** est en rôle `info` : radiateur sous-dimensionné, vanne à 22 °C jamais atteinte,
    et chauffée par l'air qui monte du bas. Ne jamais s'en servir pour juger la loi radiateurs.
  - Les consignes de vanne sont approximatives (« autour de 20-21 ») : quelques dixièmes d'écart ne
    signifient rien, raisonner sur la tendance.
- Indices dans `indices` : ce sont des pistes calculées par seuils, à recouper, jamais des verdicts.

Recommandations de loi d'eau :
- **Un seul point à la fois, par pas de 1-2 °C**, puis **3 jours d'observation** avant le suivant.
- Pièces au-dessus de la consigne ou eau durablement sous la loi avec confort tenu → baisser.
- Pièces sous la consigne alors que l'eau suit la loi → monter.
- Ordre de grandeur de gain : ~2-3 % de COP par °C d'eau en moins. Références constructeur :
  SCOP 4,51 à 35 °C d'eau, 3,58 à 55 °C.
- Repères : plancher 30-35 °C à −10 °C ; radiateurs basse température 45-55 °C. Comparer la loi
  radiateurs effective (droite + décalage en vigueur dans `reglages.json`) à ce repère : au-dessus de
  55 °C à −10 °C, c'est une loi de chaudière et la piste d'amélioration prioritaire.

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
- **Biais de sondes** : compresseur arrêté, pompe en marche, R1T lit 0,3-0,4 K de moins que R4T
  (`ecart_repos_r1t_r4t`, mesuré à chaque analyse sur les fins d'arrêt). Le COP brut est donc
  probablement sous-estimé d'environ 15 % à 2 K de ΔT, et il paraît à tort meilleur par grand froid,
  où le ΔT est plus grand. Citer `cop_chauffage` **et** `cop_chauffage_corrige` ; le corrigé reste
  une estimation (une perte réelle dans l'unité peut expliquer une part de l'écart).
- Comparer à l'attendu selon `temp_eau_moy_en_marche` (interpoler entre 4,5 à 35 °C et 3,6 à 55 °C,
  plus haut par temps doux).

### ECS, dégivrages, appoint

- Ballon (R5T) vs consigne ECS (`consigne_ecs` dans `reglages.json` : consigne de jour et de nuit).
  Le ballon ne chauffe qu'**une fois par nuit, vers 02 h 30** : après un gros soutirage
  (`chutes_ballon`), il reste froid jusque-là, même sous la consigne de jour (le 15/11/2026 : 47,6 →
  22,7 °C en soirée, réchauffé à 02 h 24).
- Les chauffes ECS sont détectées à la montée de R5T (`ecs.chauffes`) et exclues du chauffage.
- Dégivrages : normaux sous ~7 °C extérieur et par temps humide ; fréquents au-dessus de 10 °C
  (`au_dessus_de_10C`) = anormal. Ceux qui partent d'une évaporation nettement positive
  (`evaporation_positive_avant`), espacés de 6-9 h, ressemblent à un cycle périodique plutôt qu'à du
  givre : à surveiller sans alerter.
- Appoint électrique (BUH) enclenché hors grand froid ou dégivrage : à signaler (loi trop haute pour
  la puissance, consigne ECS trop haute, réglage d'équilibre).

## Pièges connus

- Le départ plancher de l'ESP (`sensor.espaltherma_temp_r7t`, registre `0x64,10`) est quantifié par
  pas de 2,56 °C : utiliser les sondes Shelly (`sensor.temperature_depart_sol_temperature`,
  `sensor.temperature_retour_sol_temperature`).
- La position de la vanne M1S n'est pas publiée par la machine.
- `sensor.espaltherma_operation` reste à « Heating » pendant l'ECS : ne pas s'en servir pour la
  repérer. R4T est transmise par pas de 0,5 °C.
- `sensor.espaltherma_target_delta_primaire` = ΔT cible **secondaire** chauffage (12),
  `…_secondaire` = ΔT de **refroidissement** (3) : les `entity_id` sont trompeurs.
- `sensor.temperature_depart_eau_temperature` s'appelle « retour eau » dans HA mais mesure un départ.
- **Aucun thermostat ne régule l'étage** (l'ancien thermostat Netatmo a été supprimé le 2026-10-08).
  Côté PAC, la demande de la zone radiateurs (« Thermostat 2 ») est à ON en permanence : les
  radiateurs ne sont régulés que par la loi d'eau et les vannes thermostatiques (la modulation
  Madoka ne concerne que le plancher).
- Une période où la PAC était coupée (ex. du 01/10 au 08/10/2026) donne des heures de marche qui
  sont de l'**ECS** : un COP calculé dessus ne dit rien du chauffage.
- Si une vanne est changée de réglage ou une pièce ajoutée, mettre à jour `pieces` dans
  `reglages.json` (avec `_maj`).
- Heures : HA renvoie de l'UTC, le script affiche l'heure locale.

## 3. Restituer

1. Verdict en une ou deux phrases (tout va bien / point d'attention / réglage à faire).
2. Un tableau court des chiffres clés de la période.
3. Pour chaque point d'attention : ce qu'on observe → le mécanisme → ce qu'il faut faire ou surveiller.
4. Réglages proposés : zone, point de la loi, valeur actuelle → valeur proposée, quand réévaluer.
   Si les données ne suffisent pas (période trop courte, maison pas stabilisée), le dire et proposer
   la période d'observation adaptée plutôt qu'un réglage.
