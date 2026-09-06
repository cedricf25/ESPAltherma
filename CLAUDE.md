# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Projet

Firmware PlatformIO/Arduino (C++) pour ESP32/ESP8266/M5StickC qui interroge le port série X10A d'une pompe à chaleur Daikin Altherma (ou Rotex), décode les registres et publie les valeurs en JSON sur MQTT (auto-découverte Home Assistant). Peut aussi piloter un relais thermostat, des relais Smart Grid et un relais de sécurité via MQTT.

## Commandes

```bash
pio run                       # build de l'env par défaut (esp32)
pio run -e m5stickcplus       # autres envs: nodemcuv2, esp32, m5stickc, m5stickcplus, m5stickcplus2, native
pio run -t upload             # flash USB
pio device monitor            # logs série (115200)
pio test -e native -v         # tests unitaires (nécessite g++ dans le PATH)
pio test -e native -f test_rotex_protocol   # un seul fichier de test
```

OTA : décommenter `upload_port = ESPAltherma.local` (et éventuellement `upload_protocol = espota`) dans la section d'env de [platformio.ini](platformio.ini).

Il n'y a ni linter ni CI configurés dans ce dépôt.

## Matériel cible de ce fork

Ce dépôt est configuré pour l'installation réelle du propriétaire — en tenir compte pour toute question sur les valeurs, registres ou fichiers `def/` :

- **Daikin Altherma 3 H HT N**, taille 18, monophasée, chauffage + ECS (air/eau haute température).
- Unité extérieure `EPRA18DV3` + unité intérieure `ETVZ16S23D6V`, ballon ECS 230 L, 11,97 kW, fluide **R32**.
- SCOP 4,51 à 35 °C / 3,58 à 55 °C ; ETAS 177 % à 35 °C / 140 % à 55 °C. Thermostat d'ambiance Daikin Madoka.
- Surface chauffée 160 m², en remplacement d'une chaudière fioul basse température.

Définitions pertinentes : série **EPRA D 14-18 kW avec intérieure ETV/ETB/ETVZ16**, protocole **I**. `src/setup.h` pointe sur `def/French/Altherma(EPRA D_D7 ETV16-ETB16-ETVZ16 E_E7 series 14-18kW).h`, qui couvre la taille 18 (l'ancien `... D series 14-16kW).h` reste dans le dépôt, ses 84 labels toujours activés). Les définitions LT, Monobloc, GEO, ECH2O et Mini chiller ne concernent pas cette machine.

Différences entre les deux définitions (le reste des registres est identique) : `0x63,13` "BUH capacité de sortie" utilise le convid **152** (14-18) au lieu de **311** (14-16), qui n'existe pas dans `converters.h` et produisait `Conv 311 not avail.` ; `0x63` offsets 8/10/11/12 et `0x62,8` bits 305-307/336 sont redéfinis (libellés « Not translated yet », les convid 317/323/336 restant non gérés par `converters.h` — ne pas les activer) ; le registre **`0x65`** est ajouté (hydro split DLWB2, kit bizone EKMIK) — non applicable ici, laissé commenté.

## Configuration (avant tout build)

[src/setup.h](src/setup.h) est le point d'entrée de configuration : WiFi, MQTT, broches RX/TX, broches relais, fréquence d'interrogation, et surtout **le `#include` de def/ à décommenter** qui sélectionne le modèle de pompe à chaleur.

- [src/main.cpp](src/main.cpp) inclut `my_setup.h` s'il existe, sinon `setup.h` (`#if __has_include("my_setup.h")`). `src/my_setup.h` est gitignoré — c'est le moyen propre de garder une config locale.
- Attention : dans ce fork, `src/setup.h` est **suivi par git** bien qu'il figure dans [.gitignore](.gitignore) (config personnelle committée, identifiants WiFi/MQTT en clair). Ne pas y ajouter de secrets supplémentaires, et vérifier ce qu'on committe en le modifiant.
- Un seul fichier `def/` doit être décommenté : chacun définit le tableau global `labelDefs[]` et la macro `LABELDEF`. Sans sélection, `DEFAULT.h` est inclus avec un `#warning`.

## Architecture

Tout est en headers inclus depuis [src/main.cpp](src/main.cpp) — pas de fichiers `.cpp` séparés, les objets globaux (`mqttSerial`, `client`, `MySerial`, `labelDefs[]`) sont définis dans les headers. **L'ordre des `#include` dans main.cpp est significatif** (setup.h → mqttserial.h → converters.h → comm.h → mqtt.h) : les headers suivants dépendent des macros et globales définies par les précédents.

Boucle principale (`loop()` dans main.cpp) : pour chaque registre de `registryIDs[]` → `queryRegistry()` → `converter.readRegistryValues()` → `updateValues()` (accumulation dans `jsonbuff`) → `sendValues()` en fin de cycle, puis `waitLoop(FREQUENCY - écoulé)`. `waitLoop`/`extraLoop` remplacent `delay()` pour continuer à servir MQTT et OTA pendant l'attente — ne jamais bloquer avec `delay()` dans les chemins longs.

Fichiers clés :

- [include/labeldef.h](include/labeldef.h) — classe `LabelDef` : `{registryID, offset, convid, dataSize, dataType, label}`. C'est le format de chaque ligne des fichiers `def/`.
- [include/def/](include/def/) — une définition par modèle de PAC (+ traductions dans `French/`, `German/`, `Spanish/`, `Italian/`, `Japanese/`). Les lignes commentées sont les valeurs non interrogées ; `initRegistries()` déduit la liste des registres à interroger des lignes actives (max 32 registres, message JSON limité à `MAX_MSG_SIZE`).
- [include/converters.h](include/converters.h) — classe `Converter` : `convert()` est un gros `switch` sur `convid` (100–119 signés, 151–165 non signés, 200+ tables d'énumération) qui remplit `LabelDef::asString`. Ces conversions sont reprises du logiciel Daikin DChecker ; les modifier casse la compatibilité des fichiers `def/`.
- [include/comm.h](include/comm.h) — trame série 9600 8E1, CRC = somme inversée. Deux protocoles : **I** (par défaut, requête `03 40 <reg> <crc>`, longueur de réponse dynamique lue au 3ᵉ octet) et **S** (requête `02 <reg> <crc>`, longueurs codées en dur par registre). Le protocole vient de la macro `PROTOCOL`, qu'un fichier `def/` peut surcharger (cf. [PROTOCOL_S_ROTEX.h](include/def/PROTOCOL_S_ROTEX.h)). L'offset de données diffère selon le protocole (1 pour S, 3 pour I).
- [include/mqtt.h](include/mqtt.h) — connexion/reconnexion, LWT, publication des configs d'auto-découverte HA, callbacks des topics `espaltherma/POWER`, `espaltherma/sg/set`, `espaltherma/SAFETY`. L'état du thermostat est persisté en EEPROM et restauré au boot.
- [include/mqttserial.h](include/mqttserial.h) — `Stream` qui duplique les logs sur `Serial`, l'écran M5 et le topic MQTT `espaltherma/log`.

Les variantes matérielles sont gérées par `#ifdef` (`ARDUINO_ARCH_ESP8266`, `ARDUINO_M5Stick_C`, `ARDUINO_M5Stick_C_Plus`, `ARDUINO_M5Stick_C_Plus2`) ; les fonctionnalités optionnelles par la présence de macros (`PIN_SG1`, `SAFETY_RELAY_PIN`, `MQTT_ENCRYPTED`, `ONEVAL_ONETOPIC`, `JSONTABLE`, `DISABLE_LOG_MESSAGES`). Toute nouvelle option de config suit ce schéma : macro commentée dans `setup.h` + blocs `#ifdef`.

## Tests

[test/test_rotex_protocol.cpp](test/test_rotex_protocol.cpp) tourne sur l'env `native` (Unity) : il inclut un fichier `def/` et `converters.h` directement, avec [test/arduino_to_native.h](test/arduino_to_native.h) qui bouchonne l'API Arduino. C'est le seul moyen de tester le décodage sans matériel — un test se rédige en fournissant une trame brute et en comparant la chaîne `label: valeur; ` produite.

## Documentation & outils

- [doc/Daikin I protocol.md](doc/Daikin%20I%20protocol.md) et [doc/Daikin S protocol.md](doc/Daikin%20S%20protocol.md) — format des trames, indispensable avant de toucher à `comm.h`.
- [doc/list_labels.csv](doc/list_labels.csv), [doc/list registries.txt](doc/list%20registries.txt) — inventaire des labels/registres connus.
- [contrib/](contrib/) — outils d'analyse hors firmware : `hp_emulator.py` (simulateur de PAC sur port série, pour tester sans matériel), `ktb_decoder.py`, `ldd_decoder/` (C#, extrait les définitions des fichiers Daikin DChecker pour générer des fichiers `def/`).
- [README.md](README.md) — procédure de câblage X10A, intégration Home Assistant, dépannage (timeouts, `0x15 0xEA`, erreurs CRC).
